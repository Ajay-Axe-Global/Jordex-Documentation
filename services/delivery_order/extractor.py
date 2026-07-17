"""
delivery_order.py — Delivery Order Extractor
=============================================
Extracts from a Delivery Order PDF:
  - MBL / carrier / container numbers
  - Pickup address + reference
  - Return (empty depot) address + reference

Called by extractor.py → process_document() when doc_type == "delivery_order".
"""

import base64, json, os, re, logging
from datetime import datetime

log = logging.getLogger("delivery_order")

# ── Carrier SCAC lookup ──────────────────────────────────────────────
CARRIER_SCAC = {
    "hapag": "HLCU", "hapag-lloyd": "HLCU", "hapag lloyd": "HLCU",
    "maersk": "MAEU",
    "msc": "MSCU", "mediterranean shipping": "MSCU",
    "one": "ONEY", "ocean network express": "ONEY",
    "yang ming": "YMLU",
    "evergreen": "EGLV",
    "cosco": "COSU",
    "oocl": "OOLU",
    "zim": "ZIMU",
    "cma cgm": "CMDU", "cma": "CMDU",
    "hmm": "HDMU", "hyundai": "HDMU",
    "wan hai": "WHLC",
    "pil": "PCIU",
    "fps": "FPS", "famous pacific": "FPS", "famous pacific shipping": "FPS",
}
KNOWN_SCAC = set(CARRIER_SCAC.values())
CONTAINER_RE = re.compile(r'\b([A-Z]{4})\s*(\d{7})\b')


# ══════════════════════════════════════════════════════════════════════
#  PROMPTS
# ══════════════════════════════════════════════════════════════════════

_PICKUP_RETURN_RULES = """
PICKUP/RETURN EXTRACTION — apply ONLY the rule block matching the carrier
you identified above. Ignore all other blocks.

*** GLOBAL RULE FOR ALL CARRIERS ***
If ANY container numbers are present in the document (even if there is only ONE container), you MUST set BOTH pickup.reference_mode and return.reference_mode to "per_container". 
When using "per_container", you MUST populate the 'references' array with an object for EVERY container listed. Each object in the array MUST include:
- "container_no": the exact container number.
- "reference": the specific PIN/Reference code for that container (or null/PCS based on carrier rules).
- "address": the specific address for that container. If all containers share the same main address, you MUST copy that main address into the "address" field for every single container in the array.
Only use "single" mode if the document literally has zero container numbers mentioned anywhere.

── IF CARRIER = Hapag-Lloyd (HLCU) ──
- pickup.address = value of field labeled "Container Place of Availability".
- pickup.reference_mode = "per_container". Each container in the container
  table has its own "Reference" value. Look closely at the line directly underneath
  the container number. It says "Reference: [value]". Extract this exact numeric
  or alphanumeric value (e.g. "24450131").
  Do NOT confuse the reference with the "Pick up by" company name on the right.
  If the Reference is empty, contains the word "PORTBASE" (case-insensitive), or
  is just a company name (like "JORDEX SHIPP & FORW"), output "PCS" as the reference instead.
  Return one entry per container: {"container_no": "...", "reference": "..."}.
- return.address = the main "Empty Return Depot(s)" address if all containers
  share it, else null.
- return.reference_mode = "per_container", using the "Turn-In-Reference" value
  shown for each container. If empty, use "PCS". 
  IMPORTANT: You must output a consistent JSON shape for references. Every container 
  object in the 'references' array MUST include the 'address' field. If all containers 
  go to the same main Empty Return Depot, copy that main address into the 'address' 
  field for every single container.
  Format: {"container_no": "...", "reference": "...", "address": "specific depot address"}

── IF CARRIER = Maersk (MAEU) ──
Check if this document is an "Acknowledgement" (e.g. titled "Acknowledgement copy for delivery order request" or "Smart Inland Delivery request"):
  - If YES (Acknowledgement layout):
    - pickup.address = value of the "Pickup Site" field.
    pickup.reference = "Release Code" or "PIN" if present; if missing, use "PCS".
    - return.address = look for an empty depot or return location; if not mentioned explicitly, leave as null.
    return.reference = any explicitly stated return reference; if missing, leave as null.
  - If NO (Standard Delivery Order layout / Cargo Release Notice):
    - This document has an "Equipment" table containing container details, and a "Merchant Haulage Delivery Itinerary" table with "Full Delivery Pickup Terminal" (PICKUP) and "Empty Container Depot" (RETURN).
    - pickup.address = the "Name" cell of the "Full Delivery Pickup Terminal" row.
    - pickup.reference_mode = "per_container". For each container in the "Equipment" table, extract its specific "Pin". If the Pincode is blank/empty, leave it as null (do NOT substitute "PCS" or anything else).
    - return.address = the "Name" cell of the "Empty Container Depot" row (or from Haulage Instructions).
    - return.reference = Look at the "Haulage Instructions:" block. Extract the exact value written after "Reference:" (e.g. "A314221"). 
        - Do NOT hallucinate or substitute a general reference like "MAEMT". If it says "Reference: A314221", extract exactly "A314221".
        - If empty → leave return.reference as null.

── IF CARRIER = MSC (MSCU) ──
- pickup.address = field labeled "Terminal".
pickup.reference = Pincode if present,
  else use "PCS" (Portbase).
- return.address = field labeled "Depot".
return.reference = "Drop Off Ref" value
  if present. If missing, leave return.reference empty and set
  flag = "forward_to_jordex_import".

── IF CARRIER = COSCO (COSU) ──
- pickup.address = field labeled "Cargo Pickup Location".
pickup.reference = Pincode/PIN if
  present, else use "PCS" (Portbase).
- return.address = field labeled "Empty Return Location".
- return.reference: the "Turn In Reference" value for the container. If the
  return section lists multiple transport modes (e.g. "BY TRUCK=TRUCKCOSMT;
  BARGE=..."), extract ONLY the exact reference code for Truck (e.g.
  "TRUCKCOSMT"). Do NOT include the text for barge or rail. If empty/null,
  use "PCS".

── IF CARRIER = ONE Line (ONEY) ──
- pickup.address = the "Cargo Pick Up Loc" field. This field often has the format
  "NLRTM01 (ECT DELTA TERMINAL, ROTTERDAM)". Extract ONLY the human-readable name
  inside the parentheses — e.g. "ECT DELTA TERMINAL, ROTTERDAM".
  If there are no parentheses, use the full field value.
Check the "Secure Release Details" box:
  - If it contains a field explicitly labeled "PIN" with a numeric value → use that as pickup.reference.
  - If it shows only company name and SRI/country-prefixed code (e.g. "NL101595") with
    NO explicit PIN label → use "PCS" as pickup.reference.
  - Default: use "PCS".
- return.address: Look at "Empty Return Location". This field may say
  "NLRTM95 (ROTTERDAM OFF-HIRE FACILITY)" or similar. If the REMARKS/Notice section
  specifies a concrete depot (e.g. "ATO TERMINAL ANTWERP"), use that name instead.
  Otherwise extract the name in parentheses from "Empty Return Location".
Read the REMARKS / Notice section carefully for return reference codes.
  Look for text like "REF: [CODE]" or "OPEN REF.: '[CODE]'".
  Extract the exact code mentioned without modifying or guessing it.
  For example, if it says "REF: ONEMT", extract "ONEMT". 
  If it says "REF: ONE", extract "ONE".
  - If no reference code is explicitly found in the remarks → default to "ONEMT".

── IF CARRIER = Yang Ming (YMLU) ──
- pickup.address = field labeled "Discharging terminal".
pickup.reference = Pincode if present.
  If Pincode is "PORTBASE" (case-insensitive) or empty/null, use "PCS".
- return.address = field labeled "Turn in depot".
return.reference = "Turn in Reference"
  value if present. If it says something like "check with eqt@nl.yangming.com",
  or if it is empty/null, use "PCS".

── IF CARRIER = OOCL (OOLU) ──
- pickup.address = field labeled "Cargo Pickup Location".
pickup.reference = "PIN" value if
  present. If the PIN is blank, empty, or literally just the word "PIN" with no code, use "PCS" (Portbase).
- return.address = field labeled "Empty Return Location".
- return.reference: read the "REMARKS" section. It lists lines like
  "20GP please return to: X - CONTAINER NUMBER", "40GP = EUROMAX", etc.
  Find the line whose container-size code (e.g. "40HQ") matches this
  shipment's actual container size/type, and whose instruction ends in
  "CONTAINER NUMBER" — if it does, return.reference = the shipment's own
  ACTUAL container number (the real value from the container table, never
  the literal words "CONTAINER NUMBER").

── IF CARRIER = HMM (HDMU) ──
- pickup.address = field labeled "Cargo Release Facility".
pickup.reference = PIN value if
  present, else "PCS" (the literal string "PCS", do NOT use the numeric package/PCS count).
- return.address = field labeled "EQ Return Facility Name".
return.reference = "Turn-In Ref" value
  if present; if missing, use the container type/size (e.g. "40HQ", "20DC") instead. If no reference is found at all, use "PCS".

── IF CARRIER = CMA CGM (CMDU) ──
- pickup.address = field labeled "QUAY / TERMINAL".
Look for a field/row labeled "Pincode" or "PIN" in the document.
  - If a Pincode value is present → pickup.reference = that Pincode value.
  - If NO Pincode is present (field is empty, absent, or says "Portbase"/"PCS") →
    pickup.reference = "PCS" (the literal string, NOT any numeric value from the
    container table such as PCS/QTY count or NET WT weight).
  CRITICAL: Do NOT use numbers like 23350.000 or 1000 as the pickup reference.
  The only valid non-PCS pickup reference for CMA CGM is an explicit Pincode.
- return.address = the "EMPTY RETURN ADDRESS" block/table near the bottom.
  Use the address text from the leftmost column.
- return.reference: look at the table in the EMPTY RETURN ADDRESS section.
  This table has the columns:
    EMPTY RETURN ADDRESS | CONTAINERS | Turn-In-Ref | D&D Invoice
  Read the "Turn-In-Ref" column carefully for EACH individual container.
  It contains the return reference code (e.g. "CMAREEFER20", "CMADRY26", or "CMA STOCK"). 
  Extract this value exactly for each matching container.
  - If a Turn-In-Ref value is present next to the container (e.g. "CMA STOCK") → return.reference = that exact value.
  - If the Turn-In-Ref value for a specific container is blank/empty → return.reference = null.
  - If ALL containers have empty references → set flag = "forward_to_client".

── IF CARRIER = ZIM (ZIMU) ──
- pickup.address = field labeled "Pick up terminal".
pickup.reference = Pincode value.
- return.address = the "Empty return" field (e.g. "Kramer Delta").
return.reference = "Empty Ref" value if
  present. If missing, leave return.reference empty and set
  flag = "forward_to_jordex".

── IF CARRIER = FPS ──
Do NOT extract pickup or return at all. Set pickup and return to null,
and set flag = "skip_extraction". FPS documents are upload-only.

── IF CARRIER = anything else / cannot be determined ──
Extract pickup/return using the document's own field labels as best you
can, and set flag = "needs_manual_review".
"""

DELIVERY_ORDER_PROMPT = f"""You are a logistics document parser. This is a carrier
Delivery Order. Extract BOTH of the following from this ONE PDF:
  (A) MBL / carrier / container numbers
  (B) Pickup address+reference and Return address+reference

Return ONLY a valid JSON object (no markdown, no backticks, no extra text):

{{
  "mbl": "Master Bill of Lading number or null",
  "carrier_name": "shipping carrier/line name visible in the document or null",
  "carrier_logo_present": true or false,
  "containers": ["list of container numbers"],
  "pickup": {{
    "address": "full pickup address text, or null",
    "reference_mode": "single" or "per_container",
    "reference": "reference value if reference_mode is 'single', else null",
    "references": [{{"container_no": "...", "reference": "...", "address": "container-specific address (or main address if same for all)"}}]
  }},
  "return": {{
    "address": "full return address text, or null",
    "reference_mode": "single" or "per_container",
    "reference": "reference value if reference_mode is 'single', else null",
    "references": [{{"container_no": "...", "reference": "...", "address": "container-specific address (or main address if same for all)"}}]
  }},
  "flag": "null, or one of: forward_to_client, forward_to_jordex, forward_to_jordex_import, needs_manual_review, skip_extraction"
}}

═══ PART A — CRITICAL RULES FOR MBL ═══
1. MBL (Master Bill of Lading / B/L) is the PRIMARY shipping reference number.
2. A REAL MBL ALWAYS starts with a 4-letter carrier SCAC code: HLCU (Hapag-Lloyd),
   MAEU/MRKU (Maersk), MSCU/MEDU (MSC), ONEY (ONE), YMLU (Yang Ming), EGLV
   (Evergreen), COSU (COSCO), OOLU (OOCL), ZIMU (ZIM), CMDU (CMA CGM), HDMU (HMM).
3. Example REAL MBLs: HLCUSZX2605APPZ0, MAEU123456789, MEDUJS977760, ONEYSHA12345678.
4. If you see a reference number WITHOUT a carrier SCAC prefix, it is NOT an MBL.
   Set mbl to null.
5. Customs declaration numbers (Aangiftenummer), LRN numbers, dossier numbers,
   booking references are NOT MBLs. Do NOT return these as mbl.
6. Only set carrier_name if you can see a shipping line name or logo
   (Hapag-Lloyd, Maersk, MSC, etc.) in the document.
7. Only set carrier_logo_present to true if a carrier company logo image is visible.
8. Container numbers format: exactly 4 uppercase letters + 7 digits
   (e.g. BEAU2199630, HLXU1114191).
9. If no valid MBL is found, set mbl to null. Do NOT guess or fabricate.

═══ PART B — {_PICKUP_RETURN_RULES.strip()}

Rules for pickup/return JSON shape:
- If reference_mode is "single", fill "reference" and leave "references" as [].
- If reference_mode is "per_container", fill "references" and leave "reference" null.
- Never invent a value. If a rule says leave it empty, use null / empty string.

═══ PART C — EXCLUSIONS / SKIP ═══
- INVOICES: Sometimes an invoice is mistakenly filed as a Delivery Order. If the document explicitly says "INVOICE" or "INVOICE NO." (e.g. Hapag-Lloyd invoices with an "OE..." reference indicating Ocean Export), do NOT attempt to parse it as a Delivery Order. Set `flag` = "skip_extraction" and leave all other fields as null/empty arrays.

First identify if the document should be skipped (Part C). If not, identify the carrier from Part A, THEN apply the matching Part B rule block.
"""


# ══════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════

def _validate_mbl(mbl: str, carrier_name: str, logo_present: bool) -> str | None:
    if len(mbl) < 10:
        return None
    prefix4 = mbl[:4]
    if prefix4 in KNOWN_SCAC:
        return mbl
    if carrier_name and logo_present:
        carrier_lower = carrier_name.lower().strip()
        for key, scac in CARRIER_SCAC.items():
            if key in carrier_lower:
                combined = scac + mbl
                log.info("  Adding SCAC prefix: %s + %s = %s", scac, mbl, combined)
                return combined
    return None


def _scac_from_carrier_name(carrier_name: str) -> str | None:
    if not carrier_name:
        return None
    carrier_lower = carrier_name.lower().strip()
    for key, scac in CARRIER_SCAC.items():
        if key in carrier_lower:
            return scac
    return None


def _apply_safety_net(scac: str, result: dict) -> dict:
    """Re-applies hard-coded defaults in case Gemini forgets a rule."""
    ret = result.get("return") or {}

    if scac == "MAEU":
        if ret.get("reference_mode", "single") == "single" and not (ret.get("reference") or "").strip():
            addr = (ret.get("address") or "").lower()
            ret["reference"] = "MAERSKSTACK" if "star container" in addr else "MAEMT"
            result["return"] = ret
            log.info("Maersk safety-net: return reference defaulted to '%s'", ret["reference"])

    elif scac == "MSCU":
        if not (ret.get("reference") or "").strip():
            result["flag"] = result.get("flag") or "forward_to_jordex_import"

    elif scac == "CMDU":
        pickup = result.get("pickup") or {}
        r_pickup = (pickup.get("reference") or "").strip()
        # If Gemini returned a numeric value (weight/qty) instead of a Pincode
        # or PCS, replace it with PCS.
        def _looks_numeric(s):
            try: float(s.replace(",", "")); return True
            except: return False
        if not r_pickup or r_pickup in ("NONE", "NULL") or _looks_numeric(r_pickup):
            pickup["reference"] = "PCS"
            result["pickup"] = pickup
            log.info("CMDU safety-net: pickup reference set to 'PCS' (was %r)", r_pickup or None)

        ret = result.get("return") or {}
        r_ret = (ret.get("reference") or "").strip()
        if not r_ret or r_ret in ("NONE", "NULL"):
            # Gemini missed Turn-In-Ref — scan raw PDF text for CMA-style codes
            pdf_path = result.get("_pdf_path", "")
            if pdf_path:
                try:
                    import pdfplumber
                    with pdfplumber.open(pdf_path) as _pdf:
                        raw = " ".join(p.extract_text() or "" for p in _pdf.pages)
                    # Look for Turn-In-Ref patterns: CMADRY, CMAREEFER, etc.
                    m = re.search(r'Turn[- ]In[- ]Ref[:\s]+([A-Z0-9]{4,20})', raw, re.IGNORECASE)
                    if not m:
                        # Also try column-adjacent pattern: digits then space then CMA-code
                        m = re.search(r'\b(CMA[A-Z0-9]{3,15})\b', raw, re.IGNORECASE)
                    if m:
                        ret["reference"] = m.group(1).strip().upper()
                        result["return"] = ret
                        log.info("CMDU safety-net: Turn-In-Ref extracted from text: %s", ret["reference"])
                    else:
                        result["flag"] = result.get("flag") or "forward_to_client"
                except Exception as e:
                    log.warning("CMDU safety-net text scan failed: %s", e)
                    result["flag"] = result.get("flag") or "forward_to_client"
            else:
                result["flag"] = result.get("flag") or "forward_to_client"

    elif scac == "ZIMU":
        if not (ret.get("reference") or "").strip():
            result["flag"] = result.get("flag") or "forward_to_jordex"

    elif scac == "HLCU":
        pickup = result.get("pickup") or {}
        if pickup.get("references"):
            for ref in pickup["references"]:
                r = (ref.get("reference") or "").strip().upper()
                if not r or "JORDEX" in r or "PORTBASE" in r or r in ("NONE", "NULL"):
                    ref["reference"] = "PCS"
            result["pickup"] = pickup
        if ret.get("references"):
            for ref in ret["references"]:
                r = (ref.get("reference") or "").strip().upper()
                if not r or "PORTBASE" in r or r in ("NONE", "NULL"):
                    ref["reference"] = "PCS"
            result["return"] = ret

    elif scac == "COSU":
        if ret.get("reference"):
            r = ret["reference"].strip().upper()
            if "TRUCK=" in r:
                m = re.search(r'TRUCK=([A-Z0-9]+)', r)
                if m:
                    ret["reference"] = m.group(1)
                    log.info("COSU safety-net: extracted TRUCK ref %s", m.group(1))
            elif not r or r in ("NONE", "NULL"):
                ret["reference"] = "PCS"
            result["return"] = ret
        elif not ret.get("reference"):
            ret["reference"] = "PCS"
            result["return"] = ret

    elif scac == "YMLU":
        pickup = result.get("pickup") or {}
        r_pickup = (pickup.get("reference") or "").strip().upper()
        if not r_pickup or "PORTBASE" in r_pickup or r_pickup in ("NONE", "NULL"):
            pickup["reference"] = "PCS"
            result["pickup"] = pickup
        r_ret = (ret.get("reference") or "").strip().upper()
        if not r_ret or r_ret in ("NONE", "NULL"):
            ret["reference"] = "PCS"
            result["return"] = ret

    elif scac == "ONEY":
        pickup = result.get("pickup") or {}
        r_pickup = (pickup.get("reference") or "").strip().upper()
        # SRI codes like "NL101595" are not PINs — replace with PCS
        if not r_pickup or r_pickup in ("NONE", "NULL") or re.match(r'^[A-Z]{2}\d+$', r_pickup):
            pickup["reference"] = "PCS"
            result["pickup"] = pickup
            log.info("ONEY safety-net: pickup reference set to 'PCS'")

        r_ret = (ret.get("reference") or "").strip().upper()
        if not r_ret or r_ret in ("NONE", "NULL"):
            ret["reference"] = "ONEMT"
            result["return"] = ret
            log.info("ONEY safety-net: return reference defaulted to 'ONEMT'")

    elif scac == "FPS":
        result["pickup"] = None
        result["return"] = None
        result["flag"] = "skip_extraction"

    return result


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
    containers = list(set(f"{m.group(1)}{m.group(2)}" for m in CONTAINER_RE.finditer(text)))
    result["containers"] = containers
    for scac in KNOWN_SCAC:
        pattern = re.compile(rf'\b({re.escape(scac)}[A-Z0-9]{{6,}})\b')
        hits = pattern.findall(text)
        if hits:
            result["mbl"] = hits[0]
            result["folder_name"] = hits[0]
            result["scac"] = scac
            return result
    if containers:
        result["folder_name"] = containers[0]
    return result


# ══════════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════

def extract_delivery_order(pdf_path: str, gemini_model) -> dict:
    """
    Single Gemini call to extract everything a Delivery Order needs.
    Returns: mbl, carrier_name, scac, containers, pickup{}, return{},
             flag, folder_name, doc_type, source_file, extracted_at.
    """
    result = {
        "doc_type": "delivery_order",
        "mbl": None, "carrier_name": None, "scac": None, "containers": [],
        "pickup": {"address": None, "reference_mode": "single", "reference": None, "references": []},
        "return": {"address": None, "reference_mode": "single", "reference": None, "references": []},
        "flag": None,
        "source_file": os.path.basename(pdf_path),
        "extracted_at": datetime.now().isoformat(),
        "folder_name": None,
    }

    try:
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        resp = gemini_model.generate_content(
            [
                {"mime_type": "application/pdf", "data": base64.b64encode(pdf_bytes).decode()},
                DELIVERY_ORDER_PROMPT,
            ],
            generation_config={"temperature": 0.1, "max_output_tokens": 1500},
        )

        raw = resp.text.strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        parsed = json.loads(raw)
        log.info("  DO Gemini raw: %s", parsed)

        mbl = (parsed.get("mbl") or "").strip()
        carrier = (parsed.get("carrier_name") or "").strip()
        logo_present = parsed.get("carrier_logo_present", False)
        containers = [re.sub(r'\s+', '', c).upper() for c in (parsed.get("containers") or []) if c]

        result["carrier_name"] = carrier or None
        result["containers"] = containers
        result["scac"] = _scac_from_carrier_name(carrier)

        if mbl:
            mbl = re.sub(r'\s+', '', mbl).upper()
            validated = _validate_mbl(mbl, carrier, logo_present)
            if validated:
                result["mbl"] = validated
                result["folder_name"] = validated
                log.info("  Validated MBL: %s", validated)
            else:
                log.info("  Rejected MBL candidate: %s", mbl)

        if not result["folder_name"] and containers:
            valid = [c for c in containers if re.fullmatch(r'[A-Z]{4}\d{7}', c)]
            if valid:
                result["folder_name"] = valid[0]

        result["pickup"] = {**result["pickup"], **(parsed.get("pickup") or {})}
        result["return"] = {**result["return"], **(parsed.get("return") or {})}
        result["flag"] = parsed.get("flag")
        result["_pdf_path"] = pdf_path   # used by CMDU safety-net for Turn-In-Ref scan
        result = _apply_safety_net(result["scac"] or "", result)
        result.pop("_pdf_path", None)    # remove internal field before returning

    except json.JSONDecodeError as e:
        log.warning("  DO JSON parse failed: %s", e)
        return _regex_fallback(pdf_path, result)
    except Exception as e:
        log.warning("  DO extraction failed: %s", e)
        return _regex_fallback(pdf_path, result)

    return result
