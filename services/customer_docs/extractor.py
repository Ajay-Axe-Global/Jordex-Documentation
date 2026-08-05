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


# Ocean carriers — BLs issued by these are ALWAYS Master BLs
_CARRIER_TO_SCAC = {
    "CMA CGM":              "CMDU",
    "HAPAG-LLOYD":          "HLCU",
    "HAPAG LLOYD":          "HLCU",
    "MAERSK":               "MAEU",
    "MSC":                  "MEDU",
    "OOCL":                 "OOLU",
    "EVERGREEN":            "EGLV",
    "ZIM":                  "ZIMU",
    "ONE":                  "ONEY",
    "OCEAN NETWORK EXPRESS":"ONEY",
    "YANG MING":            "YMLU",
    "HMM":                  "HDMU",
    "HYUNDAI":              "HDMU",
    "COSCO":                "COSU",

    "WAN HAI":              "WHLC",
    
}

# Flat, human-readable list of the known ocean carriers above — injected
# into the classification prompt (GATE 0) so Gemini checks the issuer
# against a concrete closed list instead of a vague "actual ocean carrier"
# instruction. Single source of truth: derived from _CARRIER_TO_SCAC so
# the prompt can never drift out of sync with the Python-side carrier map.
#
# Includes each carrier's SCAC code alongside its name (e.g. "MAERSK
# (MAEU)") — some carrier documents identify themselves in the masthead
# ONLY by SCAC code (e.g. a Maersk waybill header reading "SCAC MAEU"
# with the word "Maersk" appearing nowhere near the logo/title, only
# buried in fine print). A name-only list caused GATE 0 to fail on those
# documents and default them to HOUSE BILL OF LADING even when the
# Consignee was genuinely Jordex.
_KNOWN_OCEAN_CARRIERS_LIST = ", ".join(
    f"{name} ({scac})" for name, scac in sorted(_CARRIER_TO_SCAC.items())
)


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
  Reference number priority (use the FIRST one that is actually present):
    1. "BL/SWB No(s).:" — the carrier's Bill of Lading / Sea Waybill
       number. This is the real trackable shipment number and takes
       priority over any internal reference.
    2. "Uw referentie" / "Your reference" / OI number (pattern OI followed
       by digits, e.g. OI2615762) — only if the value looks like a real
       reference code, NOT a free-text customer/shipment name (e.g.
       "THAI WORLD" is a name, not a reference — skip it).
    3. "Our Reference" / the carrier's own booking number — last resort.
  container_no: only set this if an actual container number (4 letters +
    7 digits, e.g. HLXU1234567) is visibly printed on the page. Do NOT
    infer, guess, or reuse a BL/booking number as a container number.

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

── CARGO RELEASE NOTICE / RELEASE NOTICE ──
CRITICAL: A CARGO RELEASE NOTICE (also called "Release Notice", "Cargo Release",
"PIN Release", "Container Release") is a carrier administrative notice informing that
cargo has been released for pickup. It is NOT a Bill of Lading even if it carries a
B/L number and is printed on carrier letterhead (e.g. Evergreen Line, Maersk, CMA CGM).
If the header/title/body says ANY of:
  "CARGO RELEASE NOTICE", "RELEASE NOTICE", "CARGO RELEASE",
  "PIN RELEASE", "CONTAINER RELEASE", "RELEASE ADVISE", "RELEASE ADVICE"
  → classify as: "ADDITIONAL FILES"
  doc_title: use the exact header (e.g. "Cargo Release Notice")
  reference_number: extract the B/L number if present.
  CRITICAL: Do NOT proceed to Step 2. This is NOT a Bill of Lading.
 
── CERTIFICATE OF ORIGIN ──
If the header says "CERTIFICATE OF ORIGIN", "C/O", "GSP FORM A":
  → classify as: "ADDITIONAL FILES"
  doc_title: "Certificate of Origin"
 
── INSURANCE CERTIFICATE ──
If the header says "INSURANCE CERTIFICATE", "CARGO INSURANCE":
  → classify as: "ADDITIONAL FILES"
  doc_title: use the exact header text.
 
── WEIGHT CERTIFICATE / INSPECTION / SAFETY CERTIFICATE ──
If the header says "WEIGHT CERTIFICATE", "INSPECTION CERTIFICATE",
  "SURVEY REPORT", "FUMIGATION CERTIFICATE", "PHYTOSANITARY CERTIFICATE",
  "SAFETY CERTIFICATE", "GAS CERTIFICATE", "MEASUREMENT CERTIFICATE", or a
  generic "CERTIFICATE" issued by a surveyor/inspection/customs-support
  company about a specific container:
  → classify as: "ADDITIONAL FILES"
  doc_title: use the exact header text.
  reference_number: look for a "Customer reference number" field FIRST —
    this is usually the actual carrier BL/booking number (e.g.
    "MAEU270448365", recognizable by its 4-letter carrier SCAC prefix).
    Do NOT use the surveyor/inspector's own internal "Order number" /
    "Job number" / "Case number" — that ID only exists in their system,
    it is not searchable in Jordex.
  container_no: look for an "Object number" / "Container No." field — the
    container being measured/inspected is the container_no
    (e.g. "TGHU966483-6" → container_no "TGHU9664836").

── CMR / TRANSPORT DOCUMENT ──
If the header/title says ANY of:
  "CMR", "LETTRE DE VOITURE", "VRACHTBRIEF", "FRACHTBRIEF",
  "TRANSPORTDOKUMENT", "CONSIGNMENT NOTE", "CMR CONSIGNMENT NOTE",
  "VERVOERDOCUMENT", "TRANSPORT DOCUMENT"
  This is a ROAD TRANSPORT document, NOT a Bill of Lading.
  It has sender/receiver fields that look like Shipper/Consignee — ignore them.
  → classify as: "ADDITIONAL FILES"
  doc_title: "CMR"
  reference_number: Look for an OI number, container number, or B/L reference
    mentioned anywhere on the document. If none found, return null.
  CRITICAL: Do NOT proceed to Step 2. This is NOT a Bill of Lading.

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

── GATE 0 — IDENTIFY THE ISSUER FIRST (do this BEFORE reading the Consignee) ──
A Master Bill of Lading can ONLY be issued by one of these actual ocean
carriers — this is a CLOSED list, there are no others:
  <<CARRIER_LIST>>
Look at the carrier logo, letterhead, and any "Carrier" field on the page.
  - If the issuer is NOT one of the carriers listed above (an unfamiliar
    company name, a freight forwarder, an NVOCC, a trading company, or
    any name you do not clearly recognize from that list) — the document
    CANNOT be a Master Bill of Lading, no matter how official, formal, or
    "master"-looking its layout is.
    → It MUST be HOUSE BILL OF LADING. Skip Rule A entirely; go to Rule B.
  - Do NOT infer carrier identity from formatting, letterhead elegance, or
    the words "Bill of Lading"/"Multimodal Transport" in the title — a
    forwarder's own house bill often looks just as formal as a carrier's
    master bill.
  - If you cannot clearly match the issuer to one of the listed carriers,
    default to HOUSE BILL OF LADING.

RULE A — MASTER BILL OF LADING (MBL):
  BOTH conditions are required:
    1. GATE 0 passes — the issuer IS one of the listed ocean carriers.
    2. The CONSIGNEE box MUST contain "JORDEX" (any variation) as the
       primary consignee.
  → doc_type = "MASTER BILL OF LADING"
  Note EDGE CASE :  Sometime Carrier logo there but consginee not Jordex .if carrier matchs then Thats MBL.

RULE B — HOUSE BILL OF LADING (HBL):
  GATE 0 fails (issuer is not a recognized ocean carrier), OR the
  CONSIGNEE box is any company other than JORDEX.
  → doc_type = "HOUSE BILL OF LADING"

CRITICAL CONSIGNEE RULES:
  - "FOR DELIVERY, PLEASE APPLY TO" is NOT the consignee.
  - JORDEX in Notify Party, Delivery Agent, or anywhere else does NOT count as the Consignee.
  - Read the CONSIGNEE box independently from the NOTIFY PARTY box — they
    frequently show the SAME company name right next to each other. Do
    not let the Notify Party's presence influence your reading of the
    Consignee box.
  - ONLY the CONSIGNEE box and GATE 0's issuer check determine MBL vs HBL.
  - EXCEPTION FOR LOGOS/CARRIERS: A prominent carrier/NVOCC logo (e.g. BEE LOGISTICS, Hapag-Lloyd, ZIM, etc.) at the top does NOT make it an MBL. If the Consignee is NOT Jordex, you MUST classify it as a HOUSE BILL OF LADING, regardless of the logo.
  - EXCEPTION FOR FORWARDERS: If the document is issued by a Freight Forwarder or NVOCC, it is ALWAYS a HOUSE BILL OF LADING. Master Bills are ONLY issued by actual ocean carriers — see GATE 0.
    Known freight forwarders that ALWAYS produce HBLs (never MBLs):
    "GREEN LOGISTICS", "GREENX LOGISTICS", "GREENX LOGISTICS CO.", "GREENX LOGISTICS CO., LTD",
    "MRF INTERNATIONAL FORWARDING", "KUEHNE+NAGEL", "FIATA", "BEE LOGISTICS".
  - CRITICAL: If you see the GREEN LOGISTICS or GREENX LOGISTICS logo or name ANYWHERE on the document — even prominently at the top — classify it as HOUSE BILL OF LADING. Do NOT upgrade to MBL.

=====================================================================
STEP 3 — EXTRACT REFERENCE NUMBER AND DOC TITLE
=====================================================================

CRITICAL — DO NOT GUESS:
Only fill reference_number / container_no with a value that is literally
printed in the document. If no such value is visible or legible, return
null for that field. NEVER invent, estimate, or reuse an example value
from these instructions (e.g. do not return "HLXU1234567" or
"MRSU6620410" — those are illustrative examples only, never real answers).

PRIORITY 0 — Prefer carrier-identifiable references over internal IDs:
  A document page can show several reference-like fields (e.g. "Order
  number", "Case number", "Job number", "Customer reference number",
  "Reference", "Booking number"). When more than one is present, PREFER
  the value that starts with a known 4-letter ocean-carrier SCAC code
  (MAEU, HLCU, MSCU, MEDU, ONEY, YMLU, EGLV, COSU, OOLU, ZIMU, CMDU,
  HDMU, PCIU, WHLC, SUDU) or matches an OI pattern — these are the values
  Jordex can actually be searched by. A plain internal order/job/case
  number issued by a customs broker, surveyor, or agent (not the carrier)
  is NOT a valid reference_number, even if it is the most prominent
  number on the page.

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
  "carrier_name": "the ocean carrier name OR its SCAC code, whichever is actually printed/visible on the document — some carriers only show the SCAC code (e.g. 'SCAC MAEU' in the header) with no spelled-out name nearby. ONLY if it matches one of the carriers/codes listed in GATE 0 (e.g. CMA CGM, MAERSK, MAEU, MSC, MEDU), otherwise null — do NOT put a forwarder/NVOCC/unrecognized company name in this field",
  "confidence": "high" or "medium" or "low"
}

CRITICAL:
- "ADDITIONAL FILES" is the fallback — use it when you truly cannot identify the doc type.
- NEVER return "UNKNOWN". Always classify.
- NEVER return just "INVOICE" — always resolve to AGENT INVOICE or COMMERCIAL INVOICE.
- Freetime/Detention/Demurrage notices are ADDITIONAL FILES, NOT ARRIVAL NOTICE.
- For ADDITIONAL FILES: still extract the Bill of Lading reference_number if visible.
- Always extract doc_title regardless of doc_type."""

# Inject the closed carrier list (GATE 0) — kept as a post-hoc .replace()
# rather than an f-string/`.format()` because the prompt's JSON output
# template above contains literal `{`/`}` braces that would otherwise need
# escaping.
CUSTOMER_DOC_CLASSIFY_PROMPT = CUSTOMER_DOC_CLASSIFY_PROMPT.replace(
    "<<CARRIER_LIST>>", _KNOWN_OCEAN_CARRIERS_LIST
)


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
        elif re.search(r'\bCARGO\s+RELEASE|\bRELEASE\s+NOTICE|\bPIN\s+RELEASE|\bCONTAINER\s+RELEASE|\bRELEASE\s+ADVI[SC]E\b', text_upper):
            doc_type = "ADDITIONAL FILES"
            # Grab first matching phrase as title
            for phrase in ("CARGO RELEASE NOTICE", "RELEASE NOTICE", "CARGO RELEASE", "PIN RELEASE", "CONTAINER RELEASE"):
                if phrase in text_upper:
                    doc_title = phrase.title()
                    break
            else:
                doc_title = "Cargo Release Notice"
        elif re.search(r'\bARRIVAL\s+NOTICE\b|\bNOTICE\s+OF\s+ARRIVAL\b', text_upper):
            doc_type = "ARRIVAL NOTICE"
            doc_title = "Arrival Notice"
        elif re.search(r'\bBOOKING\s+CONFIRM', text_upper) or re.search(r'\bBOEKINGS', text_upper):
            doc_type = "BOOKING CONFIRMATION"
            doc_title = "Booking Confirmation"
        elif re.search(r'\bINVOICE\b|\bTAX\s+INVOICE\b|\bFREIGHT\s+INVOICE\b', text_upper):
            doc_type = "AGENT INVOICE" if "JORDEX" in text_upper else "COMMERCIAL INVOICE"
        elif re.search(r'\bCMR\b|\bVRACHTBRIEF\b|\bFRACHTBRIEF\b|\bLETTRE\s+DE\s+VOITURE\b', text_upper):
            doc_type = "ADDITIONAL FILES"
            doc_title = "CMR"
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

        # Container number (allows a space/hyphen before the ISO check digit,
        # e.g. "TGHU966483-6")
        cont_match = re.search(r'\b([A-Z]{4})\s?(\d{6})[\s-]?(\d)\b', text_upper)
        if cont_match:
            container_no = cont_match.group(1) + cont_match.group(2) + cont_match.group(3)

    return {
        "doc_type": doc_type,
        "reference_number": reference_number,
        "container_no": container_no,
        "doc_title": doc_title,
        "confidence": "low",
    }


def _looks_like_bl_or_oi(ref: str) -> bool:
    """True if ref looks like a real SCAC-prefixed BL number or an OI reference —
    i.e. something Jordex can actually be searched by. A 3-letter carrier-internal
    code like "DRA5532" does NOT qualify."""
    return bool(ref) and (
        (re.match(r'^[A-Z]{4}', ref) and not ref.isdigit()) or re.match(r'^OI\d{4,}', ref)
    )


def _resolve_folder_name(doc_type: str, reference_number: str, container_no: str) -> str | None:
    ref = None
    if reference_number:
        r = re.sub(r'\s+', '', reference_number).upper()
        if len(r) >= 6:
            ref = r

    container = None
    if container_no:
        c = re.sub(r'\s+', '', container_no).upper()
        if re.fullmatch(r'[A-Z]{4}\d{7}', c):
            container = c

    if doc_type == "BOOKING CONFIRMATION":
        # Booking confirmations: accept booking numbers (can be all digits)
        return ref or container

    if doc_type in ("ADDITIONAL FILES", "ARRIVAL NOTICE"):
        # These doc types don't carry their own BL number — reference_number here
        # is often just a carrier-internal AN/notice code (e.g. "DRA5532"), which
        # Jordex can't be searched by. Only trust it if it actually looks like a
        # real BL/OI reference; otherwise the container number is the far more
        # meaningful Jordex search key.
        if ref and _looks_like_bl_or_oi(ref):
            return ref
        return container or ref

    # HBL/MBL, invoices, debit notes, packing lists: reference_number IS the
    # document's own number (extracted per Step 3 of the prompt) — trust it as-is.
    return ref or container


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
    "CMR": "ADDITIONAL FILES",
    "CMR CONSIGNMENT NOTE": "ADDITIONAL FILES",
    "VRACHTBRIEF": "ADDITIONAL FILES",
    "TRANSPORT DOCUMENT": "ADDITIONAL FILES",
    "CARGO RELEASE NOTICE": "ADDITIONAL FILES",
    "CARGO RELEASE": "ADDITIONAL FILES",
    "RELEASE NOTICE": "ADDITIONAL FILES",
    "PIN RELEASE": "ADDITIONAL FILES",
    "CONTAINER RELEASE": "ADDITIONAL FILES",
    "RELEASE ADVICE": "ADDITIONAL FILES",
    "RELEASE ADVISE": "ADDITIONAL FILES",
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

    carrier_name = None

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
            
            # Normalise all OI OCR variants (01, O1, 0I, 0i, etc.)
            if reference_number:
                from shared.helpers import normalize_oi_reference
                reference_number = normalize_oi_reference(reference_number)
                
            container_no = (parsed.get("container_no") or "").strip().upper() or None
            
            # If Gemini missed the container number, try extracting via regex
            if not container_no:
                _txt = _extract_text(pdf_path)
                if _txt:
                    cont_match = re.search(r'\b([A-Z]{4})\s?(\d{6})[\s-]?(\d)\b', _txt.upper())
                    if cont_match:
                        container_no = cont_match.group(1) + cont_match.group(2) + cont_match.group(3)
                        log.info("  Regex fallback extracted container_no: %s", container_no)

            doc_title = (parsed.get("doc_title") or "").strip() or None
            confidence = (parsed.get("confidence") or "high").strip().lower()
            carrier_name = (parsed.get("carrier_name") or "").strip().upper() or None

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
            carrier_name = None

        except Exception as e:
            log.warning("  Customer Doc Gemini failed: %s — keyword fallback", e)
            fb = _keyword_fallback(pdf_path)
            doc_type, reference_number = fb["doc_type"], fb["reference_number"]
            container_no, doc_title = fb["container_no"], fb["doc_title"]
            confidence = "low"
            carrier_name = None

    # ── No Gemini ────────────────────────────────────────────────────
    else:
        fb = _keyword_fallback(pdf_path)
        doc_type, reference_number = fb["doc_type"], fb["reference_number"]
        container_no, doc_title = fb["container_no"], fb["doc_title"]
        confidence = "low"
        carrier_name = None

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

    # ── Ocean carrier detection → upgrade HBL → MBL + prepend SCAC ──
    # Only upgrade genuine Bill of Lading documents. If the doc_title indicates
    # this is a carrier NOTICE / RELEASE / MANIFEST (i.e. not actually a BL),
    # skip the upgrade even if an ocean carrier logo is present on the page.
    _NON_BL_TITLE_PATTERNS = re.compile(
        r'\bRELEASE\b|\bMANIFEST\b|\bNOTICE\b|\bFREETIME\b|\bDETENTION\b|\bDEMURRAGE\b',
        re.IGNORECASE,
    )
    _is_non_bl_title = bool(doc_title and _NON_BL_TITLE_PATTERNS.search(doc_title))

    # Known freight forwarders / NVOCCs — NEVER upgrade these to MBL, even if
    # their logo appears prominently on the document.  They issue HBLs only.
    _KNOWN_FORWARDERS = {
        "GREEN LOGISTICS", "GREENX LOGISTICS", "GREENX LOGISTICS CO",
        "MRF INTERNATIONAL FORWARDING", "KUEHNE+NAGEL", "BEE LOGISTICS",
    }
    _is_forwarder = carrier_name and any(fw in carrier_name for fw in _KNOWN_FORWARDERS)

    if doc_type == "HOUSE BILL OF LADING" and carrier_name and not _is_non_bl_title:
        if _is_forwarder:
            log.info(
                "  Carrier '%s' is a known FORWARDER/NVOCC → keeping HBL, NOT upgrading to MBL",
                carrier_name,
            )
        else:
            for carrier_key, scac in _CARRIER_TO_SCAC.items():
                if carrier_key in carrier_name:
                    log.info("  Ocean carrier '%s' detected → upgrading HBL → MBL", carrier_name)
                    doc_type = "MASTER BILL OF LADING"
                    # Prepend SCAC if reference doesn't already have one
                    if reference_number and reference_number[:4] not in _KNOWN_SCAC_PREFIXES:
                        reference_number = scac + reference_number
                        log.info("  Prepended SCAC %s → %s", scac, reference_number)
                    break
    elif doc_type == "HOUSE BILL OF LADING" and carrier_name and _is_non_bl_title:
        log.info(
            "  Ocean carrier '%s' detected but doc_title='%s' suggests non-BL notice "
            "→ NOT upgrading to MBL, classifying as ADDITIONAL FILES",
            carrier_name, doc_title,
        )
        doc_type = "ADDITIONAL FILES"

    # ── Downgrade safety net: MBL requires a real ocean carrier ──────
    # Master Bills can ONLY be issued by an actual ocean carrier (never a
    # forwarder/NVOCC/trading company) — mirrors GATE 0 in the prompt.
    # If Gemini classified this straight as MBL but the carrier_name it
    # extracted doesn't match any carrier in _CARRIER_TO_SCAC (or no
    # carrier_name was extracted at all), it cannot legitimately be a
    # Master Bill — force it back to HOUSE BILL OF LADING. This enforces
    # the rule deterministically instead of trusting Gemini to have
    # followed GATE 0 correctly.
    # A valid SCAC-prefixed reference number counts as alternate proof of
    # carrier issuance (Gemini sometimes reads the ref correctly but
    # misses the logo/carrier_name), so it exempts a doc from downgrade.
    # carrier_name is matched against BOTH the full name and the SCAC code
    # (e.g. "MAEU") — some carriers (Maersk in particular) only print the
    # SCAC code in the masthead ("SCAC MAEU"), never the spelled-out name,
    # so a name-only check was wrongly downgrading genuine Maersk MBLs.
    if doc_type == "MASTER BILL OF LADING":
        _matched_carrier = bool(carrier_name and (
            any(k in carrier_name for k in _CARRIER_TO_SCAC) or
            any(v in carrier_name for v in _CARRIER_TO_SCAC.values())
        ))
        _matched_scac_ref = bool(
            reference_number and reference_number[:4].upper() in _KNOWN_SCAC_PREFIXES
        )
        if not _matched_carrier and not _matched_scac_ref:
            log.info(
                "  doc_type=MASTER BILL OF LADING but carrier_name='%s' matches no known "
                "ocean carrier and ref='%s' has no known SCAC prefix → downgrading to "
                "HOUSE BILL OF LADING",
                carrier_name, reference_number,
            )
            doc_type = "HOUSE BILL OF LADING"

    # ── Secondary MBL check via SCAC prefix ──────────────────────────
    # SKIP this check if the document was issued by a known forwarder/NVOCC.
    # A Green Logistics HBL will naturally carry an HLCU-prefixed carrier BL
    # number in its reference field — that prefix belongs to the ocean carrier,
    # NOT to Green Logistics, so it must NOT trigger an MBL upgrade.
    if doc_type == "HOUSE BILL OF LADING" and reference_number and not _is_forwarder:
        prefix4 = reference_number[:4].upper()
        if prefix4 in _KNOWN_SCAC_PREFIXES:
            log.info("  SCAC prefix '%s' → upgrading HBL → MBL", prefix4)
            doc_type = "MASTER BILL OF LADING"

    # ── Direct-MBL safety net: prepend SCAC if missing ───────────────
    # The two blocks above only fix the reference when Gemini first said HBL
    # and got upgraded to MBL. If Gemini already returned MASTER BILL OF
    # LADING directly (e.g. a CMA CGM waybill numbered "GQL0462680" with no
    # "CMDU" prefix), reference_number never passes through that upgrade
    # path, so it stayed unprefixed. Catch that case here too.
    if doc_type == "MASTER BILL OF LADING" and reference_number and carrier_name:
        prefix4 = reference_number[:4].upper()
        if prefix4 not in _KNOWN_SCAC_PREFIXES:
            for carrier_key, scac in _CARRIER_TO_SCAC.items():
                if carrier_key in carrier_name:
                    reference_number = scac + reference_number
                    log.info(
                        "  Direct MBL missing SCAC — carrier '%s' → prepended %s → %s",
                        carrier_name, scac, reference_number,
                    )
                    break

    # ── BKK- prefix safety net: force HOUSE BILL OF LADING ────────────
    # Bangkok-based freight forwarders (e.g. "Amazing Logistics and Supply
    # Chain Co., Ltd") issue FIATA multimodal B/Ls numbered "BKK-xxxxxxxx".
    # Gemini sometimes classifies these as MASTER BILL OF LADING even though
    # a forwarder issued them. A BKK- prefixed number is never a real ocean
    # carrier's Master B/L number (those carry the carrier's own SCAC
    # prefix), so force it back to HOUSE BILL OF LADING regardless of what
    # the upgrade logic above decided.
    if doc_type == "MASTER BILL OF LADING" and reference_number and reference_number.upper().startswith("BKK-"):
        log.info(
            "  Reference '%s' starts with BKK- (forwarder-issued FIATA B/L) "
            "→ forcing MASTER BILL OF LADING → HOUSE BILL OF LADING",
            reference_number,
        )
        doc_type = "HOUSE BILL OF LADING"

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