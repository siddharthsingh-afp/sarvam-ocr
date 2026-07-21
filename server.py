"""
Affordplan — Sarvam Doc AI
Reads any medical document and returns all text extracted from it.
100% India servers. DPDP compliant. No external AI calls.
Set SARVAM_API_KEY as environment variable on Render.
"""

import os, re, time, zipfile, io
from pathlib import Path
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests as req_lib

app = Flask(__name__)
CORS(app)

SARVAM_KEY  = os.environ.get("SARVAM_API_KEY", "")
SARVAM_BASE = "https://api.sarvam.ai/doc-digitization/job/v1"
ALLOWED     = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}


def sh():
    return {"Content-Type": "application/json", "api-subscription-key": SARVAM_KEY}


def html_to_text(html):
    """Strip HTML tags and return clean readable text"""
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


def extract_from_zip(content_bytes):
    """Unzip Sarvam output and return text"""
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


def run_sarvam(file_bytes, filename, mime):
    """Run Sarvam Doc AI pipeline and return extracted text"""

    # Step 1 — Create job
    r1 = req_lib.post(SARVAM_BASE, headers=sh(),
                      json={"job_parameters": {"language": "en-IN", "output_format": "html"}},
                      timeout=30)
    if not r1.ok:
        return None, f"Create job failed: {r1.status_code}"

    job_id = r1.json().get("job_id") or r1.json().get("id")
    if not job_id:
        return None, "No job_id returned"

    # Step 2 — Register file
    r2 = req_lib.post(f"{SARVAM_BASE}/upload-files", headers=sh(),
                      json={"job_id": job_id, "files": [filename]}, timeout=30)
    if not r2.ok:
        return None, f"Register failed: {r2.status_code}"

    upload_urls = r2.json().get("upload_urls") or {}
    entry = upload_urls.get(filename) or (list(upload_urls.values())[0] if upload_urls else None)
    upload_url = (entry.get("file_url") or "") if isinstance(entry, dict) else (entry or "")
    if not upload_url:
        return None, "No upload URL"

    # Step 3 — Upload to Azure blob
    r3 = req_lib.put(upload_url,
                     headers={"x-ms-blob-type": "BlockBlob", "Content-Type": mime},
                     data=file_bytes, timeout=60)
    if r3.status_code not in (200, 201):
        return None, f"Upload failed: {r3.status_code}"

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
            return None, f"Sarvam job failed: {state}"
    else:
        return None, "Sarvam timed out after 80 seconds"

    # Step 6 — Download document.zip
    r6 = req_lib.post(f"{SARVAM_BASE}/{job_id}/download-files",
                      headers=sh(), json={"files": ["document.zip"]}, timeout=30)
    if not r6.ok:
        return None, "Download request failed"

    dl_urls = r6.json().get("download_urls") or {}
    entry6  = dl_urls.get("document.zip") or (list(dl_urls.values())[0] if dl_urls else None)
    dl_url  = (entry6.get("file_url") or "") if isinstance(entry6, dict) else (entry6 or "")
    if not dl_url:
        return None, "No download URL"

    rd = req_lib.get(dl_url, timeout=60)
    if not rd.ok:
        return None, "File download failed"

    # Extract text from ZIP
    text = extract_from_zip(rd.content)
    if not text:
        # Try as plain HTML
        text = html_to_text(rd.content.decode("utf-8", errors="ignore"))
    if not text:
        return None, "No text extracted from document"

    return text, None


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status":   "ok",
        "service":  "affordplan-sarvam-ocr",
        "key_set":  bool(SARVAM_KEY),
        "data":     "India only — DPDP compliant"
    })


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

        mime = f.content_type or "image/jpeg"
        text, err = run_sarvam(f.read(), filename, mime)

        if err:
            return jsonify({"error": err}), 500

        return jsonify({
            "sarvam": {
                "raw_text":  text,
                "char_count": len(text)
            }
        })

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()[-500:]}), 500


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
