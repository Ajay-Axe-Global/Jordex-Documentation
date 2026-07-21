"""
services/invoice_carrier/extractor.py — Invoice Carrier Extraction Logic
=========================================================================
Contains:
  - INVOICE_CARRIER_PROMPT
  - Keyword fallback helpers
  - extract_invoice_carrier(pdf_path, gemini_model) → dict

Called by invoice_carrier.py (this service's main file).
"""

import base64, json, os, re, logging
from datetime import datetime
from shared.helpers import resolve_carrier_code, ensure_scac_prefix

log = logging.getLogger("invoice_carrier.extractor")

# ══════════════════════════════════════════════════════════════════════
#  PROMPT
# ══════════════════════════════════════════════════════════════════════

INVOICE_CARRIER_PROMPT = """You are a logistics document parser. This is an INVOICE from a carrier.

Return ONLY a valid JSON object (no markdown, no backticks, no extra text):

{
  "reference": "the primary reference number (B/L, Container, or OI number), or null",
  "secondary_ref": "the secondary reference. For Hapag-Lloyd, extract SHIPMENT no (e.g. 13089204) else Container. For others, extract Container. Or null.",
  "invoice_no": "the invoice number printed on the document, or null",
  "carrier_name": "the name of the shipping line or carrier (e.g. CMA CGM), or null",
  "carrier_code": "the 4-letter SCAC carrier code inferred from carrier name or logo (e.g. HLCU, MSKU, CMAU, PNKG), or null"
}

RULES FOR reference:
1. PRIORITY 1 (HIGHEST) — OI or OE Number. Look for fields labeled "Your-Reference", "Our Ref", "Reference", or anywhere for a string starting with "OI" or "OE" followed by 5+ digits (e.g., OI2615762). If found, MUST use it.
2. PRIORITY 2 — B/L Number / Bill of Lading No. Only if NO OI/OE number exists. Usually has carrier prefix + digits.
3. PRIORITY 3 — Container Number. Exactly 4 uppercase letters + 7 digits.
4. If the document is from CMA CGM and the B/L number is exactly 10 letters/digits (e.g., VLN0150979), prepend 'CMDU'.
5. Extract exactly as printed, removing spaces.
6. Do NOT extract short internal carrier references (like '23461314') as the reference, EXCEPT for Hapag-Lloyd SHIPMENT numbers which go to secondary_ref.

RULES FOR secondary_ref:
1. If carrier is Hapag-Lloyd: Extract the "SHIPMENT" number (e.g. 13089204) if it exists. If not, extract a Container Number.
2. For all other carriers: Extract a Container Number.

RULES FOR invoice_no:
1. Look for fields labeled "Invoice No", "Invoice Number", "Document No", "Inv. No.", "Factuur", "Rechnung", etc.
2. Extract the exact invoice number.
3. Do NOT extract customer number, VAT number, or amount as the invoice number.

RULES FOR carrier_name:
1. Extract the name of the shipping carrier or line.
2. If the document has a prominent "CMA CGM" logo at the top, extract "CMA CGM".
"""


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
    text       = _extract_text(pdf_path)
    text_upper = text.upper() if text else ""
    reference  = None
    invoice_no = None

    if text_upper:
        oi_match = re.search(r'\b(O[IE]\d{5,})\b', text_upper)
        if oi_match:
            reference = oi_match.group(1)

        if not reference:
            bl_match = re.search(
                r'(?:B/L\s*NO\.?|BL\s*NO\.?|BILL\s+OF\s+LADING)[:\s]*([A-Z0-9]{6,20})',
                text, re.IGNORECASE,
            )
            if bl_match:
                reference = bl_match.group(1).strip().upper()
                if "CMA CGM" in text_upper and len(reference) == 10 and not reference.startswith("CMDU"):
                    reference = "CMDU" + reference

        if not reference:
            cont_match = re.search(r'\b([A-Z]{4}\s*\d{7})\b', text_upper)
            if cont_match:
                reference = cont_match.group(1).replace(" ", "")

        inv_match = re.search(
            r'(?:INVOICE\s*NO\.?|INV\.?\s*NO\.?|FACTUUR)[:\s]*([A-Z0-9\-/]{4,20})',
            text, re.IGNORECASE,
        )
        if inv_match:
            invoice_no = inv_match.group(1).strip()

    return {"reference": reference, "invoice_no": invoice_no}


# ══════════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════

def extract_invoice_carrier(pdf_path: str, gemini_model=None) -> dict:
    log.info(f"  Extracting Invoice Carrier from {os.path.basename(pdf_path)}")

    result = {
        "doc_type":     "invoice_carrier",
        "reference":    None,
        "secondary_ref": None,
        "invoice_no":   None,
        "carrier_name": None,
        "source_file":  os.path.basename(pdf_path),
        "extracted_at": datetime.now().isoformat(),
        "flag":         None,
    }

    if gemini_model is not None:
        try:
            with open(pdf_path, "rb") as f:
                pdf_bytes = f.read()

            resp = gemini_model.generate_content(
                [
                    {"mime_type": "application/pdf", "data": base64.b64encode(pdf_bytes).decode()},
                    INVOICE_CARRIER_PROMPT,
                ],
                generation_config={"temperature": 0.0, "max_output_tokens": 150},
            )

            raw = resp.text.strip()
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
            parsed = json.loads(raw)

            reference    = (parsed.get("reference") or "").strip().upper() or None
            secondary_ref = (parsed.get("secondary_ref") or "").strip().upper() or None
            invoice_no   = (parsed.get("invoice_no") or "").strip() or None
            carrier_name = (parsed.get("carrier_name") or "").strip() or None
            carrier_code = (parsed.get("carrier_code") or "").strip() or None

            # OCR Correction for OI numbers (e.g. 012618725 -> OI2618725)
            if reference and re.match(r'^(01|0I|O1)\d{5,}$', reference):
                old_ref = reference
                reference = "OI" + reference[2:]
                log.info(f"  Invoice OCR Correction: {old_ref} -> {reference}")

            # Resolve carrier code
            resolved_scac = resolve_carrier_code(carrier_name, carrier_code)

            # Prepend SCAC if reference is a B/L (not an OI/OE number)
            if reference and resolved_scac and not reference.startswith("OI") and not reference.startswith("OE"):
                old_ref = reference
                reference = ensure_scac_prefix(reference, resolved_scac)
                if reference != old_ref:
                    log.info("  Invoice Prepended SCAC %s: %s → %s", resolved_scac, old_ref, reference)

            result["reference"]    = reference
            result["secondary_ref"] = secondary_ref
            result["invoice_no"]   = invoice_no
            result["carrier_name"] = carrier_name
            result["carrier_code"] = carrier_code
            log.info(f"  Invoice Gemini: ref={reference} sec={secondary_ref} invoice={invoice_no} scac={resolved_scac}")

        except Exception as e:
            log.warning(f"  Invoice Gemini failed: {e}. Keyword fallback.")
            fb = _keyword_fallback(pdf_path)
            result["reference"]  = fb["reference"]
            result["invoice_no"] = fb["invoice_no"]
            result["flag"]       = "low_confidence"
    else:
        fb = _keyword_fallback(pdf_path)
        result["reference"]  = fb["reference"]
        result["invoice_no"] = fb["invoice_no"]
        result["flag"]       = "low_confidence"

    if result["reference"]:
        result["reference"] = re.sub(r'\s+', '', result["reference"])

    return result
