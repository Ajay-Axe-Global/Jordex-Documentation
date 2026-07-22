"""
services/delivery_order/evergreen_portal.py
============================================
Scrapes Pick-up & Return Depot Information from the Evergreen tracking portal
for a given MBL number (e.g. EGLV143651666636).

Flow:
  1. Strip the SCAC prefix (first 4 chars) -> bare 12-digit B/L number
  2. Navigate to https://ct.shipmentlink.com/servlet/TDB1_CargoTracking.do
  3. Accept cookies (if shown), fill #NO, click Submit
  4. Click "Pick-up & Return Depot Information" hyperlink
  5. Screenshot the depot table
  6. Send screenshot to Gemini -> extract per-container pickup/return addresses
  7. Return a dict compatible with the DO extraction result format

Pickup reference  -> always "PCS"
Return reference  -> the bare 12-digit B/L number (per the portal note)
"""
import base64
import json
import logging
import re

log = logging.getLogger("evergreen_portal")

PORTAL_URL = "https://ct.shipmentlink.com/servlet/TDB1_CargoTracking.do"

_DEPOT_EXTRACT_PROMPT = """\
The image shows the "Pick-up & Return Depot Information" table from the Evergreen tracking website.

The table has these columns:
  Container No. | Pick-up Depot Name & Address | Empty Container Return Depot Name & Address | Turn-in Reference

For EVERY row in the table, extract:
  - container_no:    the container number (e.g. EITU8107600)
  - pickup_address:  the full "Pick-up Depot Name & Address" (name + street/city/postcode)
  - return_address:  the full "Empty Container Return Depot Name & Address" (name + street/city/postcode)

IMPORTANT RULES:
- Extract EVERY container row, even if there is only one.
- Combine the depot name and address into a single address string.
- The "Turn-in Reference" column often says "Please use the 12-digit B/L number..." - IGNORE it; the caller will supply the return reference.
- Return ONLY valid JSON. No markdown, no backticks.

Output format:
{
  "containers": [
    {
      "container_no": "EITU8107600",
      "pickup_address": "E.C.T. DELTA TERMINALS B.V. - Zuidzijde (Southside), EUROPEAWEG 875 PORT NR 8180, ROTTERDAM, ZUID HOLLAND 3199 LD",
      "return_address": "EUROMAX TERMINAL ROTTERDAM, MAASVLAKTEWEG 951, ROTTERDAM, ZUID HOLLAND 3199 LZ"
    }
  ]
}
"""


def _strip_scac(mbl: str) -> str:
    """Remove the leading 4-letter SCAC prefix from the MBL.

    EGLV143651666636 -> 143651666636
    If the MBL is already bare (no SCAC), return as-is.
    """
    stripped = mbl.strip().upper()
    if re.match(r'^[A-Z]{4}[0-9]{10,14}$', stripped):
        return stripped[4:]
    return stripped


def scrape_evergreen_depot(mbl_full: str, pw, gemini_model=None) -> dict | None:
    """
    Scrape Pick-up & Return Depot Information from the Evergreen tracking portal.

    Args:
        mbl_full:      The full MBL string (e.g. "EGLV143651666636")
        pw:            A running playwright instance (sync_playwright().start())
        gemini_model:  Gemini model instance for screenshot-to-JSON extraction

    Returns:
        dict with keys: pickup, return  (compatible with DO extraction format)
        or None on failure.
    """
    bl_number = _strip_scac(mbl_full)
    log.info("  [Evergreen] Scraping portal for BL: %s (from MBL: %s)", bl_number, mbl_full)

    browser = None
    try:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.goto(PORTAL_URL, timeout=30_000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        # -- Accept cookie consent if present ----------------------
        for btn_text in ("Accept All", "Accept", "Agree", "OK"):
            try:
                btn = page.get_by_text(btn_text, exact=True)
                if btn.is_visible(timeout=2000):
                    btn.click()
                    page.wait_for_timeout(1000)
                    break
            except Exception:
                pass

        # -- Fill B/L number and submit ----------------------------
        page.locator("#NO").click()
        page.locator("#NO").fill(bl_number)
        page.wait_for_timeout(500)
        page.get_by_role("button", name="Submit").click()
        page.wait_for_timeout(4000)

        # -- Click the "Pick-up & Return Depot" hyperlink ----------
        try:
            depot_link = page.get_by_role("link", name="Pick-up & Return Depot")
            if not depot_link.is_visible(timeout=5000):
                depot_link = page.locator("a", has_text="Pick-up & Return Depot")
            depot_link.click(timeout=8000)
            page.wait_for_timeout(3000)
        except Exception as e:
            log.warning("  [Evergreen] Could not click depot link: %s", e)
            return None

        # -- Screenshot the depot table ----------------------------
        screenshot_bytes = page.screenshot(full_page=False)
        log.info("  [Evergreen] Screenshot captured (%d bytes)", len(screenshot_bytes))

        # -- Send to Gemini for extraction -------------------------
        if gemini_model is None:
            log.warning("  [Evergreen] No Gemini model -- cannot extract depot data")
            return None

        resp = gemini_model.generate_content(
            [
                {"mime_type": "image/png", "data": base64.b64encode(screenshot_bytes).decode()},
                _DEPOT_EXTRACT_PROMPT,
            ],
            generation_config={"temperature": 0.0, "max_output_tokens": 600},
        )
        raw = resp.text.strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        parsed = json.loads(raw)

        containers_raw = parsed.get("containers") or []
        if not containers_raw:
            log.warning("  [Evergreen] Gemini returned no containers from depot table")
            return None

        log.info("  [Evergreen] Extracted %d container rows from depot table", len(containers_raw))

        # -- Build DO-compatible pickup / return sections ----------
        pickup_refs = []
        return_refs = []

        for row in containers_raw:
            cno = (row.get("container_no") or "").strip().upper().replace(" ", "")
            pickup_addr = (row.get("pickup_address") or "").strip()
            return_addr = (row.get("return_address") or "").strip()

            if not cno:
                continue

            pickup_refs.append({
                "container_no": cno,
                "reference":    "PCS",      # always PCS for Evergreen
                "address":      pickup_addr,
            })
            return_refs.append({
                "container_no": cno,
                "reference":    bl_number,  # 12-digit BL as return ref
                "address":      return_addr,
            })

        if not pickup_refs:
            log.warning("  [Evergreen] No valid container rows parsed -- aborting")
            return None

        result = {
            "pickup": {
                "address":        pickup_refs[0]["address"],
                "reference_mode": "per_container",
                "reference":      None,
                "references":     pickup_refs,
            },
            "return": {
                "address":        return_refs[0]["address"],
                "reference_mode": "per_container",
                "reference":      None,
                "references":     return_refs,
            },
            "evergreen_bl": bl_number,
        }

        log.info("  [Evergreen] Depot data built: %d pickup, %d return refs",
                 len(pickup_refs), len(return_refs))
        return result

    except Exception as e:
        log.error("  [Evergreen] Portal scrape failed: %s", e, exc_info=True)
        return None
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
