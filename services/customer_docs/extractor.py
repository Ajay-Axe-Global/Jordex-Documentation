"""
services/customer_docs/extractor.py — Customer Docs Classifier
===============================================================
Classifies each PDF in a Customer Docs email individually.

Supported document types → Jordex mapping:
  HOUSE BILL OF LADING   → House BL
  MASTER BILL OF LADING  → Master BL
  AGENT INVOICE          → Agent Invoice
  COMMERCIAL INVOICE     → Commercial Invoice
  BOOKING CONFIRMATION   → Booking Confirmation
  PACKING LIST           → Packing List
  DEBIT NOTE             → Additional Files  (comment: "Debit Note")
  ARRIVAL NOTICE         → Additional Files  (comment: "Arrival Notice")
  ADDITIONAL FILES       → Additional Files  (comment: doc_title from extractor)

The Gemini prompt now also extracts doc_title — the header/title text
of the document — which is stored in the classification JSON and used
as the comment field when uploading "Additional Files" to Jordex.
"""

import base64, json, os, re, logging
from datetime import datetime

log = logging.getLogger("customer_docs")


# ══════════════════════════════════════════════════════════════════════
#  KNOWN SCAC PREFIXES (MBL confirmation)
# ══════════════════════════════════════════════════════════════════════

_KNOWN_SCAC_PREFIXES = {
    "HLCU", "MAEU", "MRKU", "MSCU", "MEDU", "ONEY", "YMLU", "EGLV",
    "COSU", "OOLU", "ZIMU", "CMDU", "HDMU", "PCIU", "WHLC", "SUDU",
    "COEU",
}


# ══════════════════════════════════════════════════════════════════════
#  CLASSIFICATION PROMPT
# ══════════════════════════════════════════════════════════════════════

CUSTOMER_DOC_CLASSIFY_PROMPT = """You are a logistics document classification AI used by a freight forwarder (JORDEX).
Analyze this PDF document. Follow the steps IN ORDER.

=====================================================================
STEP 1 — DETECT NON-BL DOCUMENTS (DO THIS FIRST)
=====================================================================

Read the TITLE, HEADER, and FIRST FEW LINES of the document carefully.

── FREETIME / DEMURRAGE / DETENTION NOTICE ──
If the header/title says:
  "Freetime Notification", "Free Time Notification", "Detention Notice",
  "Demurrage Notice", "D&D Notice", or similar — this is a carrier
  administrative notice, NOT an Arrival Notice.
  CRITICAL: Do NOT classify these as ARRIVAL NOTICE.
  → classify as: "ADDITIONAL FILES"
  Reference number: look for the "Bill of Lading:" field in the document.
  Use that value as reference_number. Do NOT use the carrier's internal
  "HL Reference" or "Reference No" as reference_number.

── BOOKING CONFIRMATION ──
If the header/title says:
  "Booking Confirmation", "Boekingsbevestiging", "Boekingconfirmatie",
  "Booking Advice", "Shipment Booking Confirmation", "SHIPPING ADVISE"
  → classify as: "BOOKING CONFIRMATION"
  Reference number: look for "Uw referentie" / "Your reference" / OI number
  (pattern OI followed by digits, e.g. OI2615762). Use that as reference_number.

── DEBIT NOTE ──
If the header says "DEBIT NOTE", "D/N", or "DEBIT ADVICE":
  → classify as: "DEBIT NOTE"

── INVOICE ──
If the header says "INVOICE", "TAX INVOICE", "FREIGHT INVOICE", "COMMERCIAL INVOICE":
  → If "JORDEX" appears ANYWHERE on the invoice → "AGENT INVOICE"
  → If "JORDEX" does NOT appear → "COMMERCIAL INVOICE"
  CRITICAL: NEVER return just "INVOICE".

── PACKING LIST ──
If the header says "PACKING LIST":
  → classify as: "PACKING LIST"

── ARRIVAL NOTICE ──
Only use this if the header/title EXPLICITLY says "ARRIVAL NOTICE" or
"NOTICE OF ARRIVAL" AND the document's PRIMARY PURPOSE is announcing an
ETA / vessel arrival.
  → classify as: "ARRIVAL NOTICE"



─ CARGO MANIFEST / SHIPPING MANIFEST ──
CRITICAL: A CARGO MANIFEST is NOT a Bill of Lading, even though it has
Shipper, Consignee, MBL number, Vessel, Port of Loading, and Port of Discharge fields.
If the header/title says ANY of:
  "CARGO MANIFEST", "SHIPPING MANIFEST", "FREIGHT MANIFEST",
  "MANIFEST", "OCEAN FREIGHT (EXPORT)", "OCEAN FREIGHT (IMPORT)"
  → classify as: "ADDITIONAL FILES"
  doc_title: use the exact header (e.g. "Cargo Manifest")
  reference_number: extract the MB/L number if present.
  Do NOT proceed to Step 2. This is NOT a BL.
 
── CERTIFICATE OF ORIGIN ──
If the header says "CERTIFICATE OF ORIGIN", "C/O", "GSP FORM A":
  → classify as: "ADDITIONAL FILES"
  doc_title: "Certificate of Origin"
 
── INSURANCE CERTIFICATE ──
If the header says "INSURANCE CERTIFICATE", "CARGO INSURANCE":
  → classify as: "ADDITIONAL FILES"
  doc_title: use the exact header text.
 
── WEIGHT CERTIFICATE / INSPECTION ──
If the header says "WEIGHT CERTIFICATE", "INSPECTION CERTIFICATE",
  "SURVEY REPORT", "FUMIGATION CERTIFICATE", "PHYTOSANITARY CERTIFICATE":
  → classify as: "ADDITIONAL FILES"
  doc_title: use the exact header text.

── LOPERSOPDRACHT (COURIER ASSIGNMENT) ──
If the header/title says "LOPERSOPDRACHT" or "LOPERS OPDRACHT":
  This is a Jordex internal courier/runner assignment form.
  → classify as: "ADDITIONAL FILES"
  reference_number: Extract the "Referentienummer" field value (always an OI number
    like OI2619414). This is the PRIMARY reference — do NOT use the B/L number.
  doc_title: "Lopersopdracht"  (always this exact word, nothing else)
  CRITICAL: The OI number from "Referentienummer" takes absolute priority over
  any B/L number on this document.

IF ANY of the above non-BL indicators are found, do NOT proceed to Step 2.



=====================================================================
STEP 2 — BL DOCUMENT: CLASSIFY AS MBL OR HBL
=====================================================================

Only proceed here if NONE of the Step 1 indicators were found.

A document IS a Bill of Lading if it has:
  - A dedicated Shipper box with company name/address
  - A dedicated Consignee box with company name/address
  - A B/L No., Sea Waybill No., or Bill of Lading No. field
  - Vessel/Voyage, Port of Loading, Port of Discharge fields
  - Header/title containing "BILL OF LADING", "WAYBILL", or "B/L"

IMPORTANT: "SEA WAYBILL" IS a type of Bill of Lading. Do NOT skip it.

RULE A — MASTER BILL OF LADING (MBL):
  The CONSIGNEE box MUST contain "JORDEX" (any variation) as the primary consignee.
  → doc_type = "MASTER BILL OF LADING"

RULE B — HOUSE BILL OF LADING (HBL):
  The CONSIGNEE box is any OTHER company (NOT JORDEX).
  → doc_type = "HOUSE BILL OF LADING"

CRITICAL CONSIGNEE RULES:
  - "FOR DELIVERY, PLEASE APPLY TO" is NOT the consignee.
  - JORDEX in Notify Party, Delivery Agent, or anywhere else does NOT count as the Consignee.
  - ONLY the CONSIGNEE box determines MBL vs HBL.
  - EXCEPTION FOR LOGOS/CARRIERS: A prominent carrier/NVOCC logo (e.g. BEE LOGISTICS, Hapag-Lloyd, ZIM, etc.) at the top does NOT make it an MBL. If the Consignee is NOT Jordex, you MUST classify it as a HOUSE BILL OF LADING, regardless of the logo.
  - EXCEPTION FOR FORWARDERS: If the document explicitly says it is issued by a Freight Forwarder (e.g. "MRF INTERNATIONAL FORWARDING", "KUEHNE+NAGEL", "FIATA"), it is ALWAYS a HOUSE BILL OF LADING. Master Bills are ONLY issued by actual ocean carriers (MSC, Maersk, etc).

=====================================================================
STEP 3 — EXTRACT REFERENCE NUMBER AND DOC TITLE
=====================================================================

PRIORITY 1 — Bill of Lading number:
  Look for: "Bill of Lading:", "B/L No.", "BL No.", "Sea Waybill No."
  Use THIS value as reference_number.
  CRITICAL: If the document shows a B/L number WITHOUT the 4-letter SCAC prefix, you MUST prepend the correct 4-letter prefix based on the carrier logo or name on the page.
  Examples of SCAC codes: Hapag-Lloyd = HLCU, MSC = MEDU, ONE = ONEY, OOCL = OOLU, HMM / Hyundai = HDMU, CMA CGM = CMDU.
  For example, if carrier is HMM and BL is "SELE33195000" -> return "HDMUSELE33195000".
  Do NOT use "HL Reference", "Reference No", "Our Ref" — those are internal IDs.

PRIORITY 2 — Booking/OI reference:
  For BOOKING CONFIRMATION: look for "Uw referentie", "Your reference", OI pattern.

PRIORITY 3 — Container number (fallback):
  4 uppercase letters + 6-7 digits (e.g. MRSU6620410).

DOC TITLE:
  Extract the main title/header text from the top of the document.
  Examples: "DEBIT NOTE", "FREETIME NOTIFICATION", "CERTIFICATE OF ORIGIN",
  "CMR CONSIGNMENT NOTE", "ARRIVAL NOTICE", "PACKING LIST".
  This is used as a comment when uploading to Jordex.
  For BL documents, set doc_title to "Bill of Lading" or the exact header text.

=====================================================================
OUTPUT — Return ONLY valid JSON. No markdown. No backticks.
=====================================================================

{
  "doc_type": "HOUSE BILL OF LADING" or "MASTER BILL OF LADING" or "COMMERCIAL INVOICE" or "AGENT INVOICE" or "DEBIT NOTE" or "PACKING LIST" or "ARRIVAL NOTICE" or "BOOKING CONFIRMATION" or "ADDITIONAL FILES",
  "reference_number": "BL number / booking ref / invoice number or null",
  "container_no": "first container number or null",
  "doc_title": "the main header/title text from the document, or null",
  "confidence": "high" or "medium" or "low"
}

CRITICAL:
- "ADDITIONAL FILES" is the fallback — use it when you truly cannot identify the doc type.
- NEVER return "UNKNOWN". Always classify.
- NEVER return just "INVOICE" — always resolve to AGENT INVOICE or COMMERCIAL INVOICE.
- Freetime/Detention/Demurrage notices are ADDITIONAL FILES, NOT ARRIVAL NOTICE.
- For ADDITIONAL FILES: still extract the Bill of Lading reference_number if visible.
- Always extract doc_title regardless of doc_type."""


# ══════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════

def _extract_text(pdf_path: str) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception:
        pass
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(pdf_path)
        return "\n".join(p.extract_text() or "" for p in reader.pages)
    except Exception:
        pass
    return ""


def _keyword_fallback(pdf_path: str) -> dict:
    text = _extract_text(pdf_path)
    text_upper = text.upper() if text else ""

    doc_type = "ADDITIONAL FILES"
    reference_number = None
    container_no = None
    doc_title = None

    if text_upper:
        # Extract doc_title from first non-empty line
        for line in text.split('\n'):
            stripped = line.strip()
            if stripped and len(stripped) > 2:
                doc_title = stripped[:100]
                break

        # Non-BL checks
        if re.search(r'\bLOPERSOPDRACHT\b|\bLOPERS\s+OPDRACHT\b', text_upper):
            doc_type = "ADDITIONAL FILES"
            doc_title = "Lopersopdracht"
            # OI from Referentienummer is primary ref
            oi_match = re.search(r'(?:Referentienummer|Referentie)\s*[:\s]*(OI\d{4,})', text, re.IGNORECASE)
            if oi_match:
                reference_number = oi_match.group(1).upper()
        elif re.search(r'\bDEBIT\s+(?:NOTE|ADVICE)\b|(?:^|\s)D/N\b', text_upper):
            doc_type = "DEBIT NOTE"
            doc_title = "Debit Note"
        elif re.search(r'\bPACKING\s+LIST\b', text_upper):
            doc_type = "PACKING LIST"
            doc_title = "Packing List"
        elif re.search(r'\bARRIVAL\s+NOTICE\b|\bNOTICE\s+OF\s+ARRIVAL\b', text_upper):
            doc_type = "ARRIVAL NOTICE"
            doc_title = "Arrival Notice"
        elif re.search(r'\bBOOKING\s+CONFIRM', text_upper) or re.search(r'\bBOEKINGS', text_upper):
            doc_type = "BOOKING CONFIRMATION"
            doc_title = "Booking Confirmation"
        elif re.search(r'\bINVOICE\b|\bTAX\s+INVOICE\b|\bFREIGHT\s+INVOICE\b', text_upper):
            doc_type = "AGENT INVOICE" if "JORDEX" in text_upper else "COMMERCIAL INVOICE"
        elif re.search(r'\bBILL\s+OF\s+LADING\b|\bSEA\s+WAYBILL\b|\bB/L\b', text_upper):
            consignee_match = re.search(
                r'CONSIGNEE[:\s]+(.{0,200}?)(?:\n[A-Z]{3,}|\Z)', text_upper, re.DOTALL
            )
            if consignee_match and "JORDEX" in consignee_match.group(1):
                doc_type = "MASTER BILL OF LADING"
            elif "JORDEX" in text_upper:
                doc_type = "MASTER BILL OF LADING"
            else:
                doc_type = "HOUSE BILL OF LADING"

        # Reference number
        ref_match = re.search(
            r'(?:B/L\s*No\.?|BL\s*No\.?|Sea\s+Waybill\s+No\.?|Waybill\s+No\.?)[:\s]*([A-Z0-9]{6,20})',
            text, re.IGNORECASE
        )
        if ref_match:
            reference_number = ref_match.group(1).strip().upper()

        # Container number
        cont_match = re.search(r'\b([A-Z]{4})(\d{7})\b', text_upper)
        if cont_match:
            container_no = cont_match.group(1) + cont_match.group(2)

    return {
        "doc_type": doc_type,
        "reference_number": reference_number,
        "container_no": container_no,
        "doc_title": doc_title,
        "confidence": "low",
    }


def _resolve_folder_name(doc_type: str, reference_number: str, container_no: str) -> str | None:
    if reference_number:
        ref = re.sub(r'\s+', '', reference_number).upper()
        if len(ref) >= 6:
            if doc_type in ("ADDITIONAL FILES", "BOOKING CONFIRMATION"):
                if (re.match(r'^[A-Z]{4}', ref) and not ref.isdigit()) or re.match(r'^OI\d{4,}', ref):
                    return ref
            else:
                return ref
    if container_no:
        c = re.sub(r'\s+', '', container_no).upper()
        if re.fullmatch(r'[A-Z]{4}\d{7}', c):
            return c
    return None


# ══════════════════════════════════════════════════════════════════════
#  DOC TYPE NORMALISATION
# ══════════════════════════════════════════════════════════════════════

_VALID_TYPES = {
    "HOUSE BILL OF LADING", "MASTER BILL OF LADING",
    "COMMERCIAL INVOICE", "AGENT INVOICE",
    "DEBIT NOTE", "PACKING LIST", "ARRIVAL NOTICE",
    "BOOKING CONFIRMATION", "ADDITIONAL FILES",
}

_NORMALISE = {
    "LOPERSOPDRACHT": "ADDITIONAL FILES",
    "LOPERS OPDRACHT": "ADDITIONAL FILES",
    "HBL": "HOUSE BILL OF LADING",
    "MBL": "MASTER BILL OF LADING",
    "HOUSE BL": "HOUSE BILL OF LADING",
    "MASTER BL": "MASTER BILL OF LADING",
    "SEA WAYBILL": "HOUSE BILL OF LADING",
    "INVOICE": "COMMERCIAL INVOICE",
    "DEBIT ADVICE": "DEBIT NOTE",
    "D/N": "DEBIT NOTE",
    "CREDIT NOTE": "ADDITIONAL FILES",
    "PRE-ALERT": "ADDITIONAL FILES",
    "PRE ALERT": "ADDITIONAL FILES",
    "CERTIFICATE OF ORIGIN": "ADDITIONAL FILES",
    "FREETIME NOTIFICATION": "ADDITIONAL FILES",
    "FREE TIME NOTIFICATION": "ADDITIONAL FILES",
    "DETENTION NOTICE": "ADDITIONAL FILES",
    "DEMURRAGE NOTICE": "ADDITIONAL FILES",
    "SHIPPING ADVISE": "BOOKING CONFIRMATION",
    "BOOKING ADVICE": "BOOKING CONFIRMATION",
    "UNKNOWN": "ADDITIONAL FILES",
}

def _simplify_doc_title(title: str) -> str:
    """Strip reference numbers, long suffixes from doc_title for cleaner Jordex comments."""
    if not title:
        return title
    # Remove trailing reference numbers / codes (e.g. "Gas Insurance Certificate No 56G654")
    cleaned = re.sub(r'\s*(?:No\.?|Nr\.?|Ref\.?|#)\s*[A-Z0-9\-/]{3,}.*$', '', title, flags=re.IGNORECASE).strip()
    # Cap length at 60 chars
    if len(cleaned) > 60:
        cleaned = cleaned[:60].rsplit(' ', 1)[0]
    return cleaned or title

# ══════════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════

def classify_customer_doc(pdf_path: str, gemini_model=None) -> dict:
    """
    Classify a single Customer Docs PDF.

    Returns:
      {
        "doc_type":          "HOUSE BILL OF LADING" etc.,
        "reference_number":  "HLCUSZX2605APPZ0" or null,
        "container_no":      "BEAU2199630" or null,
        "doc_title":         "DEBIT NOTE" or "FREETIME NOTIFICATION" or null,
        "folder_name":       "HLCUSZX2605APPZ0" or null,
        "source_file":       "filename.pdf",
        "extracted_at":      "2026-07-16T13:00:00",
        "flag":              null | "needs_manual_review" | "low_confidence",
      }
    """
    result = {
        "doc_type": "ADDITIONAL FILES",
        "reference_number": None,
        "container_no": None,
        "doc_title": None,
        "folder_name": None,
        "source_file": os.path.basename(pdf_path),
        "extracted_at": datetime.now().isoformat(),
        "flag": None,
    }

    # ── Gemini path ──────────────────────────────────────────────────
    if gemini_model is not None:
        try:
            ext = os.path.splitext(pdf_path)[1].lower()
            mime_type = "application/pdf"
            if ext in (".jpg", ".jpeg"):
                mime_type = "image/jpeg"
            elif ext == ".png":
                mime_type = "image/png"

            with open(pdf_path, "rb") as f:
                doc_bytes = f.read()

            resp = gemini_model.generate_content(
                [
                    {"mime_type": mime_type,
                     "data": base64.b64encode(doc_bytes).decode()},
                    CUSTOMER_DOC_CLASSIFY_PROMPT,
                ],
                generation_config={"temperature": 0.0, "max_output_tokens": 300},
            )

            raw = resp.text.strip()
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
            parsed = json.loads(raw)

            doc_type = (parsed.get("doc_type") or "ADDITIONAL FILES").strip().upper()
            reference_number = (parsed.get("reference_number") or "").strip().upper() or None
            
            # Normalise common OCR error: "01" instead of "OI"
            if reference_number and reference_number.startswith("01") and len(reference_number) >= 7:
                reference_number = "OI" + reference_number[2:]
                
            container_no = (parsed.get("container_no") or "").strip().upper() or None
            doc_title = (parsed.get("doc_title") or "").strip() or None
            confidence = (parsed.get("confidence") or "high").strip().lower()

            log.info(
                "  Customer Doc Gemini: %s → type=%s ref=%s title=%s conf=%s",
                os.path.basename(pdf_path), doc_type, reference_number, doc_title, confidence,
            )

        except json.JSONDecodeError as e:
            log.warning("  Customer Doc JSON parse failed: %s — keyword fallback", e)
            fb = _keyword_fallback(pdf_path)
            doc_type, reference_number = fb["doc_type"], fb["reference_number"]
            container_no, doc_title = fb["container_no"], fb["doc_title"]
            confidence = "low"

        except Exception as e:
            log.warning("  Customer Doc Gemini failed: %s — keyword fallback", e)
            fb = _keyword_fallback(pdf_path)
            doc_type, reference_number = fb["doc_type"], fb["reference_number"]
            container_no, doc_title = fb["container_no"], fb["doc_title"]
            confidence = "low"

    # ── No Gemini ────────────────────────────────────────────────────
    else:
        fb = _keyword_fallback(pdf_path)
        doc_type, reference_number = fb["doc_type"], fb["reference_number"]
        container_no, doc_title = fb["container_no"], fb["doc_title"]
        confidence = "low"

    # ── Normalise doc_type ───────────────────────────────────────────
    # ── Lopersopdracht: force OI as reference, strip verbose title ───
    if doc_title and re.search(r'\bLOPERSOPDRACHT\b', (doc_title or '').upper()):
        doc_type = "ADDITIONAL FILES"
        doc_title = "Lopersopdracht"
        # If Gemini returned B/L as reference but OI exists, prefer OI
        if reference_number and not reference_number.startswith("OI"):
            text = _extract_text(pdf_path)
            oi_m = re.search(r'(?:Referentienummer|Referentie)\s*[:\s]*(OI\d{4,})', text, re.IGNORECASE)
            if oi_m:
                reference_number = oi_m.group(1).upper()

    # ── Normalise doc_type ───────────────────────────────────────────
    doc_type = _NORMALISE.get(doc_type, doc_type)
    if doc_type not in _VALID_TYPES:
        log.warning("  Unrecognised doc_type '%s' → ADDITIONAL FILES", doc_type)
        doc_type = "ADDITIONAL FILES"

    # ── Secondary MBL check via SCAC prefix ──────────────────────────
    if doc_type == "HOUSE BILL OF LADING" and reference_number:
        prefix4 = reference_number[:4].upper()
        if prefix4 in _KNOWN_SCAC_PREFIXES:
            log.info("  SCAC prefix '%s' → upgrading HBL → MBL", prefix4)
            doc_type = "MASTER BILL OF LADING"

    # ── Build result ─────────────────────────────────────────────────
    result["doc_type"] = doc_type
    result["reference_number"] = reference_number
    result["container_no"] = container_no
    result["doc_title"] = _simplify_doc_title(doc_title)
    result["folder_name"] = _resolve_folder_name(doc_type, reference_number, container_no)

    if confidence == "low":
        result["flag"] = "low_confidence"
    elif doc_type == "ADDITIONAL FILES":
        result["flag"] = "needs_manual_review"

    return result


def classify_all_customer_docs(pdf_paths: list, gemini_model=None, subject: str = None) -> list:
    """
    Classify all PDFs from one Customer Docs email.
    Returns list of result dicts with shared_folder_name propagated.
    """
    results = []
    for pdf_path in pdf_paths:
        r = classify_customer_doc(pdf_path, gemini_model)
        results.append(r)

    # Resolve shared folder name: strict priority sequence
    folder_name = None

    # Priority 1: OI reference from subject
    if subject:
        import re
        m = re.search(r'(OI\d{4,})', subject, re.IGNORECASE)
        if m:
            folder_name = m.group(1).upper()

    # Priority 2: Master BL reference number
    if not folder_name:
        for r in results:
            if r["doc_type"] == "MASTER BILL OF LADING" and r.get("reference_number"):
                folder_name = r["reference_number"]
                break

    # Priority 3: Master BL container number
    if not folder_name:
        for r in results:
            if r["doc_type"] == "MASTER BILL OF LADING" and r.get("container_no"):
                folder_name = r["container_no"]
                break

    # Fallback 1: Any HBL reference number
    if not folder_name:
        for r in results:
            if r["doc_type"] == "HOUSE BILL OF LADING" and r.get("reference_number"):
                folder_name = r["reference_number"]
                break

    # Fallback 2: Any reference from any document
    if not folder_name:
        for r in results:
            if r.get("folder_name"):
                folder_name = r["folder_name"]
                break

    for r in results:
        r["shared_folder_name"] = folder_name

    return results