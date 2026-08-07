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
  "references": ["one or more reference numbers (B/L, Container, OI number, or MTD/SWB reference)"] or [],
  "secondary_ref": "the secondary reference. For Hapag-Lloyd, extract SHIPMENT no (e.g. 13089204) else Container. For others, extract Container. Or null.",
  "invoice_no": "the invoice number printed on the document, or null",
  "carrier_name": "the name of the shipping line or carrier (e.g. CMA CGM), or null",
  "carrier_code": "the 4-letter SCAC carrier code inferred from carrier name or logo (e.g. HLCU, MSKU, CMAU, PNKG), or null"
}

RULES FOR references:
1. PRIORITY 1 (HIGHEST, ALWAYS WINS) — OI or OE Number. Look closely for fields labeled "Your-Reference", "Our Ref", "Reference", "YOUR REF.", or anywhere for a string starting with "OI" or "OE" followed by 5+ digits (e.g., OI2615762, OE2625140). If ANY OI/OE number appears ANYWHERE on the document — including repeated under a "REFERENCES:" block next to labels like CUSTOMER / FOB FORWARDER (MTD) / SHIPPER (MTD) — then references MUST contain ONLY that OI/OE number (deduplicated, one entry even if it's repeated many times under different labels). In this case COMPLETELY IGNORE and DO NOT extract the B/L-NO, any MTD number, or any container number from the document — they are irrelevant once an OI/OE number exists. Do not combine an OI/OE number with a B/L/MTD/container number in the same references array under any circumstance.
2. PRIORITY 2 — Only when NO OI/OE number exists anywhere on the document: Hapag-Lloyd "SWB-NO." / "REFERENCES: SHIPPER (MTD)" block. When the values under this block are genuine carrier MTD-style references (not OI/OE), it can list TWO OR THREE separate MTD reference numbers (e.g. HLCUNG12606TDDM2, HLCUNG12606AUVB6, HLCUNG12606TDDL1) — one invoice can cover multiple shipments. If this block is present, return EVERY distinct reference listed in it as its own separate array entry. Do NOT pick just one and do NOT merge them together.
3. PRIORITY 3 — B/L Number / Bill of Lading No. Only if NO OI/OE number and NO SWB-NO/REFERENCES block exists. Look for fields labeled "B/L No.", "Bill of Lading", etc. Usually has carrier prefix + digits. Single entry. EXTREMELY IMPORTANT: NEVER extract a "Case Id" (e.g., NCPW26239000) or "Approval Code" as a B/L number!
4. PRIORITY 4 — Container Number. Exactly 4 uppercase letters + 7 digits. Single entry.
5. If the document is from CMA CGM and the B/L number is exactly 10 letters/digits (e.g., VLN0150979), prepend 'CMDU'.
6. Extract exactly as printed, removing spaces. Each reference must be PURE ALPHANUMERIC — never include "/", "-", spaces, or any other punctuation inside a single reference. If you see what looks like two different codes near each other (e.g. a shipment code next to an unrelated numeric code), they are almost always two DIFFERENT fields, not one reference — do not concatenate them into a single string. If you are not confident a piece of text is a real shipment/B/L/container reference, leave it out of the array rather than guessing.
7. Do NOT extract short internal carrier references (like '23461314'), charge/line-item codes, dates, Case IDs, or amounts as a reference, EXCEPT for Hapag-Lloyd SHIPMENT numbers which go to secondary_ref, not references.
8. If nothing on the document qualifies as a reference, return an empty array — do not invent one.

RULES FOR secondary_ref:
1. If carrier is Hapag-Lloyd: Extract the "SHIPMENT" number (e.g. 13089204) if it exists. If not, extract a Container Number.
2. For all other carriers: Extract a Container Number.
3. Must be a single pure-alphanumeric value, same punctuation rule as references.

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


def _enforce_oi_oe_priority(references: list) -> list:
    """
    Code-level guardrail (not just a prompt instruction) — OI/OE is always
    PRIORITY 1 and must be the ONLY reference used when present. Some
    Hapag-Lloyd invoices repeat the SAME OE number under several stakeholder
    labels in the "REFERENCES:" block (CUSTOMER, FOB FORWARDER (MTD),
    SHIPPER (MTD)) right next to an unrelated B/L-NO — Gemini can end up
    returning both the OE number AND the B/L number together despite the
    prompt saying not to. If any OI/OE-shaped reference is present, drop
    every non-OI/OE reference and dedupe the OI/OE ones down to one entry
    each, so a B/L/MTD/container number never rides along with an OE ref
    into a Jordex search or upload.
    """
    oi_oe = [r for r in references if re.fullmatch(r'(OI|OE)\d{4,}', r)]
    if not oi_oe:
        return references
    deduped = list(dict.fromkeys(oi_oe))
    dropped = [r for r in references if r not in deduped]
    if dropped:
        log.info(f"  OI/OE present — dropping non-OI/OE reference(s) {dropped}, keeping {deduped}")
    return deduped


# ══════════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════

def extract_invoice_carrier(pdf_path: str, gemini_model=None) -> dict:
    """
    Returns a dict with `references` — a LIST of shipment references, since
    one Hapag-Lloyd invoice can cover multiple MTD/SWB-NO shipments (see
    PRIORITY 2 in INVOICE_CARRIER_PROMPT). `reference` is kept as
    `references[0]` for callers that only care about a single value.
    """
    log.info(f"  Extracting Invoice Carrier from {os.path.basename(pdf_path)}")

    result = {
        "doc_type":     "invoice_carrier",
        "references":   [],
        "reference":    None,
        "secondary_ref": None,
        "invoice_no":   None,
        "carrier_name": None,
        "carrier_code": None,
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
                generation_config={"temperature": 0.0, "max_output_tokens": 200},
            )

            raw = resp.text.strip()
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
            parsed = json.loads(raw)

            raw_refs = parsed.get("references")
            if raw_refs is None:
                # Defensive: model returned the old singular "reference" key.
                single = parsed.get("reference")
                raw_refs = [single] if single else []
            if not isinstance(raw_refs, list):
                raw_refs = [raw_refs]

            secondary_ref = (parsed.get("secondary_ref") or "").strip().upper() or None
            invoice_no    = (parsed.get("invoice_no") or "").strip() or None
            carrier_name  = (parsed.get("carrier_name") or "").strip() or None
            carrier_code  = (parsed.get("carrier_code") or "").strip() or None

            resolved_scac = resolve_carrier_code(carrier_name, carrier_code)

            references = []
            for r in raw_refs:
                r = (r or "").strip().upper()
                if not r:
                    continue
                r = re.sub(r'\s+', '', r)

                # OCR Correction for OI numbers (e.g. 012618725 -> OI2618725)
                if re.match(r'^(01|0I|O1)\d{5,}$', r):
                    old_r = r
                    r = "OI" + r[2:]
                    log.info(f"  Invoice OCR Correction: {old_r} -> {r}")

                # Reject anything that isn't pure alphanumeric — a garbled
                # extraction like "624W/683632" must never reach a Jordex
                # search as a "valid-looking" reference.
                if not re.fullmatch(r'[A-Z0-9]+', r):
                    log.warning(f"  Dropping non-alphanumeric extracted reference: '{r}'")
                    continue

                # Prepend SCAC if reference is a B/L (not an OI/OE number)
                if resolved_scac and not r.startswith("OI") and not r.startswith("OE"):
                    old_r = r
                    r = ensure_scac_prefix(r, resolved_scac)
                    if r != old_r:
                        log.info("  Invoice Prepended SCAC %s: %s → %s", resolved_scac, old_r, r)

                references.append(r)

            references = _enforce_oi_oe_priority(references)

            if secondary_ref:
                secondary_ref = re.sub(r'\s+', '', secondary_ref)
                if not re.fullmatch(r'[A-Z0-9]+', secondary_ref):
                    log.warning(f"  Dropping non-alphanumeric secondary_ref: '{secondary_ref}'")
                    secondary_ref = None

            result["references"]    = references
            result["reference"]     = references[0] if references else None
            result["secondary_ref"] = secondary_ref
            result["invoice_no"]    = invoice_no
            result["carrier_name"]  = carrier_name
            result["carrier_code"]  = carrier_code
            log.info(f"  Invoice Gemini: refs={references} sec={secondary_ref} invoice={invoice_no} scac={resolved_scac}")

        except Exception as e:
            log.warning(f"  Invoice Gemini failed: {e}. Keyword fallback.")
            fb = _keyword_fallback(pdf_path)
            result["references"] = [fb["reference"]] if fb["reference"] else []
            result["reference"]  = fb["reference"]
            result["invoice_no"] = fb["invoice_no"]
            result["flag"]       = "low_confidence"
    else:
        fb = _keyword_fallback(pdf_path)
        result["references"] = [fb["reference"]] if fb["reference"] else []
        result["reference"]  = fb["reference"]
        result["invoice_no"] = fb["invoice_no"]
        result["flag"]       = "low_confidence"

    return result
