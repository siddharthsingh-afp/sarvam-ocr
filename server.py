"""
Affordplan — Sarvam OCR Service
Works exactly like the Claude OCR service.
POST /compare with form field 'image' — returns extracted fields.
Set SARVAM_API_KEY as environment variable on Render.
"""

import os, re, json, time
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests as req_lib

app = Flask(__name__)
CORS(app)

SARVAM_KEY  = os.environ.get("SARVAM_API_KEY", "")
SARVAM_BASE = "https://api.sarvam.ai/doc-digitization/job/v1"
SARVAM_CHAT = "https://api.sarvam.ai/v1/chat/completions"
ALLOWED     = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}

PROMPTS = {
    "medicine": """From this document text extract ONLY these fields as JSON:
{"patient_name":null,"doctor_name":null,"hospital_name":null,"date":null,"diagnosis":null,
"medicines":[{"name":null,"strength":null,"form":null,"frequency":null,"duration":null,"quantity":null,"confidence":"high"}]}
Rules: frequency=Indian format e.g.1-0-1/BD/TDS. quantity=frequency x duration as integer. Return ONLY valid JSON.""",

    "labtest": """From this document text extract ONLY these fields as JSON:
{"patient_name":null,"doctor_name":null,"hospital_name":null,"date":null,
"lab_tests":[{"name":null,"value":null,"unit":null,"reference_range":null,"flag":null,"confidence":"high"}]}
Rules: flag=NORMAL/HIGH/LOW/ABNORMAL. Return ONLY valid JSON.""",

    "records": """From this document text extract ONLY these fields as JSON:
{"document_type":null,"patient_name":null,"patient_uhid":null,"doctor_name":null,
"hospital_name":null,"date":null,"diagnosis":null,"is_handwritten":null,"summary":null}
Rules: document_type=prescription/lab_report/bill/imaging/discharge/other. Return ONLY valid JSON.""",

    "maternity": """From this document text extract ONLY these fields as JSON:
{"patient_name":null,"edd":null,"lmp":null}
Rules: edd=Expected Date of Delivery YYYY-MM-DD. lmp=Last Menstrual Period YYYY-MM-DD. Return ONLY valid JSON.""",
}

def sh():
    return {"Content-Type": "application/json", "api-subscription-key": SARVAM_KEY}

def sleep(s):
    time.sleep(s)

def run_sarvam_ocr(file_bytes, filename, mime, flow="records"):
    # Step 1 — Create job
    r1 = req_lib.post(SARVAM_BASE, headers=sh(),
                      json={"job_parameters": {"language": "en-IN", "output_format": "md"}},
                      timeout=30)
    if not r1.ok:
        return {"error": f"Create job failed: {r1.status_code} {r1.text[:200]}"}
    job_id = r1.json().get("job_id") or r1.json().get("id")
    if not job_id:
        return {"error": "No job_id returned: " + r1.text[:200]}

    # Step 2 — Register file
    r2 = req_lib.post(f"{SARVAM_BASE}/upload-files", headers=sh(),
                      json={"job_id": job_id, "files": [filename]}, timeout=30)
    if not r2.ok:
        return {"error": f"Register file failed: {r2.status_code} {r2.text[:200]}"}
    j2 = r2.json()
    upload_url = (j2.get("upload_urls", {}).get(filename, {}) or {})
    if isinstance(upload_url, dict):
        upload_url = upload_url.get("file_url", "")
    if not upload_url:
        # Try first value
        vals = list(j2.get("upload_urls", {}).values())
        if vals:
            upload_url = vals[0].get("file_url", "") if isinstance(vals[0], dict) else vals[0]
    if not upload_url:
        return {"error": "No upload URL: " + str(j2)[:200]}

    # Step 3 — Upload file to Azure blob
    r3 = req_lib.put(upload_url,
                     headers={"x-ms-blob-type": "BlockBlob", "Content-Type": mime},
                     data=file_bytes, timeout=60)
    if r3.status_code not in (200, 201):
        return {"error": f"File upload failed: {r3.status_code}"}

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
        j5 = r5.json()
        state = (j5.get("job_state") or j5.get("status") or j5.get("state") or "").lower()
        if "complet" in state or "success" in state:
            output_file = (j5.get("output_files") or [None])[0] or \
                          (j5.get("results") or [None])[0] or ""
            break
        if "fail" in state or "error" in state:
            return {"error": "Sarvam job failed: " + str(j5)[:200]}
    else:
        return {"error": "Sarvam job timed out"}

    # Step 6 — Download output
    extracted_text = ""
    if output_file:
        r6 = req_lib.post(f"{SARVAM_BASE}/{job_id}/download-files",
                          headers=sh(), json={"files": [output_file]}, timeout=30)
        if r6.ok:
            j6 = r6.json()
            dl_urls = j6.get("download_urls") or j6.get("urls") or {}
            dl_url = ""
            entry = dl_urls.get(output_file, {})
            if isinstance(entry, dict):
                dl_url = entry.get("file_url", "")
            elif isinstance(entry, str):
                dl_url = entry
            if not dl_url and dl_urls:
                first = list(dl_urls.values())[0]
                dl_url = first.get("file_url", "") if isinstance(first, dict) else first
            if dl_url:
                rd = req_lib.get(dl_url, timeout=60)
                if rd.ok:
                    extracted_text = rd.text

    if not extracted_text:
        return {"error": "No text extracted from document"}

    # Step 7 — Apply targeted prompt via Sarvam 30B
    prompt = PROMPTS.get(flow, PROMPTS["records"])
    rx = req_lib.post(SARVAM_CHAT, headers=sh(), json={
        "model": "sarvam-30b",
        "messages": [
            {"role": "system", "content": "You are a medical data extractor. Return ONLY valid JSON. No markdown."},
            {"role": "user",   "content": prompt + "\n\nDOCUMENT TEXT:\n" + extracted_text}
        ],
        "max_tokens": 1500,
        "temperature": 0
    }, timeout=60)

    if not rx.ok:
        return {"error": f"Extraction failed: {rx.status_code}", "raw_text": extracted_text[:500]}

    raw = rx.json().get("choices", [{}])[0].get("message", {}).get("content", "")
    raw = re.sub(r"^```json\s*", "", raw).strip()
    raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        result = json.loads(raw)
    except Exception:
        result = {"raw_text": raw}

    result["_raw_extracted"] = extracted_text[:500]
    result["_flow"] = flow
    return result


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "affordplan-sarvam-ocr",
        "key_set": bool(SARVAM_KEY)
    })


@app.route("/compare", methods=["POST"])
def compare():
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
    flow = request.form.get("flow", "records")

    result = run_sarvam_ocr(f.read(), filename, mime, flow)
    return jsonify({"sarvam": result})


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "Affordplan Sarvam OCR",
        "endpoint": "POST /compare",
        "fields": "image (file), flow (medicine/labtest/records/maternity)",
        "health": "/health"
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5002"))
    print(f"Affordplan Sarvam OCR — http://localhost:{port}")
    app.run(debug=False, host="0.0.0.0", port=port)
