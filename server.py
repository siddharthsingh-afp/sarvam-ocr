"""
Affordplan — Sarvam OCR Service
POST /compare with form field 'image' and 'flow'
Set SARVAM_API_KEY as environment variable on Render.
"""

import os, re, json, time
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


def run_sarvam_ocr(file_bytes, filename, mime, flow="records"):
    log = []  # collect debug info

    # Step 1 — Create job
    r1 = req_lib.post(SARVAM_BASE, headers=sh(),
                      json={"job_parameters": {"language": "en-IN", "output_format": "md"}},
                      timeout=30)
    log.append(f"Step1 status={r1.status_code}")
    if not r1.ok:
        return {"error": f"Create job failed: {r1.status_code}", "_log": log, "_raw": r1.text[:300]}

    j1 = r1.json()
    job_id = j1.get("job_id") or j1.get("id")
    log.append(f"job_id={job_id}")
    if not job_id:
        return {"error": "No job_id", "_log": log, "_j1": j1}

    # Step 2 — Register file
    r2 = req_lib.post(f"{SARVAM_BASE}/upload-files", headers=sh(),
                      json={"job_id": job_id, "files": [filename]}, timeout=30)
    log.append(f"Step2 status={r2.status_code}")
    if not r2.ok:
        return {"error": f"Register failed: {r2.status_code}", "_log": log, "_raw": r2.text[:300]}

    j2 = r2.json()
    log.append(f"Step2 response keys={list(j2.keys())}")

    # Extract upload URL — handle all possible shapes
    upload_url = ""
    upload_urls = j2.get("upload_urls") or j2.get("uploadUrls") or {}
    entry = upload_urls.get(filename) or (list(upload_urls.values())[0] if upload_urls else None)
    if isinstance(entry, dict):
        upload_url = entry.get("file_url") or entry.get("url") or ""
    elif isinstance(entry, str):
        upload_url = entry
    log.append(f"upload_url_found={'yes' if upload_url else 'no'}")
    if not upload_url:
        return {"error": "No upload URL", "_log": log, "_j2": j2}

    # Step 3 — Upload to Azure blob
    r3 = req_lib.put(upload_url,
                     headers={"x-ms-blob-type": "BlockBlob", "Content-Type": mime},
                     data=file_bytes, timeout=60)
    log.append(f"Step3 blob upload status={r3.status_code}")
    if r3.status_code not in (200, 201):
        return {"error": f"Blob upload failed: {r3.status_code}", "_log": log}

    # Step 4 — Start job
    r4 = req_lib.post(f"{SARVAM_BASE}/{job_id}/start",
                      headers={**sh(), "X-Dashboard": "true"}, timeout=30)
    log.append(f"Step4 start status={r4.status_code}")

    # Step 5 — Poll until complete (max 80s)
    output_file = ""
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
        log.append(f"Poll {attempt+1}: state={state} keys={list(j5.keys())}")

        if "complet" in state or "success" in state:
            # Try every possible field for output file name
            for key in ("output_files", "outputFiles", "results", "output", "files"):
                val = j5.get(key)
                if isinstance(val, list) and val:
                    output_file = val[0]
                    break
                elif isinstance(val, str) and val:
                    output_file = val
                    break
            log.append(f"output_file={output_file}")
            break

        if "fail" in state or "error" in state:
            return {"error": f"Sarvam job failed: {state}", "_log": log, "_status": j5}

    else:
        return {"error": "Timed out", "_log": log, "_last_status": final_status}

    # Step 6a — Get download URL
    extracted_text = ""
    if output_file:
        r6 = req_lib.post(f"{SARVAM_BASE}/{job_id}/download-files",
                          headers=sh(), json={"files": [output_file]}, timeout=30)
        log.append(f"Step6a download-files status={r6.status_code}")
        if r6.ok:
            j6 = r6.json()
            log.append(f"Step6a keys={list(j6.keys())}")
            dl_urls = j6.get("download_urls") or j6.get("urls") or {}
            entry6 = dl_urls.get(output_file) or (list(dl_urls.values())[0] if dl_urls else None)
            dl_url = ""
            if isinstance(entry6, dict):
                dl_url = entry6.get("file_url") or entry6.get("url") or ""
            elif isinstance(entry6, str):
                dl_url = entry6
            log.append(f"dl_url_found={'yes' if dl_url else 'no'}")
            if dl_url:
                rd = req_lib.get(dl_url, timeout=60)
                log.append(f"Step6b fetch status={rd.status_code} len={len(rd.text)}")
                if rd.ok:
                    extracted_text = rd.text

    # Fallback — check status response for inline text
    if not extracted_text:
        for key in ("output", "text", "content", "extracted_text", "markdown"):
            val = final_status.get(key, "")
            if val and isinstance(val, str):
                extracted_text = val
                log.append(f"Got text from status.{key}")
                break

    if not extracted_text:
        return {
            "error": "No text extracted",
            "_log": log,
            "_final_status": final_status,
            "_output_file": output_file
        }

    log.append(f"extracted_text_len={len(extracted_text)}")

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

    log.append(f"Step7 extract status={rx.status_code}")
    if not rx.ok:
        return {"error": f"Extraction failed: {rx.status_code}", "_log": log,
                "raw_text": extracted_text[:500]}

    raw = rx.json().get("choices", [{}])[0].get("message", {}).get("content", "")
    raw = re.sub(r"^```json\s*", "", raw).strip()
    raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        result = json.loads(raw)
    except Exception:
        result = {"raw_text": raw}

    result["_log"] = log
    result["_flow"] = flow
    return result


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "affordplan-sarvam-ocr", "key_set": bool(SARVAM_KEY)})


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
    html_page = Path(__file__).parent / "ocr_test_page.html"
    if html_page.exists():
        return html_page.read_text(), 200, {"Content-Type": "text/html"}
    return jsonify({"service": "Affordplan Sarvam OCR", "endpoint": "POST /compare"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5002"))
    print(f"Affordplan Sarvam OCR — http://localhost:{port}")
    app.run(debug=False, host="0.0.0.0", port=port)
