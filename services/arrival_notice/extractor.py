"""
services/arrival_notice/extractor.py — Arrival Notice Extraction Logic
=======================================================================
Contains:
  - ARRIVAL_NOTICE_PROMPT  (AN-specific Gemini prompt)
  - FCS_EXTRA_PROMPT       (additional extraction for FCS/FPS carriers)
  - Date normalisation helpers
  - extract_arrival_notice(pdf_path, gemini_model, subject) → dict

Called by arrival_notice.py (this service's main file).
Uses the shared Gemini model from root extractor.py.
"""

import base64, json, os, re, logging
from datetime import datetime

log = logging.getLogger("arrival_notice.extractor")


from shared.helpers import resolve_carrier_code, ensure_scac_prefix
from services.arrival_notice.fcs_handler import is_fcs_carrier

# ══════════════════════════════════════════════════════════════════════
#  PROMPT
# ══════════════════════════════════════════════════════════════════════

ARRIVAL_NOTICE_PROMPT = """You are a logistics document parser. This is a carrier
ARRIVAL NOTICE (may be multiple pages — READ EVERY PAGE, not just page 1;
the arrival date is not always on the first page).

Return ONLY a valid JSON object (no markdown, no backticks, no extra text):

{
  "reference": "the B/L Number (MBL) from the email Subject (if provided) or printed on the document, or null",
  "container_no": "the container number printed on the document (e.g., 4 letters followed by 7 digits), or null",
  "arrival_date_raw": "the arrival date exactly as printed on the document, or null",
  "carrier_name": "shipping carrier/line name visible in the document, or null",
  "carrier_code": "the 4-letter SCAC carrier code inferred from carrier name or logo (e.g. HLCU, MSKU, CMAU, PNKG), or null"
}

RULES FOR reference (B/L Number):
1. Extract the B/L Number (MBL) ONLY from the document text itself. Do NOT use the email Subject to determine the MBL.
2. Look for a field labeled "B/L Number", "B/L-NO", "Bill of Lading No.", "BL No", or similar.
3. Do NOT confuse it with Customs Reference Number, MRN, Vessel Customs ID, or Vessel/Voyage number.
4. CRITICAL: Clean the extracted B/L Number. Strip parentheticals or extra text (e.g. "(SW)"). Return only the core alphanumeric reference.
5. If no B/L Number field/value is found anywhere in the document, set reference to null.
6. SCAC PREFIX CHECK: After extracting the B/L number, check if it already starts with the
   carrier's 4-letter SCAC code. If it does NOT, prepend the SCAC code.
   Examples:
     - Carrier: ONE (ONEY), BL printed as "MNLG23355900" → return "ONEYMNLG23355900"
     - Carrier: Hapag-Lloyd (HLCU), BL printed as "HLCUSZX2605APPZ0" → already has HLCU, return as-is
   EXCEPTION: Do NOT prepend if the B/L clearly starts with the carrier's identity in a different format or a very similar prefix (e.g. Yang Ming BLs start with "YM" like "YMJAN405110783"; HMM/HDMU BLs might start with "HDMN"). In these cases, return the BL exactly as printed, do NOT prepend the SCAC (e.g. do not return "HDMUHDMN...").
   Only prepend when the B/L number has NO recognizable carrier prefix at all.
   **For All MBl ref Validate first it has carrier code or not if there place as it is else add prefix then place in result.
RULES FOR arrival_date_raw:
1. Search ALL pages for the arrival date. Labels: "ETA", "ETA AT POD", "Est. Arrival Date", "Estimated Time of Arrival", "POD ETA", "Arrival Date".
2. It may appear WITHOUT a label — e.g. "ETA AT POD: Rotterdam ON: Tuesday, 07 Jul, 2026". Still extract it.
3. Pick specifically the ARRIVAL date at the final destination — not the issue date and not the departure date.
4. Copy the date EXACTLY as printed (e.g. "07-JUL-26", "Tuesday, 07 Jul, 2026", "2026-07-07").
5. If not found, set arrival_date_raw to null.

RULES FOR carrier_name and carrier_code:
1. Extract the name of the shipping carrier or line.
2. If the document has a prominent logo like "CMA CGM" at the top, extract "CMA CGM" even without an explicit SCAC code.
3. Infer the 4-letter SCAC code from the carrier name or logo. Common mappings:
   - ONE / Ocean Network Express → ONEY
   - CMA CGM → CMDU
   - Hapag-Lloyd → HLCU
   - Maersk → MAEU (or MRKU for Maersk Line)
   - MSC → MEDU (or MSCU / MRKU)
   - OOCL → OOLU
   - Evergreen → EGLV
   - ZIM → ZIMU
   - Yang Ming → YMLU
   - HMM / Hyundai → HDMU
   - COSCO → COSU
   - Panda Logistics → PNKG
   - PIL / Pacific International Lines → PCIU
   - Wan Hai → WHLC
   - Hamburg Süd → SUDU
   CRITICAL: ALWAYS return carrier_code. If you see a carrier logo or name, you MUST map it.
   Never return carrier_code as null if carrier_name is identified.
"""


# ══════════════════════════════════════════════════════════════════════
#  FCS/FPS EXTRA PROMPT — appended only when FCS carrier is detected
# ══════════════════════════════════════════════════════════════════════

FCS_EXTRA_PROMPT = """
ADDITIONAL EXTRACTION for FCS / FPS (Famous Pacific Shipping) documents:

This is an FPS/FCS arrival notice. In ADDITION to the standard fields above,
also extract these fields and include them in the SAME JSON object:

{
  ... (all standard fields above) ...
  "vessel_name": "the vessel name from the document (e.g. 'COSCO SHIPPING VIRGO'), or null",
  "devanning_date_raw": "the Expected Devanning date exactly as printed (e.g. '17-02-2026'), or null",
  "warehouse_name": "the warehouse company name (e.g. 'POD LOGISTICS & WAREHOUSING BV'), or null",
  "address_line_1": "the warehouse street address (e.g. 'SHANNONWEG 72'), or null",
  "postal_code": "the warehouse postal/zip code (e.g. '3197 LH'), or null",
  "city": "the warehouse city (e.g. 'BOTLEK ROTTERDAM'), or null",
  "country": "the warehouse country (e.g. 'NETHERLANDS'), or null"
}

RULES FOR vessel_name:
1. Look for field labeled "Vessel name" or similar.
2. Return the full vessel name (e.g. "COSCO SHIPPING VIRGO").

RULES FOR devanning_date_raw:
1. Look for "Expected Devanning date" or "Devanning date".
2. Copy the date EXACTLY as printed.

RULES FOR warehouse address:
1. Look for the "Warehouse:" section in the document.
2. Extract the warehouse company name, street, postal code, city, and country SEPARATELY.
3. The postal code is typically a pattern like "3197 LH" (Dutch format).
4. The city may include district info like "BOTLEK ROTTERDAM".
5. Country is typically "NETHERLANDS" for Dutch warehouses.
"""


# ══════════════════════════════════════════════════════════════════════
#  DATE NORMALISATION
# ══════════════════════════════════════════════════════════════════════

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _normalize_date(raw: str) -> str | None:
    """Normalize a wide variety of printed date formats to DD/MM/YY."""
    if not raw:
        return None
    s = raw.strip()

    try:
        from dateutil import parser as dateutil_parser
        dt = dateutil_parser.parse(s, fuzzy=True, dayfirst=True)
        return dt.strftime("%d/%m/%y")
    except Exception:
        pass

    s_clean = re.sub(r'^[A-Za-z]+,\s*', '', s)

    m = re.search(r'\b(\d{1,2})[-\s]([A-Za-z]{3,9})[-,\s]+(\d{2,4})\b', s_clean)
    if m:
        day, mon_txt, year = m.groups()
        mon = _MONTHS.get(mon_txt.lower()[:3])
        if mon:
            yy = year[-2:] if len(year) >= 2 else year.zfill(2)
            return f"{int(day):02d}/{mon:02d}/{yy}"

    m = re.search(r'\b(\d{4})-(\d{1,2})-(\d{1,2})\b', s_clean)
    if m:
        year, mon, day = m.groups()
        return f"{int(day):02d}/{int(mon):02d}/{year[-2:]}"

    m = re.search(r'\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b', s_clean)
    if m:
        day, mon, year = m.groups()
        yy = year[-2:] if len(year) >= 2 else year.zfill(2)
        return f"{int(day):02d}/{int(mon):02d}/{yy}"

    log.warning("  Could not normalize date: '%s'", raw)
    return None


# ══════════════════════════════════════════════════════════════════════
#  REGEX FALLBACK
# ══════════════════════════════════════════════════════════════════════

_DATE_PATTERNS = [
    re.compile(r'\b\d{1,2}[-\s][A-Za-z]{3,9}[-,\s]+\d{2,4}\b'),
    re.compile(r'\b\d{4}-\d{1,2}-\d{1,2}\b'),
    re.compile(r'\b\d{1,2}/\d{1,2}/\d{2,4}\b'),
]
_BL_LABEL_RE = re.compile(
    r'(?:B/L[-\s]?No\.?|Bill of Lading No\.?|B/L Number)[:\s]*([A-Z0-9]{6,})',
    re.IGNORECASE,
)
_ETA_LABEL_RE = re.compile(
    r'(?:ETA|Est(?:imated)?\.?\s*Arrival\s*Date|Estimated Time of Arrival)[^\n:]*[:\s]+([^\n]{4,30})',
    re.IGNORECASE,
)


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


def _regex_fallback(pdf_path: str, result: dict) -> dict:
    text = _extract_text(pdf_path)
    result["flag"] = "needs_manual_review"
    if not text:
        return result
    m = _BL_LABEL_RE.search(text)
    if m:
        result["reference"] = m.group(1).strip()
    m = _ETA_LABEL_RE.search(text)
    if m:
        candidate = m.group(1).strip()
        for pat in _DATE_PATTERNS:
            dm = pat.search(candidate)
            if dm:
                result["arrival_date_raw"] = dm.group(0)
                result["arrival_date"] = _normalize_date(dm.group(0))
                break
    return result


# ══════════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════

def extract_arrival_notice(pdf_path: str, gemini_model, subject: str = None) -> dict:
    """
    Extract reference (B/L Number) + arrival_date from an Arrival Notice PDF.
    For FCS carriers, also extracts vessel_name, devanning_date, and warehouse address.

    Returns: reference, arrival_date_raw, arrival_date, carrier_name, flag,
             doc_type, source_file, extracted_at, and FCS-specific fields.
    """
    result = {
        "doc_type":            "arrival_notice",
        "reference":           None,
        "container_no":        None,
        "arrival_date_raw":    None,
        "arrival_date":        None,
        "carrier_name":        None,
        "carrier_code":        None,
        # FCS-specific fields (populated only for FCS carriers)
        "is_fcs":              False,
        "vessel_name":         None,
        "devanning_date_raw":  None,
        "devanning_date":      None,
        "warehouse_name":      None,
        "address_line_1":      None,
        "postal_code":         None,
        "city":                None,
        "country":             None,
        # Metadata
        "source_file":         os.path.basename(pdf_path),
        "extracted_at":        datetime.now().isoformat(),
        "flag":                None,
    }

    try:
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        # ── First pass: standard extraction ──────────────────────────
        prompt_parts = [
            {"mime_type": "application/pdf", "data": base64.b64encode(pdf_bytes).decode()}
        ]
        if subject:
            prompt_parts.append(f"Email Subject: {subject}")
        prompt_parts.append(ARRIVAL_NOTICE_PROMPT)

        resp = gemini_model.generate_content(
            prompt_parts,
            generation_config={"temperature": 0.0, "max_output_tokens": 300},
        )

        raw = resp.text.strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        parsed = json.loads(raw)
        log.info("  AN Gemini raw: %s", parsed)

        result["reference"]    = (parsed.get("reference") or "").strip() or None
        result["container_no"] = (parsed.get("container_no") or "").strip() or None
        result["carrier_name"] = (parsed.get("carrier_name") or "").strip() or None
        result["carrier_code"] = (parsed.get("carrier_code") or "").strip() or None

        # ── Resolve carrier_code (Gemini output + name-based fallback) ──
        result["carrier_code"] = resolve_carrier_code(
            result["carrier_name"], result["carrier_code"]
        )

        # ── Ensure reference has a known SCAC prefix ────────────────
        if result["reference"] and result["carrier_code"]:
            old_ref = result["reference"]
            result["reference"] = ensure_scac_prefix(result["reference"], result["carrier_code"])
            if result["reference"] != old_ref:
                log.info("  AN Prepended SCAC %s: %s → %s", result["carrier_code"], old_ref, result["reference"])

        # ── Fallback reference → container_no ────────────────────────
        if not result["reference"] and result["container_no"]:
            log.info("  AN reference missing, using container_no: %s", result["container_no"])
            result["reference"] = result["container_no"]

        raw_date = (parsed.get("arrival_date_raw") or "").strip() or None
        result["arrival_date_raw"] = raw_date

        if raw_date:
            normalized = _normalize_date(raw_date)
            result["arrival_date"] = normalized
            if not normalized:
                result["flag"] = "needs_manual_review"
        else:
            result["flag"] = "needs_manual_review"

        # ══════════════════════════════════════════════════════════════
        #  FCS / FPS CARRIER — SECOND PASS with extra fields
        # ══════════════════════════════════════════════════════════════
        if is_fcs_carrier(result["carrier_name"], result["source_file"]):
            log.info("  AN FCS carrier detected — running extra extraction")
            result["is_fcs"] = True

            try:
                fcs_prompt_parts = [
                    {"mime_type": "application/pdf",
                     "data": base64.b64encode(pdf_bytes).decode()}
                ]
                if subject:
                    fcs_prompt_parts.append(f"Email Subject: {subject}")
                fcs_prompt_parts.append(ARRIVAL_NOTICE_PROMPT + FCS_EXTRA_PROMPT)

                fcs_resp = gemini_model.generate_content(
                    fcs_prompt_parts,
                    generation_config={"temperature": 0.0, "max_output_tokens": 600},
                )

                fcs_raw = fcs_resp.text.strip()
                fcs_raw = re.sub(r'^```(?:json)?\s*', '', fcs_raw)
                fcs_raw = re.sub(r'\s*```$', '', fcs_raw)
                fcs_parsed = json.loads(fcs_raw)
                log.info("  AN FCS Gemini raw: %s", fcs_parsed)

                # Populate FCS-specific fields
                result["vessel_name"] = (
                    fcs_parsed.get("vessel_name") or ""
                ).strip() or None
                result["warehouse_name"] = (
                    fcs_parsed.get("warehouse_name") or ""
                ).strip() or None
                result["address_line_1"] = (
                    fcs_parsed.get("address_line_1") or ""
                ).strip() or None
                result["postal_code"] = (
                    fcs_parsed.get("postal_code") or ""
                ).strip() or None
                result["city"] = (
                    fcs_parsed.get("city") or ""
                ).strip() or None
                result["country"] = (
                    fcs_parsed.get("country") or ""
                ).strip() or None

                # Devanning date
                dev_raw = (
                    fcs_parsed.get("devanning_date_raw") or ""
                ).strip() or None
                result["devanning_date_raw"] = dev_raw
                if dev_raw:
                    result["devanning_date"] = _normalize_date(dev_raw)

            except Exception as e:
                log.warning("  AN FCS extra extraction failed: %s", e)
                # FCS extra fields remain None — non-fatal, standard fields are intact
        # ══════════════════════════════════════════════════════════════

    except json.JSONDecodeError as e:
        log.warning("  AN JSON parse failed: %s", e)
        return _regex_fallback(pdf_path, result)
    except Exception as e:
        log.warning("  AN extraction failed: %s", e)
        return _regex_fallback(pdf_path, result)

    return result