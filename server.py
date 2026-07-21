"""
================================================================================
 AFFORDPLAN — SARVAM OCR PROXY SERVER
================================================================================
 Proxies all Sarvam API calls server-to-server (avoids browser CORS issues).

 Option C Flow:
   App → This server → Sarvam Vision (reads doc, Rs 0.50)
                     → Sarvam 30B (extracts required fields, ~Rs 0.05)
                     → App gets only required fields

 SETUP:
   pip install flask flask-cors requests gunicorn
   gunicorn server:app --bind 0.0.0.0:$PORT

 ENDPOINTS (called by the HTML page):
   POST /sarvam/create-job
   POST /sarvam/register-files
   POST /sarvam/upload-file
   POST /sarvam/start-job/<job_id>
   GET  /sarvam/job-status/<job_id>
   POST /sarvam/download-files/<job_id>
   POST /sarvam/fetch-output
   POST /sarvam/extract
   GET  /health
================================================================================
"""

import os
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests as req_lib

app = Flask(__name__)
CORS(app)

SARVAM_BASE = "https://api.sarvam.ai/doc-digitization/job/v1"
SARVAM_CHAT = "https://api.sarvam.ai/v1/chat/completions"


def sarvam_headers(api_key):
    return {
        "Content-Type": "application/json",
        "api-subscription-key": api_key
    }


def get_key():
    key = request.headers.get("X-Sarvam-Key", "")
    if not key:
        return None, jsonify({"error": "Missing X-Sarvam-Key header"}), 400
    return key, None, None


# ── Health check ──────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "affordplan-sarvam-proxy"})


@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "ok", "message": "Affordplan Sarvam OCR Proxy"})


# ── Step 1: Create job ─────────────────────────────────────────────────────────
@app.route("/sarvam/create-job", methods=["POST"])
def sarvam_create_job():
    api_key = request.headers.get("X-Sarvam-Key", "")
    if not api_key:
        return jsonify({"error": "Missing X-Sarvam-Key header"}), 400

    body = request.get_json(force=True) or {
        "job_parameters": {"language": "en-IN", "output_format": "md"}
    }

    try:
        r = req_lib.post(
            SARVAM_BASE,
            headers=sarvam_headers(api_key),
            json=body,
            timeout=30
        )
        try:
            return jsonify(r.json()), r.status_code
        except Exception:
            return jsonify({"error": r.text[:300]}), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Step 2: Register files ─────────────────────────────────────────────────────
@app.route("/sarvam/register-files", methods=["POST"])
def sarvam_register_files():
    api_key = request.headers.get("X-Sarvam-Key", "")
    if not api_key:
        return jsonify({"error": "Missing X-Sarvam-Key header"}), 400

    body = request.get_json(force=True) or {}

    try:
        r = req_lib.post(
            f"{SARVAM_BASE}/upload-files",
            headers=sarvam_headers(api_key),
            json=body,
            timeout=30
        )
        try:
            return jsonify(r.json()), r.status_code
        except Exception:
            return jsonify({"error": r.text[:300]}), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Step 3: Upload file to Azure blob ─────────────────────────────────────────
@app.route("/sarvam/upload-file", methods=["POST"])
def sarvam_upload_file():
    upload_url = request.headers.get("X-Upload-Url", "")
    if not upload_url:
        return jsonify({"error": "Missing X-Upload-Url header"}), 400
    if "image" not in request.files:
        return jsonify({"error": "No file — send form field 'image'"}), 400

    f = request.files["image"]
    file_bytes = f.read()
    mime = f.content_type or "application/octet-stream"

    try:
        r = req_lib.put(
            upload_url,
            headers={
                "x-ms-blob-type": "BlockBlob",
                "Content-Type": mime,
            },
            data=file_bytes,
            timeout=60
        )
        if r.status_code in (200, 201):
            return jsonify({"status": "uploaded"}), 200
        return jsonify({"error": f"Upload failed: {r.status_code}", "detail": r.text[:300]}), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Step 4: Start job ──────────────────────────────────────────────────────────
@app.route("/sarvam/start-job/<job_id>", methods=["POST"])
def sarvam_start_job(job_id):
    api_key = request.headers.get("X-Sarvam-Key", "")
    if not api_key:
        return jsonify({"error": "Missing X-Sarvam-Key header"}), 400

    hdrs = {**sarvam_headers(api_key), "X-Dashboard": "true"}

    try:
        r = req_lib.post(f"{SARVAM_BASE}/{job_id}/start", headers=hdrs, timeout=30)
        try:
            return jsonify(r.json()), r.status_code
        except Exception:
            return jsonify({"status": "started", "raw": r.text[:200]}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Step 5: Poll job status ────────────────────────────────────────────────────
@app.route("/sarvam/debug-status/<job_id>", methods=["GET"])
def sarvam_debug_status(job_id):
    """Debug — returns full raw Sarvam status response"""
    api_key = request.headers.get("X-Sarvam-Key", "")
    if not api_key:
        return jsonify({"error": "Missing X-Sarvam-Key header"}), 400
    try:
        r = req_lib.get(
            f"{SARVAM_BASE}/{job_id}/status",
            headers={"api-subscription-key": api_key},
            timeout=30
        )
        # Return raw text so we see exactly what Sarvam sends
        return app.response_class(
            response=r.text,
            status=r.status_code,
            mimetype="application/json"
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500



def sarvam_job_status(job_id):
    api_key = request.headers.get("X-Sarvam-Key", "")
    if not api_key:
        return jsonify({"error": "Missing X-Sarvam-Key header"}), 400

    try:
        r = req_lib.get(
            f"{SARVAM_BASE}/{job_id}/status",
            headers={"api-subscription-key": api_key},
            timeout=30
        )
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Step 6a: Get download URLs ─────────────────────────────────────────────────
@app.route("/sarvam/download-files/<job_id>", methods=["POST"])
def sarvam_download_files(job_id):
    api_key = request.headers.get("X-Sarvam-Key", "")
    if not api_key:
        return jsonify({"error": "Missing X-Sarvam-Key header"}), 400

    body = request.get_json(force=True) or {}

    try:
        r = req_lib.post(
            f"{SARVAM_BASE}/{job_id}/download-files",
            headers=sarvam_headers(api_key),
            json=body,
            timeout=30
        )
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Step 6b: Download output text from blob ────────────────────────────────────
@app.route("/sarvam/fetch-output", methods=["POST"])
def sarvam_fetch_output():
    body = request.get_json(force=True) or {}
    dl_url = body.get("url", "")
    if not dl_url:
        return jsonify({"error": "Missing url in body"}), 400

    try:
        r = req_lib.get(dl_url, timeout=60)
        if r.status_code == 200:
            return jsonify({"text": r.text}), 200
        return jsonify({"error": f"Download failed: {r.status_code}"}), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Step 6c: Apply targeted prompt via Sarvam 30B ─────────────────────────────
@app.route("/sarvam/extract", methods=["POST"])
def sarvam_extract():
    api_key = request.headers.get("X-Sarvam-Key", "")
    if not api_key:
        return jsonify({"error": "Missing X-Sarvam-Key header"}), 400

    body   = request.get_json(force=True) or {}
    prompt = body.get("prompt", "")
    text   = body.get("text", "")

    try:
        r = req_lib.post(
            SARVAM_CHAT,
            headers=sarvam_headers(api_key),
            json={
                "model": "sarvam-30b",
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a medical data extractor. Return ONLY valid JSON. No markdown. No explanation."
                    },
                    {
                        "role": "user",
                        "content": prompt + "\n\nDOCUMENT TEXT:\n" + text
                    }
                ],
                "max_tokens": 1500,
                "temperature": 0
            },
            timeout=60
        )
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5002"))
    print("=" * 56)
    print("  Affordplan Sarvam OCR Proxy")
    print(f"  http://localhost:{port}")
    print("  Option C: Sarvam Vision + Sarvam 30B")
    print("=" * 56)
    app.run(debug=False, host="0.0.0.0", port=port)
