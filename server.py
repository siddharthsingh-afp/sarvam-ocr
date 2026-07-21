"""
Affordplan — Sarvam OCR Service
Single upload — auto-classifies document and extracts all required fields.
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

# Single unified prompt — short and direct
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
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_from_content(content_bytes, content_type, filename):
    if b"PK\x03\x04" == content_bytes[:4] or "zip" in content_type or filename.endswith(".zip"):
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
            pass
    text = content_bytes.decode("utf-8", errors="ignore")
    if "<html" in text.lower() or "<!doctype" in text.lower():
        return html_to_text(text)
    return text.strip()


def run_sarvam_ocr(file_bytes, filename, mime):
    log = []

    # Step 1 — Create job
    r1 = req_lib.post(SARVAM_BASE, headers=sh(),
                      json={"job_parameters": {"language": "en-IN", "output_format": "html"}},
                      timeout=30)
    log.append(f"Step1={r1.status_code}")
    if not r1.ok:
        return {"error": f"Create job failed: {r1.status_code}", "_log": log}

    job_id = r1.json().get("job_id") or r1.json().get("id")
    log.append(f"job_id={job_id}")
    if not job_id:
        return {"error": "No job_id", "_log": log}

    # Step 2 — Register file
    r2 = req_lib.post(f"{SARVAM_BASE}/upload-files", headers=sh(),
                      json={"job_id": job_id, "files": [filename]}, timeout=30)
    log.append(f"Step2={r2.status_code}")
    if not r2.ok:
        return {"error": f"Register failed: {r2.status_code}", "_log": log}

    j2 = r2.json()
    upload_urls = j2.get("upload_urls") or j2.get("uploadUrls") or {}
    entry = upload_urls.get(filename) or (list(upload_urls.values())[0] if upload_urls else None)
    upload_url = ""
    if isinstance(entry, dict):
        upload_url = entry.get("file_url") or entry.get("url") or ""
    elif isinstance(entry, str):
        upload_url = entry
    if not upload_url:
        return {"error": "No upload URL", "_log": log}

    # Step 3 — Upload to Azure blob
    r3 = req_lib.put(upload_url,
                     headers={"x-ms-blob-type": "BlockBlob", "Content-Type": mime},
                     data=file_bytes, timeout=60)
    log.append(f"Step3={r3.status_code}")
    if r3.status_code not in (200, 201):
        return {"error": f"Blob upload failed: {r3.status_code}", "_log": log}

    # Step 4 — Start job
    r4 = req_lib.post(f"{SARVAM_BASE}/{job_id}/start",
                      headers={**sh(), "X-Dashboard": "true"}, timeout=30)
    log.append(f"Step4={r4.status_code}")

    # Step 5 — Poll until complete
    final_status = {}
    for attempt in range(40):
        time.sleep(2)
        r5 = req_lib.get(f"{SARVAM_BASE}/{job_id}/status",
                         headers={"api-subscription-key": SARVAM_KEY}, timeout=30)
        if not r5.ok:
            continue
        j5 = r5.json()
        final_status = j5
        state = (j5.get("job_state") or j5.get("status") or j5.get("state") or "").lower()
        log.append(f"Poll{attempt+1}:state={state}")
        if "complet" in state or "success" in state:
            break
        if "fail" in state or "error" in state:
            return {"error": f"Job failed: {state}", "_log": log}
    else:
        return {"error": "Timed out", "_log": log}

    # Step 6 — Download document.zip
    extracted_text = ""
    r6 = req_lib.post(f"{SARVAM_BASE}/{job_id}/download-files",
                      headers=sh(), json={"files": ["document.zip"]}, timeout=30)
    log.append(f"Step6={r6.status_code}")
    if r6.ok:
        j6 = r6.json()
        dl_urls = j6.get("download_urls") or {}
        entry6 = dl_urls.get("document.zip") or (list(dl_urls.values())[0] if dl_urls else None)
        dl_url = ""
        if isinstance(entry6, dict):
            dl_url = entry6.get("file_url") or ""
        elif isinstance(entry6, str):
            dl_url = entry6
        if dl_url:
            rd = req_lib.get(dl_url, timeout=60)
            log.append(f"fetch={rd.status_code} len={len(rd.content)}")
            if rd.ok:
                extracted_text = extract_from_content(rd.content, rd.headers.get("content-type",""), "document.zip")
                log.append(f"text_len={len(extracted_text)}")

    if not extracted_text:
        return {"error": "No text extracted", "_log": log}

    # Step 7 — Extract fields via Sarvam 30B
    rx = req_lib.post(SARVAM_CHAT, headers=sh(), json={
        "model": "sarvam-30b",
        "messages": [
            {"role": "system", "content": "Return ONLY valid JSON. No explanation. No markdown. No reasoning."},
            {"role": "user",   "content": UNIFIED_PROMPT + "\n\nDOCUMENT TEXT:\n" + extracted_text}
        ],
        "max_tokens": 4000,
        "temperature": 0
    }, timeout=120)

    log.append(f"Step7={rx.status_code}")
    if not rx.ok:
        return {"error": f"Extraction failed: {rx.status_code}", "_log": log,
                "raw_text": extracted_text[:500]}

    rx_json = rx.json()
    finish_reason = (rx_json.get("choices") or [{}])[0].get("finish_reason", "")
    log.append(f"finish_reason={finish_reason}")

    raw = (rx_json.get("choices") or [{}])[0]
    raw = (raw.get("message") or {}).get("content") or ""
    raw = re.sub(r"^```json\s*", "", raw).strip()
    raw = re.sub(r"\s*```$", "", raw).strip()

    log.append(f"raw_len={len(raw)}")

    try:
        result = json.loads(raw)
    except Exception:
        result = {"raw_text": raw}

    result["_log"] = log
    return result


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "affordplan-sarvam-ocr", "key_set": bool(SARVAM_KEY)})


@app.route("/compare", methods=["POST"])
def compare():
    try:
        if not SARVAM_KEY:
            return jsonify({"error": "SARVAM_API_KEY not set on server"}), 500
        if "image" not in request.files:
            return jsonify({"error": "No image. Send form field 'image'."}), 400

        f        = request.files["image"]
        filename = f.filename or "document.jpg"
        suffix   = os.path.splitext(filename)[1].lower()
        if suffix not in ALLOWED:
            return jsonify({"error": f"Unsupported format '{suffix}'"}), 400

        mime   = f.content_type or "image/jpeg"
        result = run_sarvam_ocr(f.read(), filename, mime)
        return jsonify({"sarvam": result})
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[-500:]}), 500


@app.route("/", methods=["GET"])
def index():
    html_page = Path(__file__).parent / "ocr_test_page.html"
    if html_page.exists():
        return html_page.read_text(), 200, {"Content-Type": "text/html"}
    return jsonify({"service": "Affordplan Sarvam OCR", "endpoint": "POST /compare"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5002"))
    print(f"Affordplan Sarvam OCR — http://localhost:{port}")
    app.run(debug=False, host="0.0.0.0", port=port)
