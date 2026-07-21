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

SARVAM_DOC_KEY = os.environ.get("SARVAM_DOC_KEY") or os.environ.get("SARVAM_API_KEY", "")
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
    """Unzip Sarvam output — returns both text and structured blocks"""
    try:
        z = zipfile.ZipFile(io.BytesIO(content_bytes))
        text_parts = []
        blocks = []
        for name in z.namelist():
            raw = z.read(name).decode("utf-8", errors="ignore")
            if name.endswith(".json"):
                try:
                    data = json.loads(raw)
                    if "blocks" in data:
                        blocks = data["blocks"]
                except Exception:
                    pass
            elif name.endswith(".html"):
                text_parts.append(html_to_text(raw))
            elif name.endswith((".md", ".txt")):
                text_parts.append(raw)
        return "\n".join(text_parts).strip(), blocks
    except Exception:
        return "", []


# ─────────────────────────────────────────────────────────────────────────────
# Rule-based field extraction using Sarvam's structured blocks
# ─────────────────────────────────────────────────────────────────────────────
def extract_fields(text, blocks):

    # Build full text from blocks in reading order
    block_texts = [b.get("text","") for b in sorted(blocks, key=lambda b: b.get("reading_order",0))]
    full = text or "\n".join(block_texts)

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
        r"patient\s*[Nn]ame\s*[:\-]\s*((?:Mrs?\.?|Ms\.?|Mr\.?)\s*[A-Z][A-Za-z\s\.]{2,40})",
        r"patient\s*[Nn]ame\s*[:\-]\s*([A-Z][A-Za-z\s\.]{2,40})",
        r"(?:Mrs?\.?|Ms\.?)\s+([A-Z][A-Z\s]{2,30})",
        r"Name\s*:\s*([A-Z][A-Za-z\s\.]{2,35})",
    ])

    # ── Doctor name ───────────────────────────────────────────────────────────
    doctor_name = find([
        r"[Dd]octor\s*[Nn]ame\s*[:\-]\s*(Dr\.?\s*[A-Z][A-Za-z\s\.]{2,40})",
        r"[Ss]igned\s*by\s*:\s*(Dr\.?\s*[A-Z][A-Za-z\s\.]{2,40})",
        r"Dr\.?\s+([A-Z][A-Z\s\.]{2,35})",
    ])

    # ── Hospital name ─────────────────────────────────────────────────────────
    hospital_name = find([
        r"([A-Za-z][A-Za-z\s&\-\.]{3,60}(?:[Hh]ospital|[Cc]linic|[Cc]entre|[Cc]enter|[Mm]edical|[Hh]ealth))",
    ])

    # ── Date ──────────────────────────────────────────────────────────────────
    date = find([
        r"[Bb]ill\s*[Dd]ate\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
        r"[Dd]ate\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
        r"(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{4})",
    ])

    # ── UHID ──────────────────────────────────────────────────────────────────
    patient_uhid = find([
        r"(?:UHID|ABHA|MRN|Patient\s*ID|UHN)\s*[:\-]?\s*([A-Za-z0-9\-]{4,20})",
        r":\s*(HH\d{8,})",  # Hiranandani style: HH01.24022551
    ])

    # ── Diagnosis ─────────────────────────────────────────────────────────────
    diagnosis = find([
        r"(?:diagnosis|dx|condition|complaints?)\s*[:\-]\s*([A-Za-z][A-Za-z\s\,\/]{3,80})",
    ])

    # ── EDD / LMP ─────────────────────────────────────────────────────────────
    edd = find([
        r"(?:EDD|EDC|due\s*date|expected\s*delivery)\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
    ])
    lmp = find([
        r"(?:LMP|last\s*menstrual\s*period)\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
    ])

    # ── Document type ─────────────────────────────────────────────────────────
    doc_type = "other"
    tl = full.lower()
    if any(w in tl for w in ["prescription", "drug name", "tab ", "cap ", "syrup", "oral", "topical"]):
        doc_type = "prescription"
    elif any(w in tl for w in ["lab report", "pathology", "haemoglobin", "hemoglobin", "wbc", "rbc", "hba1c", "tsh"]):
        doc_type = "lab_report"
    elif any(w in tl for w in ["invoice", "bill amount", "total amount", "payment"]):
        doc_type = "bill"
    elif any(w in tl for w in ["x-ray", "mri", "ct scan", "ultrasound", "usg", "sonography"]):
        doc_type = "imaging"
    elif any(w in tl for w in ["discharge summary", "date of discharge", "date of admission"]):
        doc_type = "discharge"

    # ── Medicines from table blocks ───────────────────────────────────────────
    medicines = []
    for block in blocks:
        if block.get("layout_tag") == "table":
            table_html = block.get("text", "")
            # Parse table rows
            rows = re.findall(r"<tr>(.*?)</tr>", table_html, re.DOTALL)
            for row in rows:
                cells = re.findall(r"<td>(.*?)</td>", row, re.DOTALL)
                if len(cells) < 2:
                    continue
                # Clean each cell
                cells = [re.sub(r"<[^>]+>", " ", c).strip() for c in cells]
                cells = [re.sub(r"\s+", " ", c) for c in cells]
                drug_name = cells[1] if len(cells) > 1 else ""
                if not drug_name or len(drug_name) < 2:
                    continue
                route     = cells[2] if len(cells) > 2 else ""
                dose      = cells[3] if len(cells) > 3 else ""
                frequency = cells[4] if len(cells) > 4 else ""
                duration  = cells[5] if len(cells) > 5 else ""
                remarks   = cells[6] if len(cells) > 6 else ""
                # Extract strength from drug name
                strength_m = re.search(r"(\d+\s*(?:mg|ml|mcg|gm|IU|%))", drug_name, re.IGNORECASE)
                strength = strength_m.group(1) if strength_m else dose
                medicines.append({
                    "name":       drug_name,
                    "strength":   strength,
                    "form":       route,
                    "frequency":  frequency,
                    "duration":   duration,
                    "quantity":   "",
                    "remarks":    remarks,
                    "confidence": "high"
                })

    # Fallback medicine extraction if no table found
    if not medicines:
        med_pattern = r"(?:Tab|Cap|Tablet|Capsule|Syrup|Inj|Injection|Gel|Cream)\.?\s+([A-Za-z][A-Za-z0-9\s\-\+\.]{1,40}?)(?:\s+(\d+\s*(?:mg|ml|mcg|gm|IU|%)))?"
        seen = set()
        for m in re.finditer(med_pattern, full, re.IGNORECASE):
            name = m.group(1).strip()
            if name.lower() in seen or len(name) < 2:
                continue
            seen.add(name.lower())
            medicines.append({
                "name": name,
                "strength": m.group(2).strip() if m.group(2) else "",
                "form": "", "frequency": "", "duration": "",
                "quantity": "", "confidence": "medium"
            })

    # ── Lab tests from table blocks ───────────────────────────────────────────
    lab_tests = []
    for block in blocks:
        if block.get("layout_tag") == "table" and doc_type == "lab_report":
            table_html = block.get("text", "")
            rows = re.findall(r"<tr>(.*?)</tr>", table_html, re.DOTALL)
            for row in rows:
                cells = re.findall(r"<td>(.*?)</td>", row, re.DOTALL)
                if len(cells) < 2:
                    continue
                cells = [re.sub(r"<[^>]+>", " ", c).strip() for c in cells]
                name  = cells[0]
                value = cells[1] if len(cells) > 1 else ""
                unit  = cells[2] if len(cells) > 2 else ""
                ref   = cells[3] if len(cells) > 3 else ""
                flag  = cells[4] if len(cells) > 4 else ""
                if not name or not value:
                    continue
                lab_tests.append({
                    "name": name, "value": value, "unit": unit,
                    "reference_range": ref, "flag": flag, "confidence": "high"
                })

    # ── Is handwritten ────────────────────────────────────────────────────────
    # If Sarvam found table blocks with high confidence, it's printed
    high_conf_blocks = [b for b in blocks if b.get("confidence", 0) > 0.7]
    is_handwritten = len(high_conf_blocks) == 0

    # ── Summary ───────────────────────────────────────────────────────────────
    summary = f"{doc_type.replace('_',' ').title()} document"
    if patient_name:
        summary += f" for {patient_name}"
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
        return None, None, f"Create job failed: {r1.status_code}"

    job_id = r1.json().get("job_id") or r1.json().get("id")
    if not job_id:
        return None, None, "No job_id"

    r2 = req_lib.post(f"{SARVAM_BASE}/upload-files", headers=sh(),
                      json={"job_id": job_id, "files": [filename]}, timeout=30)
    if not r2.ok:
        return None, None, f"Register failed: {r2.status_code}"

    upload_urls = r2.json().get("upload_urls") or {}
    entry = upload_urls.get(filename) or (list(upload_urls.values())[0] if upload_urls else None)
    upload_url = (entry.get("file_url") or "") if isinstance(entry, dict) else (entry or "")
    if not upload_url:
        return None, None, "No upload URL"

    r3 = req_lib.put(upload_url,
                     headers={"x-ms-blob-type": "BlockBlob", "Content-Type": mime},
                     data=file_bytes, timeout=60)
    if r3.status_code not in (200, 201):
        return None, None, f"Upload failed: {r3.status_code}"

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
            return None, None, f"Job failed: {state}"
    else:
        return None, None, "Timed out"

    r6 = req_lib.post(f"{SARVAM_BASE}/{job_id}/download-files",
                      headers=sh(), json={"files": ["document.zip"]}, timeout=30)
    if not r6.ok:
        return None, None, "Download failed"

    dl_urls = r6.json().get("download_urls") or {}
    entry6  = dl_urls.get("document.zip") or (list(dl_urls.values())[0] if dl_urls else None)
    dl_url  = (entry6.get("file_url") or "") if isinstance(entry6, dict) else (entry6 or "")
    if not dl_url:
        return None, None, "No download URL"

    rd = req_lib.get(dl_url, timeout=60)
    if not rd.ok:
        return None, None, "Fetch failed"

    text, blocks = extract_from_zip(rd.content)
    if not text:
        text = html_to_text(rd.content.decode("utf-8", errors="ignore"))
    if not text:
        return None, None, "No text extracted"

    return text, blocks, None


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
        text, blocks, err = run_doc_ai(f.read(), filename, mime)
        if err:
            return jsonify({"error": err}), 500

        result = extract_fields(text, blocks)
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
