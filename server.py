"""
Affordplan — Sarvam OCR Service
Two endpoints:
  POST /extract-text  — runs Sarvam Doc AI, returns raw extracted text
  POST /extract-fields — runs Sarvam 30B on text, returns structured JSON
Set SARVAM_API_KEY as environment variable on Render.
"""

import os, re, json, time, zipfile, io
from pathlib import Path
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests as req_lib

app = Flask(__name__)
CORS(app)

SARVAM_KEY  = os.environ.get("SARVAM_API_KEY", "")
SARVAM_BASE = "https://api.sarvam.ai/doc-digitization/job/v1"
SARVAM_CHAT = "https://api.sarvam.ai/v1/chat/completions"
ALLOWED     = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}

UNIFIED_PROMPT = """Extract ALL information from this medical document text. Return ONLY this JSON, nothing else:

{"document_type":"","patient_name":"","patient_uhid":"","doctor_name":"","hospital_name":"","date":"","diagnosis":"","is_handwritten":false,"summary":"","edd":"","lmp":"","medicines":[],"lab_tests":[],"bill_items":[],"total_amount":""}

document_type = prescription OR lab_report OR bill OR imaging OR discharge OR other
medicines items = {"name":"","strength":"","form":"","frequency":"","duration":"","quantity":"","confidence":"high"}
lab_tests items = {"name":"","value":"","unit":"","reference_range":"","flag":"","confidence":"high"}
bill_items items = {"description":"","amount":""}
Use empty string for missing values. Return ONLY JSON."""


def sh():
    return {"Content-Type": "application/json", "api-subscription-key": SARVAM_KEY}


def html_to_text(html):
    text = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</p>|</div>|</h[1-6]>|</tr>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_from_zip(content_bytes):
    try:
        z = zipfile.ZipFile(io.BytesIO(content_bytes))
        parts = []
        for name in z.namelist():
            raw = z.read(name).decode("utf-8", errors="ignore")
            if name.endswith(".html"):
                parts.append(html_to_text(raw))
            elif name.endswith((".md", ".txt")):
                parts.append(raw)
        return "\n".join(parts).strip()
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT 1 — Run Sarvam Doc AI, return extracted text (~30-60s)
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/extract-text", methods=["POST"])
def extract_text():
    if not SARVAM_KEY:
        return jsonify({"error": "SARVAM_API_KEY not set"}), 500
    if "image" not in request.files:
        return jsonify({"error": "No image"}), 400

    f        = request.files["image"]
    filename = f.filename or "document.jpg"
    suffix   = os.path.splitext(filename)[1].lower()
    if suffix not in ALLOWED:
        return jsonify({"error": f"Unsupported format '{suffix}'"}), 400

    mime       = f.content_type or "image/jpeg"
    file_bytes = f.read()

    # Step 1 — Create job
    r1 = req_lib.post(SARVAM_BASE, headers=sh(),
                      json={"job_parameters": {"language": "en-IN", "output_format": "html"}},
                      timeout=30)
    if not r1.ok:
        return jsonify({"error": f"Create job failed: {r1.status_code}"}), 500

    job_id = r1.json().get("job_id") or r1.json().get("id")
    if not job_id:
        return jsonify({"error": "No job_id"}), 500

    # Step 2 — Register file
    r2 = req_lib.post(f"{SARVAM_BASE}/upload-files", headers=sh(),
                      json={"job_id": job_id, "files": [filename]}, timeout=30)
    if not r2.ok:
        return jsonify({"error": f"Register failed: {r2.status_code}"}), 500

    j2 = r2.json()
    upload_urls = j2.get("upload_urls") or {}
    entry = upload_urls.get(filename) or (list(upload_urls.values())[0] if upload_urls else None)
    upload_url = (entry.get("file_url") or "") if isinstance(entry, dict) else (entry or "")
    if not upload_url:
        return jsonify({"error": "No upload URL"}), 500

    # Step 3 — Upload to Azure blob
    r3 = req_lib.put(upload_url,
                     headers={"x-ms-blob-type": "BlockBlob", "Content-Type": mime},
                     data=file_bytes, timeout=60)
    if r3.status_code not in (200, 201):
        return jsonify({"error": f"Upload failed: {r3.status_code}"}), 500

    # Step 4 — Start job
    req_lib.post(f"{SARVAM_BASE}/{job_id}/start",
                 headers={**sh(), "X-Dashboard": "true"}, timeout=30)

    # Step 5 — Poll until complete
    for _ in range(40):
        time.sleep(2)
        r5 = req_lib.get(f"{SARVAM_BASE}/{job_id}/status",
                         headers={"api-subscription-key": SARVAM_KEY}, timeout=30)
        if not r5.ok:
            continue
        state = (r5.json().get("job_state") or "").lower()
        if "complet" in state or "success" in state:
            break
        if "fail" in state or "error" in state:
            return jsonify({"error": f"Sarvam job failed: {state}"}), 500
    else:
        return jsonify({"error": "Sarvam timed out"}), 500

    # Step 6 — Download document.zip
    r6 = req_lib.post(f"{SARVAM_BASE}/{job_id}/download-files",
                      headers=sh(), json={"files": ["document.zip"]}, timeout=30)
    if not r6.ok:
        return jsonify({"error": "Download failed"}), 500

    j6 = r6.json()
    dl_urls = j6.get("download_urls") or {}
    entry6 = dl_urls.get("document.zip") or (list(dl_urls.values())[0] if dl_urls else None)
    dl_url = (entry6.get("file_url") or "") if isinstance(entry6, dict) else (entry6 or "")
    if not dl_url:
        return jsonify({"error": "No download URL"}), 500

    rd = req_lib.get(dl_url, timeout=60)
    if not rd.ok:
        return jsonify({"error": "Fetch failed"}), 500

    extracted_text = extract_from_zip(rd.content)
    if not extracted_text:
        # Try as plain HTML
        extracted_text = html_to_text(rd.content.decode("utf-8", errors="ignore"))

    if not extracted_text:
        return jsonify({"error": "No text extracted"}), 500

    return jsonify({"text": extracted_text, "job_id": job_id})


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT 2 — Run Sarvam 30B on text, return structured fields (~10-20s)
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/extract-fields", methods=["POST"])
def extract_fields():
    if not SARVAM_KEY:
        return jsonify({"error": "SARVAM_API_KEY not set"}), 500

    body = request.get_json(force=True) or {}
    text = body.get("text", "")
    if not text:
        return jsonify({"error": "No text provided"}), 400

    rx = req_lib.post(SARVAM_CHAT, headers=sh(), json={
        "model": "sarvam-30b",
        "messages": [
            {"role": "system", "content": "Return ONLY valid JSON. No explanation. No markdown."},
            {"role": "user",   "content": UNIFIED_PROMPT + "\n\nDOCUMENT TEXT:\n" + text}
        ],
        "max_tokens": 4000,
        "temperature": 0
    }, timeout=60)

    if not rx.ok:
        return jsonify({"error": f"Sarvam 30B failed: {rx.status_code}"}), 500

    rx_json  = rx.json()
    finish   = (rx_json.get("choices") or [{}])[0].get("finish_reason", "")
    raw      = (rx_json.get("choices") or [{}])[0]
    raw      = (raw.get("message") or {}).get("content") or ""
    raw      = re.sub(r"^```json\s*", "", raw).strip()
    raw      = re.sub(r"\s*```$", "", raw).strip()

    if not raw:
        return jsonify({"error": f"Sarvam 30B returned empty (finish_reason={finish})"}), 500

    try:
        result = json.loads(raw)
    except Exception:
        result = {"raw_text": raw}

    return jsonify({"sarvam": result})


# ─────────────────────────────────────────────────────────────────────────────
# Legacy single endpoint — kept for compatibility
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/compare", methods=["POST"])
def compare():
    return jsonify({"error": "Use /extract-text then /extract-fields instead"}), 400


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "affordplan-sarvam-ocr", "key_set": bool(SARVAM_KEY)})


@app.route("/", methods=["GET"])
def index():
    html_page = Path(__file__).parent / "ocr_test_page.html"
    if html_page.exists():
        return html_page.read_text(), 200, {"Content-Type": "text/html"}
    return jsonify({"service": "Affordplan Sarvam OCR"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5002"))
    print(f"Affordplan Sarvam OCR — http://localhost:{port}")
    app.run(debug=False, host="0.0.0.0", port=port)
