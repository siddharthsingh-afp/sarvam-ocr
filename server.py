"""
Affordplan — Sarvam OCR + Rule-based Extraction
100% India servers. No external AI. DPDP compliant.

Sarvam Doc AI reads the document → Python extracts fields from the text
"""

import os, re, json, time, zipfile, io
from pathlib import Path
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests as req_lib

app = Flask(__name__)
CORS(app)

SARVAM_DOC_KEY = os.environ.get("SARVAM_DOC_KEY", "")
SARVAM_BASE    = "https://api.sarvam.ai/doc-digitization/job/v1"
ALLOWED        = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}


def sh():
    return {"Content-Type": "application/json", "api-subscription-key": SARVAM_DOC_KEY}


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


def extract_from_zip(content_bytes):
    try:
        z = zipfile.ZipFile(io.BytesIO(content_bytes))
        parts = []
        for name in z.namelist():
            raw = z.read(name).decode("utf-8", errors="ignore")
            parts.append(html_to_text(raw) if name.endswith(".html") else raw)
        return "\n".join(parts).strip()
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Rule-based field extraction — pure Python, no AI, no external calls
# ─────────────────────────────────────────────────────────────────────────────
def extract_fields(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    full  = text

    def find(patterns, default=None):
        for pat in patterns:
            m = re.search(pat, full, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                val = re.sub(r"\s+", " ", val)
                if val:
                    return val
        return default

    # ── Patient name ──────────────────────────────────────────────────────────
    patient_name = find([
        r"(?:patient(?:\s*name)?|name)\s*[:\-]\s*([A-Za-z][A-Za-z\s\.]{2,40})",
        r"(?:Mrs?\.?|Ms\.?|Mr\.?|Dr\.?)\s+([A-Za-z][A-Za-z\s\.]{2,35})",
        r"Name\s*:\s*([A-Za-z][A-Za-z\s\.]{2,35})",
    ])

    # ── Doctor name ───────────────────────────────────────────────────────────
    doctor_name = find([
        r"Dr\.?\s+([A-Za-z][A-Za-z\s\.]{2,40})",
        r"(?:doctor|physician|consultant)\s*[:\-]\s*([A-Za-z][A-Za-z\s\.]{2,40})",
    ])

    # ── Hospital / clinic name ────────────────────────────────────────────────
    hospital_name = find([
        r"([A-Za-z][A-Za-z\s&\-\.]{3,50}(?:hospital|clinic|centre|center|medical|health|care|labs?|diagnostics?))",
        r"(?:hospital|clinic|centre|center)\s*[:\-]\s*([A-Za-z][A-Za-z\s]{2,50})",
    ])

    # ── Date ──────────────────────────────────────────────────────────────────
    date = find([
        r"[Dd]ate\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
        r"(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
        r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4})",
    ])

    # ── UHID ──────────────────────────────────────────────────────────────────
    patient_uhid = find([
        r"(?:UHID|ABHA|MRN|Patient\s*ID|UHN)\s*[:\-]?\s*([A-Za-z0-9\-]{4,20})",
    ])

    # ── Diagnosis ─────────────────────────────────────────────────────────────
    diagnosis = find([
        r"(?:diagnosis|dx|condition|complaints?)\s*[:\-]\s*([A-Za-z][A-Za-z\s\,\/]{3,80})",
    ])

    # ── EDD / LMP ─────────────────────────────────────────────────────────────
    edd = find([
        r"(?:EDD|EDC|due\s*date|expected\s*delivery)\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
        r"(?:EDD|EDC)\s*[:\-]?\s*(\d{1,2}\s+\w+\s+\d{2,4})",
    ])
    lmp = find([
        r"(?:LMP|last\s*menstrual\s*period)\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
    ])

    # ── Document type ─────────────────────────────────────────────────────────
    doc_type = "other"
    tl = full.lower()
    if any(w in tl for w in ["prescription", "rx", "tab ", "cap ", "syrup", "medicines advised"]):
        doc_type = "prescription"
    elif any(w in tl for w in ["lab report", "test report", "pathology", "haemoglobin", "hemoglobin", "wbc", "rbc", "platelet", "hba1c", "tsh"]):
        doc_type = "lab_report"
    elif any(w in tl for w in ["invoice", "bill", "amount", "total", "payment", "receipt"]):
        doc_type = "bill"
    elif any(w in tl for w in ["x-ray", "mri", "ct scan", "ultrasound", "usg", "sonography", "ecg", "echo"]):
        doc_type = "imaging"
    elif any(w in tl for w in ["discharge", "admitted", "discharge summary"]):
        doc_type = "discharge"

    # ── Medicines ─────────────────────────────────────────────────────────────
    medicines = []
    # Common Indian medicine patterns: Tab/Cap/Syrup/Inj followed by name and dose
    med_patterns = [
        r"(?:Tab|Cap|Tablet|Capsule|Syrup|Inj|Injection|Drops?|Gel|Cream|Oint)\s+([A-Za-z][A-Za-z0-9\s\-\+\.]{1,40}?)(?:\s+(\d+\s*(?:mg|ml|mcg|gm|IU|%)))?(?:\s+([\d\-]+(?:\s*(?:OD|BD|TDS|QID|SOS|HS|AC|PC|1-0-1|1-1-1|0-0-1|1-0-0))?))?",
        r"([A-Za-z][A-Za-z0-9\s\-\+\.]{2,30}?)\s+(\d+\s*(?:mg|ml|mcg|gm|IU|%))\s+((?:\d+-\d+-\d+|OD|BD|TDS|QID|SOS|HS))",
    ]
    seen_meds = set()
    for pat in med_patterns:
        for m in re.finditer(pat, full, re.IGNORECASE):
            name = m.group(1).strip()
            name = re.sub(r"\s+", " ", name)
            if len(name) < 2 or name.lower() in seen_meds:
                continue
            seen_meds.add(name.lower())
            strength = m.group(2).strip() if len(m.groups()) >= 2 and m.group(2) else ""
            frequency = m.group(3).strip() if len(m.groups()) >= 3 and m.group(3) else ""
            medicines.append({
                "name": name,
                "strength": strength,
                "form": "",
                "frequency": frequency,
                "duration": "",
                "quantity": "",
                "confidence": "medium"
            })
        if medicines:
            break

    # ── Lab tests ─────────────────────────────────────────────────────────────
    lab_tests = []
    # Pattern: test name followed by value and unit
    lab_pattern = r"([A-Za-z][A-Za-z\s\(\)\/]{2,40}?)\s*[:\-]?\s*(\d+\.?\d*)\s*(g\/dL|mg\/dL|mmol\/L|U\/L|IU\/L|%|cells\/μL|10\^3\/μL|mIU\/L|ng\/mL|pg\/mL|fl|fmol|mmHg)?"
    for m in re.finditer(lab_pattern, full, re.IGNORECASE):
        name  = m.group(1).strip()
        value = m.group(2).strip()
        unit  = m.group(3).strip() if m.group(3) else ""
        if len(name) < 2 or not value:
            continue
        # Skip lines that are clearly not lab tests
        if any(skip in name.lower() for skip in ["date", "name", "age", "weight", "height", "bp", "address"]):
            continue
        lab_tests.append({
            "name": name,
            "value": value,
            "unit": unit,
            "reference_range": "",
            "flag": "",
            "confidence": "medium"
        })

    # ── Is handwritten ────────────────────────────────────────────────────────
    # Heuristic: if text has many special chars/symbols it's likely handwritten
    special = len(re.findall(r"[⊙⧫ω₁₂⊕●□]", full))
    is_handwritten = special > 2 or len(lines) < 8

    # ── Summary ───────────────────────────────────────────────────────────────
    summary = f"{doc_type.replace('_',' ').title()} document"
    if patient_name:
        summary += f" for {patient_name}"
    if date:
        summary += f" dated {date}"
    if medicines:
        summary += f" with {len(medicines)} medicine(s)"
    if lab_tests:
        summary += f" with {len(lab_tests)} test(s)"

    return {
        "document_type":  doc_type,
        "patient_name":   patient_name,
        "patient_uhid":   patient_uhid,
        "doctor_name":    doctor_name,
        "hospital_name":  hospital_name,
        "date":           date,
        "diagnosis":      diagnosis,
        "is_handwritten": is_handwritten,
        "summary":        summary,
        "edd":            edd,
        "lmp":            lmp,
        "medicines":      medicines,
        "lab_tests":      lab_tests,
        "raw_text":       text,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Sarvam Doc AI pipeline
# ─────────────────────────────────────────────────────────────────────────────
def run_doc_ai(file_bytes, filename, mime):
    r1 = req_lib.post(SARVAM_BASE, headers=sh(),
                      json={"job_parameters": {"language": "en-IN", "output_format": "html"}},
                      timeout=30)
    if not r1.ok:
        return None, f"Create job failed: {r1.status_code}"

    job_id = r1.json().get("job_id") or r1.json().get("id")
    if not job_id:
        return None, "No job_id"

    r2 = req_lib.post(f"{SARVAM_BASE}/upload-files", headers=sh(),
                      json={"job_id": job_id, "files": [filename]}, timeout=30)
    if not r2.ok:
        return None, f"Register failed: {r2.status_code}"

    upload_urls = r2.json().get("upload_urls") or {}
    entry = upload_urls.get(filename) or (list(upload_urls.values())[0] if upload_urls else None)
    upload_url = (entry.get("file_url") or "") if isinstance(entry, dict) else (entry or "")
    if not upload_url:
        return None, "No upload URL"

    r3 = req_lib.put(upload_url,
                     headers={"x-ms-blob-type": "BlockBlob", "Content-Type": mime},
                     data=file_bytes, timeout=60)
    if r3.status_code not in (200, 201):
        return None, f"Upload failed: {r3.status_code}"

    req_lib.post(f"{SARVAM_BASE}/{job_id}/start",
                 headers={**sh(), "X-Dashboard": "true"}, timeout=30)

    for _ in range(40):
        time.sleep(2)
        r5 = req_lib.get(f"{SARVAM_BASE}/{job_id}/status",
                         headers={"api-subscription-key": SARVAM_DOC_KEY}, timeout=30)
        if not r5.ok:
            continue
        state = (r5.json().get("job_state") or "").lower()
        if "complet" in state or "success" in state:
            break
        if "fail" in state or "error" in state:
            return None, f"Job failed: {state}"
    else:
        return None, "Timed out"

    r6 = req_lib.post(f"{SARVAM_BASE}/{job_id}/download-files",
                      headers=sh(), json={"files": ["document.zip"]}, timeout=30)
    if not r6.ok:
        return None, "Download failed"

    dl_urls = r6.json().get("download_urls") or {}
    entry6  = dl_urls.get("document.zip") or (list(dl_urls.values())[0] if dl_urls else None)
    dl_url  = (entry6.get("file_url") or "") if isinstance(entry6, dict) else (entry6 or "")
    if not dl_url:
        return None, "No download URL"

    rd = req_lib.get(dl_url, timeout=60)
    if not rd.ok:
        return None, "Fetch failed"

    text = extract_from_zip(rd.content)
    if not text:
        text = html_to_text(rd.content.decode("utf-8", errors="ignore"))
    if not text:
        return None, "No text extracted"

    return text, None


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "affordplan-sarvam-ocr",
                    "key_set": bool(SARVAM_DOC_KEY)})


@app.route("/compare", methods=["POST"])
def compare():
    try:
        if not SARVAM_DOC_KEY:
            return jsonify({"error": "SARVAM_DOC_KEY not set"}), 500
        if "image" not in request.files:
            return jsonify({"error": "No image"}), 400

        f        = request.files["image"]
        filename = f.filename or "document.jpg"
        suffix   = os.path.splitext(filename)[1].lower()
        if suffix not in ALLOWED:
            return jsonify({"error": f"Unsupported format '{suffix}'"}), 400

        mime = f.content_type or "image/jpeg"
        text, err = run_doc_ai(f.read(), filename, mime)
        if err:
            return jsonify({"error": err}), 500

        result = extract_fields(text)
        return jsonify({"sarvam": result})

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
