"""
Affordplan — Sarvam OCR + Sarvam 30B
Two step: Doc AI reads → 30B extracts fields with simple prompt
"""
import os, re, json, time, zipfile, io
from pathlib import Path
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests as req_lib

app = Flask(__name__)
CORS(app)

SARVAM_DOC_KEY  = os.environ.get("SARVAM_DOC_KEY") or os.environ.get("SARVAM_API_KEY", "")
SARVAM_CHAT_KEY = os.environ.get("SARVAM_CHAT_KEY") or os.environ.get("SARVAM_API_KEY", "")
SARVAM_BASE     = "https://api.sarvam.ai/doc-digitization/job/v1"
SARVAM_CHAT     = "https://api.sarvam.ai/v1/chat/completions"
ALLOWED         = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}

# Absolute minimum prompt
PROMPT = """Extract these fields from the medical document text below.
Return ONLY a JSON object. No explanation.

Fields:
- patient_name
- patient_age  
- patient_sex
- patient_uhid
- doctor_name
- hospital_name
- date
- medicines (list of medicine names)
- lab_tests (list of test names)

TEXT:
"""

def doc_sh():
    return {"Content-Type":"application/json","api-subscription-key":SARVAM_DOC_KEY}

def chat_sh():
    return {"Content-Type":"application/json","api-subscription-key":SARVAM_CHAT_KEY}

def html_to_text(html):
    text = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</p>|</div>|</h[1-6]>|</tr>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&amp;","&",text).replace("&nbsp;"," ")
    text = re.sub(r"[ \t]+"," ",text)
    text = re.sub(r"\n{3,}","\n\n",text)
    return text.strip()

def extract_from_zip(content_bytes):
    try:
        z = zipfile.ZipFile(io.BytesIO(content_bytes))
        parts = []
        for name in z.namelist():
            raw = z.read(name).decode("utf-8", errors="ignore")
            if name.endswith(".html"):
                parts.append(html_to_text(raw))
            elif name.endswith((".md",".txt")):
                parts.append(raw)
        return "\n".join(parts).strip()
    except Exception:
        return ""

def run_doc_ai(file_bytes, filename, mime):
    r1 = req_lib.post(SARVAM_BASE, headers=doc_sh(),
                      json={"job_parameters":{"language":"en-IN","output_format":"html"}},
                      timeout=30)
    if not r1.ok: return None, f"Step1 failed: {r1.status_code}"
    job_id = r1.json().get("job_id") or r1.json().get("id")
    if not job_id: return None, "No job_id"

    r2 = req_lib.post(f"{SARVAM_BASE}/upload-files", headers=doc_sh(),
                      json={"job_id":job_id,"files":[filename]}, timeout=30)
    if not r2.ok: return None, f"Step2 failed: {r2.status_code}"
    upload_urls = r2.json().get("upload_urls") or {}
    entry = upload_urls.get(filename) or (list(upload_urls.values())[0] if upload_urls else None)
    upload_url = (entry.get("file_url","") if isinstance(entry,dict) else entry) or ""
    if not upload_url: return None, "No upload URL"

    r3 = req_lib.put(upload_url,
                     headers={"x-ms-blob-type":"BlockBlob","Content-Type":mime},
                     data=file_bytes, timeout=60)
    if r3.status_code not in (200,201): return None, f"Step3 failed: {r3.status_code}"

    req_lib.post(f"{SARVAM_BASE}/{job_id}/start",
                 headers={**doc_sh(),"X-Dashboard":"true"}, timeout=30)

    for _ in range(40):
        time.sleep(2)
        r5 = req_lib.get(f"{SARVAM_BASE}/{job_id}/status",
                         headers={"api-subscription-key":SARVAM_DOC_KEY}, timeout=30)
        if not r5.ok: continue
        state = (r5.json().get("job_state") or "").lower()
        if "complet" in state or "success" in state: break
        if "fail" in state or "error" in state: return None, f"Job failed: {state}"
    else:
        return None, "Timed out"

    r6 = req_lib.post(f"{SARVAM_BASE}/{job_id}/download-files",
                      headers=doc_sh(), json={"files":["document.zip"]}, timeout=30)
    if not r6.ok: return None, "Download failed"
    dl_urls = r6.json().get("download_urls") or {}
    entry6 = dl_urls.get("document.zip") or (list(dl_urls.values())[0] if dl_urls else None)
    dl_url = (entry6.get("file_url","") if isinstance(entry6,dict) else entry6) or ""
    if not dl_url: return None, "No dl URL"

    rd = req_lib.get(dl_url, timeout=60)
    if not rd.ok: return None, "Fetch failed"
    text = extract_from_zip(rd.content)
    if not text:
        text = html_to_text(rd.content.decode("utf-8", errors="ignore"))
    if not text: return None, "No text extracted"
    return text, None

def run_30b(text):
    rx = req_lib.post(SARVAM_CHAT, headers=chat_sh(), json={
        "model": "sarvam-30b",
        "messages": [
            {"role":"system","content":"You are a medical data extractor. Return only valid JSON."},
            {"role":"user","content": PROMPT + text[:2000]}
        ],
        "max_tokens": 2000,
        "temperature": 0
    }, timeout=60)
    if not rx.ok: return None, f"30B failed: {rx.status_code}"
    finish = (rx.json().get("choices") or [{}])[0].get("finish_reason","")
    raw = (rx.json().get("choices") or [{}])[0]
    raw = (raw.get("message") or {}).get("content") or ""
    raw = re.sub(r"^```json\s*","",raw).strip()
    raw = re.sub(r"\s*```$","",raw).strip()
    if not raw: return None, f"30B empty (finish={finish})"
    try:
        return json.loads(raw), None
    except Exception:
        return {"raw":raw}, None

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status":"ok","doc_key":bool(SARVAM_DOC_KEY),"chat_key":bool(SARVAM_CHAT_KEY)})

@app.route("/compare", methods=["POST"])
def compare():
    try:
        if not SARVAM_DOC_KEY: return jsonify({"error":"SARVAM_API_KEY not set"}), 500
        if "image" not in request.files: return jsonify({"error":"No image"}), 400
        f = request.files["image"]
        filename = f.filename or "document.jpg"
        suffix = os.path.splitext(filename)[1].lower()
        if suffix not in ALLOWED: return jsonify({"error":f"Unsupported '{suffix}'"}), 400
        mime = f.content_type or "image/jpeg"

        # Step 1 — Sarvam Doc AI reads document
        text, err = run_doc_ai(f.read(), filename, mime)
        if err: return jsonify({"error":err}), 500

        # Step 2 — Sarvam 30B extracts fields
        result, err = run_30b(text)
        if err:
            # Return raw text if 30B fails
            return jsonify({"sarvam":{"raw_text":text,"error_30b":err}})

        result["raw_text"] = text
        return jsonify({"sarvam":result})
    except Exception as e:
        import traceback
        return jsonify({"error":str(e),"trace":traceback.format_exc()[-300:]}), 500

@app.route("/", methods=["GET"])
def index():
    html_page = Path(__file__).parent / "ocr_test_page.html"
    if html_page.exists():
        return html_page.read_text(), 200, {"Content-Type":"text/html"}
    return jsonify({"service":"Affordplan Sarvam OCR"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT","5002"))
    app.run(debug=False, host="0.0.0.0", port=port)
