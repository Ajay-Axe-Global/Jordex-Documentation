import base64, json, os, re, logging
from datetime import datetime

log = logging.getLogger("delivery_order")

# ── Carrier SCAC lookup ──────────────────────────────────────────────
CARRIER_SCAC = {
    "hapag": "HLCU", "hapag-lloyd": "HLCU", "hapag lloyd": "HLCU",
    "maersk": "MAEU",
    "msc": "MSCU", "mediterranean shipping": "MSCU",
    "one": "ONEY", "ocean network express": "ONEY",
    "yang ming": "YMJA",
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
KNOWN_SCAC = set(CARRIER_SCAC.values()).union({"MEDU", "MRKU", "SUDU"})
CONTAINER_RE = re.compile(r'\b([A-Z]{4})\s*(\d{7})\b')

# ── Address normalization map ────────────────────────────────────────
# Common abbreviations → full terminal names used in Jordex address book
ADDRESS_ABBREVIATIONS = {
    "RWG":          "Rotterdam World Gateway",
    "ECT":          "ECT Delta Terminal",
    "ECT DELTA":    "ECT Delta Terminal",
    "APM":          "APM Terminals",
    "APM2":         "APM 2 Terminal Maasvlakte II",
    "APMT":         "APM Terminals",
    "APMT MVII":    "APM 2 Terminal Maasvlakte II",
    "EUROMAX":      "Euromax Terminal",
    "EMX":          "Euromax Terminal",
    "RST":          "RST Zuid Terminal",
    "KRAMER":       "Kramer Delta",
    "RCT":          "Kramer Delta",
    "UWT":          "UWT Depot",
    "MED":          "MED (Smirnoffweg)",
    "HUTCHISON":    "Hutchison Ports Delta",
    "HPD":          "Hutchison Ports Delta",
}


# ══════════════════════════════════════════════════════════════════════
#  CALL 1 — CARRIER + DOC SUBTYPE IDENTIFICATION
# ══════════════════════════════════════════════════════════════════════

IDENTIFY_PROMPT = """Look at this shipping document and return ONLY a JSON object:

{
  "carrier_name": "shipping line (Maersk, Hapag-Lloyd, MSC, ONE, CMA CGM, COSCO, OOCL, HMM, Yang Ming, ZIM, FPS, etc.)",
  "carrier_logo_present": true/false,
  "doc_subtype": "delivery_order" or "acknowledgement" or "invoice" or "other",
  "mbl": "Bill of Lading number starting with SCAC code, or empty string",
  "containers": ["XXXX1234567 format"]
}

doc_subtype rules:
- "delivery_order": has title like "DELIVERY ORDER", "Customer Release", "Release Order",
  "Container Release Notification", "LAAT VOLGEN", "Release Notification", "Delivery Order Amendment",
  "release message". The actual release document with container details, pickup/return info.
- "acknowledgement": title says "Acknowledgement copy for delivery order request" or
  "Delivery Order request" or "Smart Inland Delivery request". A request confirmation, NOT the release.
- "invoice": explicitly says "INVOICE" or "INVOICE NO." as the document type.
- "other": anything else.

MBL: starts with SCAC (HLCU, MAEU, MRKU, MSCU, MEDU, ONEY, YMLU, EGLV, COSU, OOLU, ZIMU, CMDU, HDMU).
*EXCEPTION*: For Maersk (MAEU), the B/L number is often purely numeric (e.g., "270557106"). If you see "B/L number: [digits]", extract exactly those digits. Do NOT extract the "Request Number" (like "HZJQCNSXZ5K") as the MBL.
Container: 4 uppercase letters + 7 digits. Use "" for missing values. Pure JSON only, no markdown."""


# ══════════════════════════════════════════════════════════════════════
#  CALL 2 — CARRIER-SPECIFIC EXTRACTION PROMPTS
# ══════════════════════════════════════════════════════════════════════

_GENERAL_RULES = """
CRITICAL ADDRESS FORMATTING RULES (apply to ALL carriers):
- For pickup/return "address" fields, extract ONLY the terminal/depot NAME.
- If format is "CODE (TERMINAL NAME, CITY)" → extract ONLY the terminal name inside parentheses.
  Example: "NLRTM01 (ECT DELTA TERMINAL, ROTTERDAM)" → "ECT DELTA TERMINAL"
  Example: "NLRTM24 (KRAMER TERMINAL (ONE DEDICATED DEPOT), ROTTERDAM)" → "KRAMER TERMINAL (ONE DEDICATED DEPOT)"
- REMOVE these from the address:
  → Location codes: NLRTM01, NLRTM24, BEANR01, etc.
  → City names: ROTTERDAM, ANTWERP, AMSTERDAM, KAMPEN, etc.
  → Country names: NETHERLANDS, BELGIUM, GERMANY, etc.
  → Street addresses, postal codes, phone numbers, haven numbers
- KEEP descriptive suffixes: "(ONE DEDICATED DEPOT)", "(Depot 2)", "BV / DDE"
- The address should be JUST the terminal name as it appears on a sign.
  Good: "ECT DELTA TERMINAL", "KRAMER DISTRIPARK DEPOTS", "EUROMAX TERMINAL ROTTERDAM"
  Bad: "NLRTM01 (ECT DELTA TERMINAL, ROTTERDAM)", "Europaweg 875 Rotterdam NL"
 
ADDRESS FORMAT — COMMA-SEPARATED STRUCTURE:
- ALL address fields MUST use commas to separate the parts:
  "TERMINAL NAME, STREET NUMBER, POSTAL CODE CITY"
- Example of WRONG: "BTT MULTIMODAL CONTAINER SOLUTIONS BV GEMINIWEG 51 5015 BP TILBURG NETHERLANDS"
- Example of RIGHT: "BTT MULTIMODAL CONTAINER SOLUTIONS BV, GEMINIWEG 51, 5015 BP TILBURG"
- The FIRST part (before first comma) must be ONLY the terminal/depot NAME.
- The SECOND part is the street + house number.
- The THIRD part is the postal code + city.
- REMOVE country names (NETHERLANDS, BELGIUM, GERMANY) entirely from the address.
- If only a terminal name is known (no street/postal), return just the name: "ECT DELTA TERMINAL"
 
CRITICAL — ADDRESS vs REFERENCE SEPARATION:
- "address" fields must contain ONLY real physical terminal/depot names.
- Default reference codes are NOT addresses. NEVER put these in an address field:
  ONEMT, MAEMT, MAERSKSTACK, PCS, PORTBASE, TRUCKCOSMT, BARGECOSMT,
  RAILCOSMT, CMA STOCK, SECURE CHAIN, or any carrier-specific default code.
- If no pickup/return terminal or depot is visible in the document → address = "".
- If the document says "TO FOLLOW" or "TBA" for a location → address = "".
- Default reference codes go ONLY in the "reference" field, never in "address".
  Example of WRONG: {"address": "ONEMT", "reference": "ONEMT"}
  Example of RIGHT: {"address": "",      "reference": "ONEMT"}
 
MISSING DATA RULES:
- If the container table is empty or no container numbers are visible → "references": [].
  Do NOT fabricate container numbers. Return an empty array.
- If a section (pickup or return) has no data at all in the document:
  → {"address": "", "references": []}
- NEVER fill address with a reference code just because the address is missing.
  Missing address = "", missing reference = use carrier default (PCS, ONEMT, etc.).
"""

_OUTPUT_SCHEMA = _GENERAL_RULES + """Return ONLY valid JSON (no markdown, no backticks):
{
  "pickup": {
    "address": "terminal name",
    "references": [{"container_no": "XXXX1234567", "reference": "value", "address": "terminal name"}]
  },
  "return": {
    "address": "depot name",
    "references": [{"container_no": "XXXX1234567", "reference": "value", "address": "depot name"}]
  },
  "flag": ""
}
RULES:
- One entry per container in BOTH pickup.references and return.references.
- Copy the shared address into every entry's "address" field.
- Use "" for missing values. NEVER write "null".
"""

CARRIER_PROMPTS = {}

# ─────────────────────────────────────────────────────────────────────
# 1. HAPAG-LLOYD (HLCU) — "Customer Release"
# ─────────────────────────────────────────────────────────────────────
CARRIER_PROMPTS["HLCU"] = _OUTPUT_SCHEMA + """
This is a Hapag-Lloyd "Customer Release" document.

PICKUP:
- address = "Container Place of Availability" (the orange/highlighted box, top-right area).
  Example: "IJSSEL DELTA TERMINAL, OSLOKADE 9, 8263 CH KAMPEN"
- reference per container: look at each container row. Directly below each container number
  there is a line "Reference: XXXXXX" (e.g. "6K4TN6NMY2"). Extract this exact value.
  → Do NOT confuse with "Pick up by" company name on the right side.
  → If reference is empty, says "PORTBASE", or is a company name like "JORDEX" → use "PCS".

RETURN:
- address = "Empty Return Depots:" section at the bottom. Extract the depot name.
  Example: "IJSSEL DELTA TERMINAL, OSLOKADE 9, 8263 CH KAMPEN"
- reference per container: "Turn-In-Reference" value shown per container.
  → If empty → "PCS".
"""

# ─────────────────────────────────────────────────────────────────────
# 2. MAERSK (MAEU) — "DELIVERY ORDER" / "DELIVERY ORDER AMENDMENT"
# ─────────────────────────────────────────────────────────────────────
CARRIER_PROMPTS["MAEU"] = _OUTPUT_SCHEMA + """
This is a Maersk Delivery Order. It has 3 key areas to read:

BOX 1 — EQUIPMENT TABLE (top):
  Contains columns: Equipment No, Size/Type, Pin, etc.
  Read the container numbers and the "Pin" column value for each.

BOX 3 — MERCHANT HAULAGE DELIVERY ITINERARY (middle):
  Two rows:
  - "Full Delivery Pickup Terminal" → this is the PICKUP address.
    Read the "Name" column and address lines below it.
  - "Empty Container Depot" → this is the RETURN address.
    Read the "Name" column and address lines below it.

HAULAGE INSTRUCTIONS (bottom):
  Text block like "truck with ref: TR40HCMSK" or "Reference: A314221".
  This contains the RETURN reference.

PICKUP:
- address = the name from "Full Delivery Pickup Terminal" row.
- reference per container = the "Pin" column from the Equipment table.
  → If Pin is BLANK/EMPTY → reference = "" (empty string).
  → NEVER use "PCS" for Maersk pickup. Only actual pin or "".

RETURN:
- address = the name from "Empty Container Depot" row.
- reference = from "Haulage Instructions:" block, extract ONLY the truck reference.
  Example: "truck with ref: TR40HCMSK / APM2 via barge with ref: BA40HCMSK" → use "TR40HCMSK".
  → If Haulage Instructions has "Reference: XXXXX" → use that exact value.
  → If NO Haulage Instructions block exists → use "MAEMT" as default.
  → SPECIAL: if the return depot name contains "Star Container" → use "MAERSKSTACK" instead.

Apply the SAME return reference to ALL containers (it's shipment-level, not per-container).
"""

# ─────────────────────────────────────────────────────────────────────
# 3. COSCO (COSU) — "LAAT VOLGEN"
# ─────────────────────────────────────────────────────────────────────
CARRIER_PROMPTS["COSU"] = _OUTPUT_SCHEMA + """
This is a COSCO "LAAT VOLGEN" (Delivery Order).

PICKUP:
- address = "CARGO PICKUP LOCATION" section (bottom-left area).
  Example: "Euromax Terminal, Maasvlaktesweg 951, 3199 LZ Rotterdam"
- reference per container = "PIN" column value in the container table.
  → If PIN is present → use it.
  → If PIN is blank or says "Portbase" → use "PCS".

RETURN:
- address = "EMPTY RETURN LOCATION" column in the container table.
  Example: "Euromax Terminal Maasvlaktesweg 951"
- reference per container = "TURN IN REFERENCE" column.
  If it lists multiple modes (e.g. "BY TRUCK=TRUCKCOSMT; BARGE=BARGECOSMT; RAIL=..."):
  → Extract ONLY the TRUCK value (e.g. "TRUCKCOSMT").
  → Ignore barge/rail values.
  → If empty → "PCS".
"""

# ─────────────────────────────────────────────────────────────────────
# 4. FPS — Upload only, no extraction needed
# ─────────────────────────────────────────────────────────────────────
CARRIER_PROMPTS["FPS"] = """Return ONLY: {"pickup": null, "return": null, "flag": "skip_extraction"}
This is an FPS document. No pickup/return extraction needed. Upload only."""

# ─────────────────────────────────────────────────────────────────────
# 5. YANG MING (YMLU) — "release message"
# ─────────────────────────────────────────────────────────────────────
CARRIER_PROMPTS["YMLU"] = _OUTPUT_SCHEMA + """
This is a Yang Ming release message.

PICKUP:
- address = "Discharging terminal" field (e.g. "Rotterdam World Gateway - Port number 8970").
- reference per container = "Pincode" column in the container table.
  → If Pincode says "PORTBASE" (case-insensitive) or is empty → use "PCS".
  → If an actual pincode value exists → use it.

RETURN:
- address = "Turn in depot" column in the container table.
  Example: "QTerminals Kramer Rotterdam(RCT)- Missouriweg 17, Port number 7220"
- reference per container = "Turn in Reference" column.
  → If a value exists (e.g. "FE12618W") → use it.
  → If it says "check with eqt@..." or is empty → use "PCS".
"""

# ─────────────────────────────────────────────────────────────────────
# 6. OOCL (OOLU) — "DELIVERY ORDER"
# ─────────────────────────────────────────────────────────────────────
CARRIER_PROMPTS["OOLU"] = _OUTPUT_SCHEMA + """
This is an OOCL Delivery Order.

PICKUP:
- address = "CARGO PICKUP LOCATION" section (left side, below container table).
  Example: "Euromax Terminal Rotterdam BV, Maasvlakte weg 951"
- reference per container = Look for "PIN" at the top of the document or in the table.
  → If a pincode value exists → use it.
  → If blank or no PIN field → use "PCS".

RETURN:
- address = "EMPTY RETURN LOCATION" section (middle, below container table).
  Example: "UWT Bunschotenweg (Depot 2)"
- reference per container = Read the "REMARKS" section carefully. It lists return rules
  per container size/type, like:
    "20GP = RCT Kramer Delta - CONTAINER NUMBER"
    "40GP = EUROMAX"
    "45HQ + 40HQ = UWT MAASVLAKTE - REFERENCE: CONTAINER NUMBER"
    "20RF + 40HQ = UWT DEPOT 2 - REFERENCE: CONTAINER NUMBER"
  Find the line that matches this container's SIZE/TYPE (from the table above).
  → If it says "CONTAINER NUMBER" → use the container's ACTUAL container number as the reference.
  → If it says a specific depot name without "CONTAINER NUMBER" → use that depot name.
  → If no matching rule → "".
"""

# ─────────────────────────────────────────────────────────────────────
# 7. HMM (HDMU) — "Delivery Order"
# ─────────────────────────────────────────────────────────────────────
CARRIER_PROMPTS["HDMU"] = _OUTPUT_SCHEMA + """
This is an HMM Delivery Order.

PICKUP:
- address = "Cargo Release Facility" field.
  Example: "Rotterdam World Gateway, Havennummer 8970 Amoeweg 50, 3199 KD MAASVLAKTE ROTTERDAM"
- reference per container = "PIN No." column in the "Container Information" table.
  → If it says "Secure Chain" or is empty → use "PCS".
  → If an actual PIN value exists → use it.
  CAREFUL: Do NOT confuse PIN with the package count or weight numbers.

RETURN:
- address = "EQ Return Facility Name" in the "* EQ Return Facility Information" section.
  The table shows: Facility name, Turn-In Ref, Phone No., Location.
  Example: "NLRTMWG Rotterdam World Gateway Havennummer 8970..."
  Extract the human-readable facility name (e.g. "Rotterdam World Gateway").
- reference per container = "Turn-In Ref" column in the EQ Return Facility table.
  → If a Turn-In Ref exists and is an actual code → use it.
  → CRITICAL: If the Turn-In Ref says "Contact HMM Netherlands" or similar instructions, use an EMPTY STRING "". Do NOT mistakenly extract numbers from the Facility Name (like "8970") as the reference.
  → If empty → check the container's size/type (e.g. "20DC", "40HQ") and use that as reference.
  → If nothing at all → "PCS".
"""

# ─────────────────────────────────────────────────────────────────────
# 8. CMA CGM (CMDU) — "Release Notification"
# ─────────────────────────────────────────────────────────────────────
CARRIER_PROMPTS["CMDU"] = _OUTPUT_SCHEMA + """
This is a CMA CGM Release Notification.

PICKUP:
- address = "QUAY / TERMINAL" field (left side, near top).
  Example: "RST ZUIDZIJDE", "DEDICATED DELTA SOUTH TERMINAL"
- reference per container = "Pincode" column in the container table.
  A valid pincode is an alphanumeric CODE like "QGC13E291".
  → If a real pincode value exists → use it.
  → If blank, says "Portbase", or no Pincode column → use "PCS".
  CRITICAL — these are NOT pincodes, do NOT use them as reference:
    - Dates like "12-AUG-26", "17-JUL-26" (those are "PIN valid until" dates)
    - Numbers like 27950.000 or 1260 (those are weights/quantities)
    - The word "Release comment" or seal numbers
  When in doubt → use "PCS".

RETURN — THIS TABLE CAN HAVE MULTIPLE ROWS WITH DIFFERENT ADDRESSES:
- Look at the "EMPTY RETURN ADDRESS" table at the bottom (usually page 2).
  Columns: EMPTY RETURN ADDRESS | CONTAINERS | Turn-In-Ref | D&D Invoice
- This table may have MULTIPLE ROWS, each with a DIFFERENT depot address and
  different containers assigned to it. For example:
    Row 1: "KRAMER DELTA CMA CGM" → TEMU5177226, SEGU1192041 → "CMA STOCK"
    Row 2: "UNITED WAALHAVEN TERMINAL (UWT DEPOT 7)" → TRHU3267601 → "CMA STOCK 28"
- You MUST match each container to its SPECIFIC row to get the correct address AND ref.
- In the references array, each container MUST have:
    - "address": the depot from ITS specific row (not a shared address)
    - "reference": the Turn-In-Ref from ITS specific row
- The top-level return.address should be the FIRST depot (or leave empty if multiple).
  → If a Turn-In-Ref value exists → use it exactly.
  → If blank for a specific container → "".
  → If ALL containers have blank Turn-In-Ref → set flag = "forward_to_client".
"""

# ─────────────────────────────────────────────────────────────────────
# 9. MSC (MSCU) — "RELEASE ORDER"
# ─────────────────────────────────────────────────────────────────────
CARRIER_PROMPTS["MSCU"] = _OUTPUT_SCHEMA + """
This is an MSC Release Order.

PICKUP:
- address = "TERMINAL" field in the "CONTAINER RELEASE DETAILS" section.
  Example: "ECT DELTA DDE"
- reference per container = "RELEASE REFERENCE" field.
  → If it shows a Portbase URL (e.g. "https://start.pcs.portbase.com/") → use "PCS".
  → If an actual pincode/reference value → use it.

RETURN:
- address = "DEPOT" field in the "CONTAINER RELEASE DETAILS" section.
  Example: "MED, Smirnoffweg 17, 3088 HE, Rotterdam"
- reference per container = "DROP OFF REFERENCE" field.
  Example: "614RTB723236"
  → If a value exists → use it.
  → If missing/empty → set flag = "forward_to_jordex_import".
"""

# ─────────────────────────────────────────────────────────────────────
# 10. ZIM (ZIMU) — "Container Release Notification"
# ─────────────────────────────────────────────────────────────────────
CARRIER_PROMPTS["ZIMU"] = _OUTPUT_SCHEMA + """
This is a ZIM Container Release Notification.

PICKUP:
- address = "Pick up terminal:" field.
  Example: "RTM ECT DELTA TERMINAL - HN 8200 NORD"
- reference per container = "Pincode" column in the BL/Container table.
  Example: "QGC13E291"
  → If a pincode exists → use it.
  → If blank → "".

RETURN:
- address = "Empty return:" field.
  Example: "RTM TO FOLLOW" or "Kramer Delta"
  Also check below for "***EMPTY RETURN***" details with specific depot info.
- reference per container = "Empty Ref:" field or "Empty Raf:" value.
  → If a value exists → use it.
  → If empty/missing → set flag = "forward_to_jordex".
"""

# ─────────────────────────────────────────────────────────────────────
# ONE LINE (ONEY) — kept from v4
# ─────────────────────────────────────────────────────────────────────
CARRIER_PROMPTS["ONEY"] = _OUTPUT_SCHEMA + """
This is a ONE LINE Delivery Order.

PICKUP:
- Look at "Cargo Pick Up Loc" field. It has TWO parts:
  1. Coded line: "NLRTM15 (RWG (ROTTERDAM WORLD GATEWAY))"
  2. Full address line below it: "Rotterdam World Gateway Amoerweg 50 Postbus 59104 3199 KD"
  → Use the FULL ADDRESS from part 2 as-is.
  Examples:
    "Rotterdam World Gateway Amoerweg 50 Postbus 59104 3199 KD" → "ROTTERDAM WORLD GATEWAY, AMOERWEG 50, 3199 KD"
    "ECT Delta Terminal Europaweg 875 Rotterdam" → "ECT DELTA TERMINAL, EUROPAWEG 875, ROTTERDAM"
  → Format as: "TERMINAL NAME, STREET NUMBER, POSTAL CODE CITY"
  → If no full address line exists, fall back to the name inside parentheses from part 1.
- reference per container: check "PINCODE:" field under each container.
  → If PINCODE HAS VALUE USE THAT ELSE PCS.
  → Default: "PCS".

RETURN:
- Look at "Empty Return Location" field. It has TWO parts:
  1. Coded: "NLRTM24 (KRAMER TERMINAL (ONE DEDICATED DEPOT), ROTTERDAM)"
  2. "Address:" field next to or below it: "Kramer Terminal Missouriweg 30 DCS Haven No. 8050 Rotterdam-Maasvlakte Rotterdam"
  → FIRST try the "Address:" field (part 2). Use the FULL ADDRESS as-is.
  Examples:
    "Kramer Terminal Missouriweg 30 DCS Haven No. 8050 Rotterdam-Maasvlakte" → "KRAMER TERMINAL, MISSOURIWEG 30, ROTTERDAM-MAASVLAKTE"
    "ECT Delta Terminal Europaweg 875 3199 LD Rotterdam" → "ECT DELTA TERMINAL, EUROPAWEG 875, 3199 LD ROTTERDAM"
  → Format as: "TERMINAL NAME, STREET NUMBER, POSTAL CODE CITY"
  → If no "Address:" field exists, fall back to the coded name from part 1:
    Extract terminal name with descriptive suffix, remove location code and city.
    "NLRTM24 (KRAMER TERMINAL (ONE DEDICATED DEPOT), ROTTERDAM)" → "KRAMER TERMINAL (ONE DEDICATED DEPOT)"
- reference: read REMARKS section for "REF: [CODE]" or "OPEN REF.: '[CODE]'".
  Extract exact code. If not found → "ONEMT".
"""

# ─────────────────────────────────────────────────────────────────────
# 11. EVERGREEN (EGLV) — "TELEX RELEASE" / "DELIVERY ORDER"
# ─────────────────────────────────────────────────────────────────────
# CRITICAL: Evergreen DO documents do NOT contain pickup/return depot
# addresses. The real depot data is fetched live from the Evergreen
# tracking portal (evergreen_portal.py). Do NOT try to guess these.
CARRIER_PROMPTS["EGLV"] = _OUTPUT_SCHEMA + """
This is an Evergreen (EGLV) document. The pickup and return depot data is
NOT available in this PDF — it is fetched live from the Evergreen web portal.
Leave all address/reference fields as empty strings. Set flag to "evergreen_portal_required".
"""

# ─────────────────────────────────────────────────────────────────────
# DEFAULT — unknown carrier
# ─────────────────────────────────────────────────────────────────────
CARRIER_PROMPTS["_DEFAULT"] = _OUTPUT_SCHEMA + """
Extract pickup and return using whatever field labels are visible.
Look for pickup/collection terminal + reference and empty return depot + reference.
If reference is via Portbase → use "PCS".
Set flag = "needs_manual_review".
"""


# ══════════════════════════════════════════════════════════════════════
#  ADDRESS NORMALIZATION
# ══════════════════════════════════════════════════════════════════════

def normalize_address(addr: str) -> str:
    if not addr:
        return ""
    first_line = addr.split("\n")[0].strip().rstrip(",")

    # Strip UN/LOCODE-style location codes like NLRTM01, BEANR01
    # These are always: 5-6 uppercase letters + 1-2 digits
    first_line = re.sub(r'^[A-Z]{5,6}\d{1,2}\s*', '', first_line).strip()

    # Handle "ABBR (FULL NAME)" pattern → extract full name from parens
    # e.g. "RWG (ROTTERDAM WORLD GATEWAY)" → "ROTTERDAM WORLD GATEWAY"
    abbr_paren = re.match(r'^[A-Z0-9]{2,6}\s*\((.+)\)\s*$', first_line)
    if abbr_paren:
        inner = abbr_paren.group(1).strip()
        # Remove trailing city after last comma
        parts = inner.rsplit(",", 1)
        city_noise = {"ROTTERDAM", "ANTWERP", "AMSTERDAM", "KAMPEN", "MOERDIJK",
                      "MAASVLAKTE", "BELGIUM", "NETHERLANDS", "GERMANY"}
        if len(parts) == 2 and parts[1].strip().upper() in city_noise:
            first_line = parts[0].strip()
        else:
            first_line = inner
    else:
        # Remove trailing city/country after last comma
        parts = first_line.rsplit(",", 1)
        city_noise = {"ROTTERDAM", "ANTWERP", "AMSTERDAM", "KAMPEN", "MOERDIJK",
                      "MAASVLAKTE", "BELGIUM", "NETHERLANDS", "GERMANY", "THE NETHERLANDS",
                      "ZUID HOLLAND", "NOORD HOLLAND"}
        if len(parts) == 2 and parts[1].strip().upper() in city_noise:
            first_line = parts[0].strip()

    upper = first_line.upper().strip()

    # Only expand abbreviation if address IS the abbreviation (not already full name)
    for abbr, full in ADDRESS_ABBREVIATIONS.items():
        if upper == abbr:
            first_line = full
            break

    return first_line.strip()


# ══════════════════════════════════════════════════════════════════════
#  TEXT-BASED FALLBACKS
# ══════════════════════════════════════════════════════════════════════

def _detect_carrier_from_text(text: str) -> tuple[str, str]:
    text_upper = text.upper()
    checks = [
        ("HAPAG-LLOYD", "hapag-lloyd", "HLCU"),
        ("HAPAG LLOYD", "hapag-lloyd", "HLCU"),
        ("A. P. MOLLER", "maersk", "MAEU"),
        ("A.P. MOLLER", "maersk", "MAEU"),
        ("MAERSK", "maersk", "MAEU"),
        ("MEDITERRANEAN SHIPPING", "msc", "MSCU"),
        ("OCEAN NETWORK EXPRESS", "one", "ONEY"),
        ("YANG MING", "yang ming", "YMLU"),
        ("EVERGREEN", "evergreen", "EGLV"),
        ("COSCO", "cosco", "COSU"),
        ("OOCL", "oocl", "OOLU"),
        ("CMA CGM", "cma cgm", "CMDU"),
        ("CMA-CGM", "cma cgm", "CMDU"),
        ("HYUNDAI", "hmm", "HDMU"),
        (" HMM ", "hmm", "HDMU"),
        ("ZIM", "zim", "ZIMU"),
        ("WAN HAI", "wan hai", "WHLC"),
        ("FAMOUS PACIFIC", "fps", "FPS"),
        ("FPS", "fps", "FPS"),
        (" MSC ", "msc", "MSCU"),
    ]
    for keyword, name, scac in checks:
        if keyword in text_upper:
            return name, scac
    for scac in KNOWN_SCAC:
        if re.search(rf'\b{scac}[A-Z0-9]{{6,}}', text_upper):
            for k, v in CARRIER_SCAC.items():
                if v == scac:
                    return k, scac
    return "", ""


def _detect_doc_subtype_from_text(text: str) -> str:
    text_lower = text.lower()
    ack_patterns = [
        "acknowledgement copy for delivery order",
        "delivery order request",
        "smart inland delivery request",
        "acknowledgement copy",
    ]
    for pat in ack_patterns:
        if pat in text_lower:
            return "acknowledgement"
    if re.search(r'\binvoice\s*(no\.?|number)', text_lower):
        return "invoice"
    do_patterns = [
        "delivery order", "customer release", "release order",
        "container release notification", "laat volgen",
        "release notification", "release message",
    ]
    for pat in do_patterns:
        if pat in text_lower:
            return "delivery_order"
    return "other"


def _maersk_text_fallback(text: str, containers: list) -> dict:
    """Extract Maersk DO fields from raw text."""
    pickup_addr = ""
    return_addr = ""
    return_ref = ""

    lines = text.split("\n")

    for i, line in enumerate(lines):
        if "full delivery pickup" in line.lower():
            addr_parts = []
            for j in range(i + 1, min(i + 6, len(lines))):
                l = lines[j].strip()
                if not l:
                    continue
                if any(kw in l.lower() for kw in ["empty container", "please be aware", "haulage"]):
                    break
                addr_parts.append(l)
            if addr_parts:
                pickup_addr = "\n".join(addr_parts)
            break

    for i, line in enumerate(lines):
        if "empty container depot" in line.lower():
            addr_parts = []
            for j in range(i + 1, min(i + 6, len(lines))):
                l = lines[j].strip()
                if not l:
                    continue
                if any(kw in l.lower() for kw in ["please be aware", "haulage", "page ", "as your"]):
                    break
                addr_parts.append(l)
            if addr_parts:
                return_addr = "\n".join(addr_parts)
            break

    # Haulage Instructions → return ref
    for i, line in enumerate(lines):
        if "haulage instruction" in line.lower():
            for j in range(i, min(i + 4, len(lines))):
                l = lines[j].strip()
                m = re.search(r'truck\s+(?:with\s+)?ref[:\s]+(\S+)', l, re.IGNORECASE)
                if m:
                    return_ref = m.group(1).strip().rstrip("/")
                    break
                m = re.search(r'Reference[:\s]+([A-Z0-9]+)', l, re.IGNORECASE)
                if m:
                    return_ref = m.group(1).strip()
                    break
            break

    # Maersk default: if no haulage instruction ref found
    if not return_ref:
        if return_addr and "star container" in return_addr.lower():
            return_ref = "MAERSKSTACK"
        else:
            return_ref = "MAEMT"

    pickup_refs = [{"container_no": c, "reference": "", "address": pickup_addr} for c in containers]
    return_refs = [{"container_no": c, "reference": return_ref, "address": return_addr} for c in containers]

    return {
        "pickup": {"address": pickup_addr, "reference_mode": "per_container", "reference": "", "references": pickup_refs},
        "return": {"address": return_addr, "reference_mode": "per_container", "reference": "", "references": return_refs},
        "flag": "",
    }


def _generic_text_fallback(text: str, containers: list) -> dict:
    pickup_addr = ""
    return_addr = ""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        ll = line.lower()
        if any(kw in ll for kw in ["pickup", "pick-up", "pick up", "cargo pickup", "discharging terminal"]):
            if ":" in line:
                pickup_addr = line.split(":", 1)[1].strip()
            elif i + 1 < len(lines):
                pickup_addr = lines[i + 1].strip()
        if any(kw in ll for kw in ["return", "depot", "empty return", "empty container", "turn in depot"]):
            if ":" in line:
                return_addr = line.split(":", 1)[1].strip()
            elif i + 1 < len(lines):
                return_addr = lines[i + 1].strip()
    refs_p = [{"container_no": c, "reference": "", "address": pickup_addr} for c in containers]
    refs_r = [{"container_no": c, "reference": "", "address": return_addr} for c in containers]
    return {
        "pickup": {"address": pickup_addr, "reference_mode": "per_container", "reference": "", "references": refs_p},
        "return": {"address": return_addr, "reference_mode": "per_container", "reference": "", "references": refs_r},
        "flag": "needs_manual_review",
    }


# ══════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════

def _safe_str(val) -> str:
    if val is None:
        return ""
    s = str(val).strip()
    if s.lower() in ("null", "none", "n/a"):
        return ""
    return s


def _validate_mbl(mbl: str, carrier_name: str, logo_present: bool) -> str:
    mbl = re.sub(r'[^A-Z0-9]', '', mbl.upper())
    if len(mbl) < 7:
        return ""

    prefix4 = mbl[:4]
    if prefix4 in KNOWN_SCAC:
        return mbl

    if carrier_name:
        carrier_lower = carrier_name.lower().strip()
        for key, scac in CARRIER_SCAC.items():
            if key in carrier_lower:
                return scac + mbl

    if len(mbl) >= 8:
        return mbl

    return ""


def _scac_from_carrier_name(carrier_name: str) -> str:
    if not carrier_name:
        return ""
    carrier_lower = carrier_name.lower().strip()
    for key, scac in CARRIER_SCAC.items():
        if key in carrier_lower:
            return scac
    return ""


def _empty_section() -> dict:
    return {"address": "", "reference_mode": "per_container", "reference": "", "references": []}


def _normalize_result(result: dict) -> dict:
    for key in ("mbl", "carrier_name", "scac", "flag", "folder_name"):
        result[key] = _safe_str(result.get(key))
    containers = result.get("containers") or []
    result["containers"] = [c for c in containers if c]

    for section_key in ("pickup", "return"):
        section = result.get(section_key)
        if section is None:
            continue
        if not isinstance(section, dict):
            result[section_key] = _empty_section()
            continue
        section["address"] = _safe_str(section.get("address"))
        section["reference"] = _safe_str(section.get("reference"))
        section["reference_mode"] = "per_container"

        refs = section.get("references") or []
        
        # Robustness: If AI returned list of strings instead of dicts, convert them
        for i in range(len(refs)):
            if isinstance(refs[i], str):
                refs[i] = {"container_no": refs[i], "reference": "", "address": section.get("address", "")}
                
        existing_cnos = {_safe_str(r.get("container_no")).upper().replace(" ", "") for r in refs if r and isinstance(r, dict)}
        for cno in result["containers"]:
            if cno.upper() not in existing_cnos:
                refs.append({"container_no": cno, "reference": "", "address": section["address"]})
        for ref in refs:
            ref["container_no"] = _safe_str(ref.get("container_no")).upper().replace(" ", "")
            ref["reference"] = _safe_str(ref.get("reference"))
            ref["address"] = _safe_str(ref.get("address")) or section["address"]
        section["references"] = refs
        result[section_key] = section

    # Normalize addresses
    for section_key in ("pickup", "return"):
        section = result.get(section_key)
        if section and isinstance(section, dict):
            section["address"] = normalize_address(section["address"])
            for ref in section.get("references", []):
                ref["address"] = normalize_address(ref.get("address", ""))

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


def _gemini_call(gemini_model, pdf_bytes_b64: str, prompt: str, max_tokens: int = 1500) -> dict | None:
    try:
        resp = gemini_model.generate_content(
            [
                {"mime_type": "application/pdf", "data": pdf_bytes_b64},
                prompt,
            ],
            generation_config={"temperature": 0.05, "max_output_tokens": max_tokens},
        )
        raw = resp.text.strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        parsed = json.loads(raw)
        log.info("  Gemini response: %s", json.dumps(parsed, ensure_ascii=False)[:500])
        return parsed
    except json.JSONDecodeError as e:
        log.warning("  Gemini JSON parse failed: %s", e)
        return None
    except Exception as e:
        log.warning("  Gemini call failed: %s", e)
        return None


# ══════════════════════════════════════════════════════════════════════
#  CARRIER-SPECIFIC SAFETY NETS
# ══════════════════════════════════════════════════════════════════════

def _apply_safety_net(scac: str, result: dict) -> dict:
    pickup = result.get("pickup")
    ret = result.get("return")

    if scac == "FPS":
        result["pickup"] = None
        result["return"] = None
        result["flag"] = "skip_extraction"
        return result

    if pickup is None or ret is None:
        return result

    if scac == "MAEU":
        # Pickup: only actual pin or ""
        _maersk_clean_refs(pickup)
        # Return: default MAEMT if empty, MAERSKSTACK if Star Container
        _maersk_return_default(ret)

    elif scac == "HLCU":
        _pcs_if_empty_or_junk(pickup)
        _pcs_if_empty_or_junk(ret)

    elif scac == "MSCU":
        _pcs_if_empty(pickup)
        if not _has_any_reference(ret):
            result["flag"] = result["flag"] or "forward_to_jordex_import"

    elif scac == "COSU":
        _pcs_if_empty(pickup)
        _cosu_clean_return_ref(ret)

    elif scac == "YMLU":
        _pcs_if_empty_or_portbase(pickup)
        _pcs_if_empty_or_portbase(ret)

    elif scac == "OOLU":
        _pcs_if_empty(pickup)
        # Return refs handled by prompt (REMARKS-based)

    elif scac == "HDMU":
        _pcs_if_empty(pickup)
        _pcs_if_empty(ret)

    elif scac == "CMDU":
        _cmdu_clean_pickup(pickup)
        if not _has_any_reference(ret):
            _cmdu_scan_return_ref(ret, result)

    elif scac == "ZIMU":
        # Pickup: pin if present (no default)
        if not _has_any_reference(ret):
            result["flag"] = result["flag"] or "forward_to_jordex"

    elif scac == "ONEY":
        _oney_clean_pickup(pickup)
        _oney_default_return(ret)

    return result


def _has_any_reference(section: dict) -> bool:
    if section.get("reference"):
        return True
    return any(r.get("reference") for r in section.get("references", []))


def _maersk_clean_refs(section: dict):
    """Maersk pickup: only actual pin. Remove any fabricated defaults."""
    bad = {"PCS", "MAEMT", "MAERSKSTACK", "PORTBASE", "NONE", "NULL"}
    r = (section.get("reference") or "").strip().upper()
    if r in bad:
        section["reference"] = ""
    for ref in section.get("references", []):
        r = (ref.get("reference") or "").strip().upper()
        if r in bad:
            ref["reference"] = ""


def _maersk_return_default(section: dict):
    """Maersk return: if no ref → MAEMT, if Star Container → MAERSKSTACK."""
    addr = (section.get("address") or "").lower()
    is_star = "star container" in addr

    if not _has_any_reference(section):
        default = "MAERSKSTACK" if is_star else "MAEMT"
        section["reference"] = default
        for ref in section.get("references", []):
            if not ref.get("reference"):
                ref["reference"] = default
        log.info("Maersk return default: '%s' (star=%s)", default, is_star)


def _pcs_if_empty_or_junk(section: dict):
    junk = {"NONE", "NULL", ""}
    for ref in section.get("references", []):
        r = (ref.get("reference") or "").strip().upper()
        if r in junk or "JORDEX" in r or "PORTBASE" in r:
            ref["reference"] = "PCS"
    r = (section.get("reference") or "").strip().upper()
    if r in junk or "PORTBASE" in r:
        section["reference"] = "PCS"


def _pcs_if_empty_or_portbase(section: dict):
    for ref in section.get("references", []):
        r = (ref.get("reference") or "").strip().upper()
        if not r or r in ("PORTBASE", "NONE", "NULL"):
            ref["reference"] = "PCS"
    r = (section.get("reference") or "").strip().upper()
    if not r or r in ("PORTBASE", "NONE", "NULL"):
        section["reference"] = "PCS"


def _pcs_if_empty(section: dict):
    for ref in section.get("references", []):
        if not (ref.get("reference") or "").strip():
            ref["reference"] = "PCS"
    if not (section.get("reference") or "").strip():
        section["reference"] = "PCS"


def _cosu_clean_return_ref(section: dict):
    def _clean(val):
        val = val.strip().upper()
        if "TRUCK=" in val:
            m = re.search(r'TRUCK=([A-Z0-9]+)', val)
            return m.group(1) if m else "PCS"
        if "BY TRUCK" in val:
            m = re.search(r'TRUCK[=:\s]+([A-Z0-9]+)', val)
            return m.group(1) if m else "PCS"
        return val if val and val not in ("NONE", "NULL") else "PCS"
    if section.get("reference"):
        section["reference"] = _clean(section["reference"])
    for ref in section.get("references", []):
        r = ref.get("reference", "")
        ref["reference"] = _clean(r) if r else "PCS"


def _oney_clean_pickup(section: dict):
    def _is_sri(val):
        return bool(re.match(r'^[A-Z]{2}\d+$', val.strip().upper()))
    for ref in section.get("references", []):
        r = ref.get("reference", "")
        if not r or _is_sri(r):
            ref["reference"] = "PCS"
    r = section.get("reference", "")
    if not r or _is_sri(r):
        section["reference"] = "PCS"


def _oney_default_return(section: dict):
    if not _has_any_reference(section):
        section["reference"] = "ONEMT"
        for ref in section.get("references", []):
            if not ref.get("reference"):
                ref["reference"] = "ONEMT"


def _cmdu_clean_pickup(section: dict):
    def _is_bad_ref(s):
        """Reject dates, weights, quantities — only keep real pincodes."""
        if not s:
            return True
        s_upper = s.strip().upper()
        # Reject date patterns: "12-AUG-26", "17-JUL-26", "2026-07-17", etc.
        if re.match(r'^\d{1,2}-[A-Z]{3}-\d{2,4}$', s_upper):
            return True
        if re.match(r'^\d{4}-\d{2}-\d{2}', s_upper):
            return True
        if re.match(r'^\d{1,2}/\d{1,2}/\d{2,4}$', s_upper):
            return True
        # Reject pure numbers (weights/quantities)
        try:
            float(s.replace(",", ""))
            return True
        except ValueError:
            pass
        # Reject known non-pincode strings
        if s_upper in ("PORTBASE", "PCS", "NONE", "NULL", "N/A"):
            return True
        return False

    r = section.get("reference", "")
    if _is_bad_ref(r):
        section["reference"] = "PCS"
    for ref in section.get("references", []):
        r = ref.get("reference", "")
        if _is_bad_ref(r):
            ref["reference"] = "PCS"


def _cmdu_scan_return_ref(ret_section: dict, result: dict):
    pdf_path = result.get("_pdf_path", "")
    if not pdf_path:
        result["flag"] = result.get("flag") or "forward_to_client"
        return
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as _pdf:
            raw = " ".join(p.extract_text() or "" for p in _pdf.pages)
        m = re.search(r'Turn[- ]In[- ]Ref[:\s]+([A-Z0-9 ]{3,25})', raw, re.IGNORECASE)
        if not m:
            m = re.search(r'\b(CMA\s*[A-Z0-9]{3,15})\b', raw, re.IGNORECASE)
        if m:
            ref_val = re.sub(r'\s+', ' ', m.group(1).strip().upper())
            for ref in ret_section.get("references", []):
                if not ref.get("reference"):
                    ref["reference"] = ref_val
        else:
            result["flag"] = result.get("flag") or "forward_to_client"
    except Exception as e:
        log.warning("CMDU safety-net scan failed: %s", e)
        result["flag"] = result.get("flag") or "forward_to_client"


# ══════════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════

def extract_delivery_order(pdf_path: str, gemini_model) -> dict:
    """
    Two-call extraction:
      Call 1 → Identify carrier + doc subtype
      Call 2 → Carrier-specific extraction (ONLY for actual delivery orders)
      Fallback → Text-based extraction

    doc_subtype in result:
      "delivery_order"  → full extraction, upload as Container release / DO
      "acknowledgement" → NO extraction, upload as Additional Files
      "invoice"         → for ZIM: upload as Carrier documents / Invoice carrier
                          for others: skip
    """
    result = {
        "doc_type": "delivery_order",
        "doc_subtype": "delivery_order",
        "mbl": "",
        "carrier_name": "",
        "scac": "",
        "containers": [],
        "pickup": _empty_section(),
        "return": _empty_section(),
        "flag": "",
        "source_file": os.path.basename(pdf_path),
        "extracted_at": datetime.now().isoformat(),
        "folder_name": "",
    }

    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()
    pdf_b64 = base64.b64encode(pdf_bytes).decode()
    raw_text = _extract_text(pdf_path)

    # ── CALL 1: Carrier + doc subtype ────────────────────────────
    call1 = _gemini_call(gemini_model, pdf_b64, IDENTIFY_PROMPT, max_tokens=500)

    carrier_name = ""
    scac = ""
    logo_present = False
    containers = []
    mbl = ""
    doc_subtype = ""

    if call1:
        carrier_name = _safe_str(call1.get("carrier_name"))
        logo_present = call1.get("carrier_logo_present", False)
        doc_subtype = _safe_str(call1.get("doc_subtype"))
        mbl = _safe_str(call1.get("mbl"))
        containers = [
            re.sub(r'\s+', '', c).upper()
            for c in (call1.get("containers") or []) if c
        ]
        scac = _scac_from_carrier_name(carrier_name)
        log.info("  Call 1 → carrier=%s scac=%s subtype=%s containers=%s",
                 carrier_name, scac, doc_subtype, containers)
    else:
        log.warning("  Call 1 failed — text fallback")
        carrier_name, scac = _detect_carrier_from_text(raw_text)
        doc_subtype = _detect_doc_subtype_from_text(raw_text)
        containers = list(set(f"{m.group(1)}{m.group(2)}" for m in CONTAINER_RE.finditer(raw_text)))
        for s in KNOWN_SCAC:
            hits = re.findall(rf'\b{s}[A-Z0-9]{{6,}}\b', raw_text.upper())
            if hits:
                mbl = hits[0]
                break

    result["carrier_name"] = carrier_name
    result["scac"] = scac
    result["containers"] = containers
    result["doc_subtype"] = doc_subtype or "delivery_order"

    if mbl:
        validated = _validate_mbl(re.sub(r'\s+', '', mbl).upper(), carrier_name, logo_present)
        if validated:
            result["mbl"] = validated
            result["folder_name"] = validated

    if not result["folder_name"] and containers:
        valid = [c for c in containers if re.fullmatch(r'[A-Z]{4}\d{7}', c)]
        if valid:
            result["folder_name"] = valid[0]

    # ── EARLY EXIT: non-DO documents ─────────────────────────────
    if doc_subtype == "acknowledgement":
        result["flag"] = "acknowledgement_only"
        result["pickup"] = _empty_section()
        result["return"] = _empty_section()
        log.info("  Acknowledgement → skip extraction, upload as Additional Files")
        return result

    if doc_subtype == "invoice":
        if scac == "ZIMU":
            result["flag"] = "zim_invoice"
            log.info("  ZIM invoice → upload as Carrier documents / Invoice carrier")
        else:
            result["flag"] = "skip_extraction"
            log.info("  Invoice → skip")
        result["pickup"] = _empty_section()
        result["return"] = _empty_section()
        return result

    if scac == "FPS":
        result["pickup"] = None
        result["return"] = None
        result["flag"] = "skip_extraction"
        return result

    # ── CALL 2: Carrier-specific extraction ──────────────────────
    carrier_prompt = CARRIER_PROMPTS.get(scac, CARRIER_PROMPTS["_DEFAULT"])
    context = f"\nContainers: {', '.join(containers) if containers else 'unknown'}\n"
    call2 = _gemini_call(gemini_model, pdf_b64, carrier_prompt + context, max_tokens=1500)

    if call2:
        for key in ("pickup", "return"):
            parsed = call2.get(key)
            if parsed and isinstance(parsed, dict):
                result[key] = {**result[key], **parsed}
        if call2.get("flag"):
            result["flag"] = _safe_str(call2["flag"])
        log.info("  Call 2 OK — pickup=%s return=%s",
                 _safe_str((result.get("pickup") or {}).get("address"))[:50],
                 _safe_str((result.get("return") or {}).get("address"))[:50])
    else:
        log.warning("  Call 2 failed — text fallback for %s", scac)
        if scac == "MAEU":
            fb = _maersk_text_fallback(raw_text, containers)
        else:
            fb = _generic_text_fallback(raw_text, containers)
        result["pickup"] = fb["pickup"]
        result["return"] = fb["return"]
        result["flag"] = fb.get("flag", "needs_manual_review")

    # ── POST-PROCESSING ──────────────────────────────────────────
    result["_pdf_path"] = pdf_path
    result = _normalize_result(result)
    result = _apply_safety_net(scac, result)
    result.pop("_pdf_path", None)

    return result