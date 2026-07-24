"""
services/arrival_notice/fcs_handler.py — FCS Carrier Special Handling
======================================================================
Correct flow (derived from Playwright codegen recording):

  PRE-ROUTING (on main shipment page):
    1. Carrier tab → verify / update Vessel Name → Save

  INSIDE VIEW ROUTING:
    2. Lane tab → update Arrival date (ETA field = #arrival-start) → Save
    3. Destination tab → add Warehouse Address → Save

  The handler is called after the AN document has been uploaded.
  At that point the browser is on the main shipment detail page.
"""

import logging
import re
from playwright.sync_api import Page, TimeoutError as PwTimeout

log = logging.getLogger("service.arrival_notice.fcs")

# ── Carrier detection ────────────────────────────────────────────────

FCS_CARRIER_NAMES = {
    "FPS", "FPS FAMOUS PACIFIC SHIPPING", "FAMOUS PACIFIC SHIPPING",
    "FCS", "SHANGHAI F S CONTAINER LINE",
}


def is_fcs_carrier(carrier_name: str | None, source_file: str | None = None) -> bool:
    """Return True if the carrier is FCS / Famous Pacific Shipping."""
    if carrier_name:
        name_upper = carrier_name.upper().strip()
        for pattern in FCS_CARRIER_NAMES:
            if pattern in name_upper:
                return True
    if source_file:
        sf = source_file.upper()
        if "FPS" in sf or "FCS" in sf or "FAMOUS PACIFIC" in sf:
            return True
    return False


# ══════════════════════════════════════════════════════════════════════
#  PART 1 — MAIN SHIPMENT PAGE: CARRIER TAB VESSEL NAME
#  (Must run BEFORE opening View Routing)
# ══════════════════════════════════════════════════════════════════════

def update_vessel_on_carrier_tab(page: Page, vessel_name: str) -> bool:
    """
    On the MAIN shipment detail page, click the Carrier tab, check vessel name,
    update if needed, and save.
    """
    if not vessel_name:
        return False

    log.info("  FCS: Navigating to main Carrier tab for vessel update...")

    # Click the main "Carrier" tab (sibling of Parties, Cargo, etc.)
    try:
        # Try role-based first
        carrier_tab = page.get_by_role("tab", name="Carrier")
        carrier_tab.wait_for(state="visible", timeout=8000)
        carrier_tab.click()
        page.wait_for_timeout(2000)
    except Exception:
        # Fallback: find the tab by text among the main tabs
        try:
            clicked = page.evaluate("""() => {
                const tabs = [...document.querySelectorAll('.el-tabs__item')];
                const t = tabs.find(el => el.textContent.trim() === 'Carrier');
                if (t) { t.click(); return true; }
                return false;
            }""")
            if not clicked:
                log.warning("  FCS: Main Carrier tab not found")
                return False
            page.wait_for_timeout(2000)
        except Exception as e:
            log.warning("  FCS: Cannot open main Carrier tab: %s", e)
            return False

    # Read current vessel name
    current_vessel = page.evaluate("""() => {
        const items = [...document.querySelectorAll('.el-form-item')];
        const item = items.find(el => (el.innerText || '').includes('Vessel name'));
        if (!item) return null;
        const inp = item.querySelector('input');
        return inp ? inp.value.trim() : null;
    }""") or ""

    if current_vessel.upper().strip() == vessel_name.upper().strip():
        log.info("  FCS: Vessel name already matches: '%s'", current_vessel)
        return True

    log.info("  FCS: Updating vessel name '%s' → '%s'", current_vessel, vessel_name)
    if not _type_vessel_name(page, vessel_name):
        return False

    return _click_main_save(page, context="Carrier tab")


def _type_vessel_name(page: Page, vessel_name: str) -> bool:
    """
    Type vessel name character-by-character into the Vessel name autocomplete
    and select the best match from the dropdown.
    """
    try:
        # Clear existing value
        page.evaluate("""() => {
            const items = [...document.querySelectorAll('.el-form-item')];
            const item = items.find(el => (el.innerText || '').includes('Vessel name'));
            if (!item) return;
            const inp = item.querySelector('input');
            if (!inp) return;
            inp.focus();
            // Try clear icon
            const clearIcon = item.querySelector('.el-icon-circle-close');
            if (clearIcon) clearIcon.click();
            // Force clear via native setter
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            setter.call(inp, '');
            inp.dispatchEvent(new Event('input', { bubbles: true }));
        }""")
        page.wait_for_timeout(600)

        # Type character by character to trigger autocomplete
        safe_val = vessel_name.upper()
        page.evaluate("""async (vesselName) => {
            const items = [...document.querySelectorAll('.el-form-item')];
            const item = items.find(el => (el.innerText || '').includes('Vessel name'));
            if (!item) return;
            const inp = item.querySelector('input');
            if (!inp) return;
            inp.removeAttribute('readonly');
            inp.click();
            inp.focus();
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            // Ensure cleared
            setter.call(inp, '');
            inp.dispatchEvent(new Event('input', { bubbles: true }));
            await new Promise(r => setTimeout(r, 300));
            for (let i = 0; i < vesselName.length; i++) {
                setter.call(inp, vesselName.substring(0, i + 1));
                inp.dispatchEvent(new InputEvent('input', {
                    bubbles: true, inputType: 'insertText', data: vesselName[i]
                }));
                await new Promise(r => setTimeout(r, 80));
            }
        }""", safe_val)

        page.wait_for_timeout(1500)

        # Select from dropdown
        selected = page.evaluate("""(vesselName) => {
            const selectors = ['.el-autocomplete-suggestion li', '.el-select-dropdown__item'];
            let options = [];
            for (const s of selectors) {
                const found = [...document.querySelectorAll(s)]
                    .filter(el => el.offsetParent !== null);
                if (found.length > 0) { options = found; break; }
            }
            if (!options.length) return { selected: false, reason: 'no_dropdown' };

            const target = vesselName.toUpperCase().trim();
            let best = null, bestScore = -1;
            for (const opt of options) {
                const text = (opt.textContent || '').trim().toUpperCase();
                const nameOnly = text.split('(')[0].trim();
                let score = 0;
                if (nameOnly === target) score = 100;
                else if (nameOnly.startsWith(target)) score = 80;
                else if (nameOnly.includes(target)) score = 60;
                else {
                    for (const w of target.split(/\\s+/)) {
                        if (nameOnly.includes(w)) score += 10;
                    }
                }
                if (score > bestScore) { bestScore = score; best = opt; }
            }
            if (best && bestScore > 0) { best.click(); return { selected: true, score: bestScore }; }
            // No match — click first option
            options[0].click();
            return { selected: true, score: 0, reason: 'first_fallback' };
        }""", safe_val)

        log.info("  FCS: Vessel dropdown result: %s", selected)
        page.wait_for_timeout(800)
        return True

    except Exception as e:
        log.warning("  FCS: _type_vessel_name failed: %s", e)
        return False


def _click_main_save(page: Page, context: str = "") -> bool:
    """Click the main Save button on the shipment detail page."""
    try:
        saved = False
        for sel in [
            "button.save-button.el-button--primary",
            "button.el-button--primary:has-text('Save')",
            "button:has(span:text-is('Save'))",
        ]:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=2000):
                    btn.click()
                    saved = True
                    break
            except Exception:
                continue

        if not saved:
            saved = page.evaluate("""() => {
                const btn = document.querySelector('button.save-button')
                    || [...document.querySelectorAll('button')]
                        .find(b => b.textContent.trim() === 'Save' && b.offsetParent !== null);
                if (btn) { btn.click(); return true; }
                return false;
            }""")

        page.wait_for_timeout(2500)

        # Dismiss OK dialog if it appears
        try:
            ok = page.locator("button:has-text('OK'):visible").first
            if ok.is_visible(timeout=2000):
                ok.click()
                page.wait_for_timeout(800)
        except Exception:
            pass

        if saved:
            log.info("  FCS: Saved (%s)", context)
        else:
            log.warning("  FCS: Save button not found (%s)", context)
        return bool(saved)

    except Exception as e:
        log.warning("  FCS: _click_main_save failed (%s): %s", context, e)
        return False


# ══════════════════════════════════════════════════════════════════════
#  PART 2 — VIEW ROUTING: OPEN
# ══════════════════════════════════════════════════════════════════════

def _open_view_routing(page: Page) -> bool:
    """Click 'View routing' to open the routing sidebar/modal."""
    try:
        # Codegen: page.locator("span").filter(has_text="View routing").first.click()
        span = page.locator("span").filter(has_text="View routing").first
        if span.is_visible(timeout=5000):
            span.click()
            page.wait_for_timeout(3000)
            return True
    except Exception:
        pass

    # Fallback: JS search
    try:
        opened = page.evaluate("""() => {
            const el = document.querySelector('.routing-sidebar__routing-label');
            if (el) { el.click(); return true; }
            const spans = [...document.querySelectorAll('span')];
            const vr = spans.find(s => s.textContent.trim() === 'View routing');
            if (vr) { vr.click(); return true; }
            const all = [...document.querySelectorAll('*')];
            const leaf = all.find(e => e.childElementCount === 0 &&
                                       (e.innerText || '').trim() === 'View routing');
            if (leaf) { leaf.click(); return true; }
            return false;
        }""")
        if opened:
            page.wait_for_timeout(3000)
            return True
    except Exception as e:
        log.warning("  FCS: _open_view_routing JS fallback failed: %s", e)

    log.warning("  FCS: Could not open View routing")
    return False


# ══════════════════════════════════════════════════════════════════════
#  PART 3 — INSIDE ROUTING: LANE TAB — ARRIVAL DATE
#  Codegen: page.get_by_label("Lane").locator("#arrival-start").click()
#  Then date picker opens → fill date → click OK
# ══════════════════════════════════════════════════════════════════════

def _navigate_to_lane_tab_in_routing(page: Page) -> bool:
    """Click the Lane tab inside View Routing."""
    try:
        # Codegen shows: page.get_by_text("Lane").click()
        lane_tab = page.locator("#tab-lane-1").first
        if lane_tab.is_visible(timeout=3000):
            lane_tab.click()
            page.wait_for_timeout(1500)
            return True
    except Exception:
        pass
    try:
        lane_tab = page.locator(".el-tabs__item:has-text('Lane')").first
        if lane_tab.is_visible(timeout=3000):
            lane_tab.click()
            page.wait_for_timeout(1500)
            return True
    except Exception:
        pass
    try:
        page.get_by_text("Lane").first.click(timeout=5000)
        page.wait_for_timeout(1500)
        return True
    except Exception as e:
        log.warning("  FCS: Cannot navigate to Lane tab: %s", e)
        return False


def _update_arrival_date_in_routing(page: Page, arrival_date: str) -> bool:
    """
    Update the Arrival ETA date on the Lane tab inside View Routing.
    
    Codegen: page.get_by_label("Lane").locator("#arrival-start").click()
    arrival_date: DD/MM/YY or DD/MM/YYYY  →  converted to YYYY-MM-DD for picker
    """
    if not arrival_date:
        return False

    # Parse DD/MM/YY(YY) → YYYY-MM-DD
    parts = arrival_date.split("/")
    if len(parts) != 3:
        log.warning("  FCS: Invalid arrival_date format: '%s'", arrival_date)
        return False

    day, month, year = parts
    if len(year) == 2:
        year = "20" + year
    date_iso = f"{year}-{month.zfill(2)}-{day.zfill(2)}"

    log.info("  FCS: Setting arrival date → %s (ISO: %s)", arrival_date, date_iso)

    try:
        # Codegen: page.get_by_label("Lane").locator("#arrival-start").click()
        arrival_input = page.get_by_label("Lane").locator("#arrival-start").first
        arrival_input.wait_for(state="visible", timeout=5000)
        arrival_input.click()
        page.wait_for_timeout(1000)

        # Fill date via JS into the opened datepicker input
        filled = page.evaluate("""(dateVal) => {
            const inputs = [...document.querySelectorAll('input.el-input__inner')]
                .filter(i => (i.placeholder === 'Select date' || i.placeholder === 'Start')
                          && i.offsetParent !== null);
            if (!inputs.length) return false;
            const inp = inputs[inputs.length - 1];
            inp.focus();
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            setter.call(inp, dateVal);
            inp.dispatchEvent(new Event('input', { bubbles: true }));
            inp.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
        }""", date_iso)

        if not filled:
            log.warning("  FCS: Date picker input not found")
            return False

        page.wait_for_timeout(500)

        # Click OK — codegen: page.get_by_role("button", name="OK").click()
        try:
            ok_btn = page.get_by_role("button", name="OK").first
            if ok_btn.is_visible(timeout=2000):
                ok_btn.click()
                page.wait_for_timeout(600)
            else:
                page.evaluate("""() => {
                    const btns = [...document.querySelectorAll('button')]
                        .filter(b => b.textContent.trim() === 'OK' && b.offsetParent !== null);
                    if (btns.length) btns[btns.length - 1].click();
                }""")
                page.wait_for_timeout(600)
        except Exception:
            pass

        log.info("  FCS: Arrival date set to %s", date_iso)
        return True

    except Exception as e:
        log.warning("  FCS: _update_arrival_date_in_routing failed: %s", e)
        return False


def _save_routing_lane(page: Page) -> bool:
    """Save inside View Routing (Lane tab)."""
    try:
        save_btn = page.locator("button:has-text('Save'):visible").first
        if save_btn.is_visible(timeout=3000):
            save_btn.click()
            page.wait_for_timeout(2500)
            try:
                ok = page.locator("button:has-text('OK'):visible").first
                if ok.is_visible(timeout=2000):
                    ok.click()
                    page.wait_for_timeout(600)
            except Exception:
                pass
            log.info("  FCS: Lane routing saved")
            return True
    except Exception as e:
        log.warning("  FCS: _save_routing_lane failed: %s", e)
    return False


# ══════════════════════════════════════════════════════════════════════
#  PART 4 — INSIDE ROUTING: DESTINATION TAB — WAREHOUSE ADDRESS
#
#  Codegen flow:
#    page.get_by_text("Destination").click()                     ← tab
#    page.get_by_text("3. Destination").click()                  ← section header (alt)
#    page.get_by_role("button","").get_by_role("button").nth(3)  ← open address book OR
#    locator("#pane-destination > ... .address-select-toolbar > button:nth-child(2)") ← add btn
#    page.get_by_role("textbox", name="Search").click()          ← search in dialog
#    page.get_by_role("dialog").locator("div")
#         .filter(has_text="Select addressNew").first.click()    ← "New" button if no match
# ══════════════════════════════════════════════════════════════════════

def _navigate_to_destination_tab_in_routing(page: Page) -> bool:
    """Click the Destination tab inside View Routing."""
    # Codegen: page.get_by_text("Destination").click()
    try:
        dest_tab = page.locator("#tab-destination-2").first
        if dest_tab.is_visible(timeout=3000):
            dest_tab.click()
            page.wait_for_timeout(2000)
            return True
    except Exception:
        pass
    try:
        # Text-based (codegen fallback)
        page.get_by_text("Destination").first.click(timeout=5000)
        page.wait_for_timeout(2000)
        return True
    except Exception as e:
        log.warning("  FCS: Cannot open Destination tab: %s", e)
        return False


def _click_add_address_button_in_routing(page: Page) -> bool:
    """
    Click the add/open-address-book button in the Destination tab.

    Codegen shows TWO possible selectors:
      A) page.get_by_role("button","").get_by_role("button").nth(3)
      B) locator("#pane-destination > ... > .address-select-toolbar > button:nth-child(2)")

    We try both plus a JS fallback.
    """
    # Strategy A: full long CSS selector from codegen
    try:
        btn = page.locator(
            "#pane-destination > div > .el-card > .el-card__body > .el-form > div > "
            ".routing-tab-panel__body > div:nth-child(2) > .wrap > div:nth-child(2) > "
            ".el-form-item > .el-form-item__content > div > "
            ".address-select__body--select > .address-select__toolbar > "
            ".address-select-toolbar > button:nth-child(2)"
        ).first
        if btn.is_visible(timeout=3000):
            btn.click()
            page.wait_for_timeout(2000)
            log.info("  FCS: Add address button clicked (selector A)")
            return True
    except Exception:
        pass

    # Strategy B: scoped to #pane-destination, find any toolbar button
    try:
        clicked = page.evaluate("""() => {
            const pane = document.querySelector('#pane-destination');
            if (!pane) return false;
            // All buttons in address-select-toolbar
            const toolbarBtns = [...pane.querySelectorAll(
                '.address-select-toolbar button, .address-select__toolbar button'
            )].filter(b => b.offsetParent !== null);
            if (toolbarBtns.length >= 2) {
                // nth(1) = second button (add/open address book)
                toolbarBtns[1].click(); return true;
            }
            if (toolbarBtns.length === 1) {
                toolbarBtns[0].click(); return true;
            }
            // Last resort: any + or add button
            const allBtns = [...pane.querySelectorAll('button')]
                .filter(b => b.offsetParent !== null);
            const plusBtn = allBtns.find(b => {
                const icon = b.querySelector('i.el-icon-plus, i.el-icon-circle-plus');
                const txt = b.textContent.trim();
                return icon || txt === '+' || txt.toLowerCase() === 'add';
            });
            if (plusBtn) { plusBtn.click(); return true; }
            return false;
        }""")
        if clicked:
            page.wait_for_timeout(2000)
            log.info("  FCS: Add address button clicked (JS fallback)")
            return True
    except Exception as e:
        log.warning("  FCS: _click_add_address_button_in_routing JS failed: %s", e)

    log.warning("  FCS: Could not find Add Address button in Destination tab")
    return False


def _handle_address_dialog(page: Page, warehouse: dict) -> bool:
    """
    Handle the address dialog that appears after clicking the add button.

    Codegen flow:
      1. page.get_by_role("textbox", name="Search").click()  ← search existing
      2. If found → click row → done
      3. If not found → page.get_by_role("dialog")
             .locator("div").filter(has_text="Select addressNew").first.click()
             → "New" button → fill form manually

    warehouse keys: warehouse_name, address_line_1, postal_code, city, country
    """
    warehouse_name = (warehouse.get("warehouse_name") or "").strip()
    if not warehouse_name:
        log.warning("  FCS: No warehouse name provided")
        return False

    # Wait for dialog to appear
    try:
        page.wait_for_selector(
            "[role='dialog'], .el-dialog, .el-dialog__wrapper:not([style*='display: none'])",
            state="visible", timeout=5000
        )
    except Exception:
        log.warning("  FCS: Address dialog did not appear")
        return False

    page.wait_for_timeout(500)

    # Step 1: Search for existing address
    try:
        # Codegen: page.get_by_role("textbox", name="Search").click()
        search_box = page.get_by_role("textbox", name="Search").first
        if search_box.is_visible(timeout=3000):
            search_box.click()
            search_box.fill("")
            search_box.fill(warehouse_name[:20])  # partial search
            page.wait_for_timeout(1500)

            # Check if any matching rows appear in the dialog table
            matched = page.evaluate("""(target) => {
                const rows = [...document.querySelectorAll(
                    '.el-dialog table tbody tr, .el-dialog .el-table__row'
                )].filter(r => r.offsetParent !== null);
                if (!rows.length) return false;
                const t = target.toUpperCase();
                for (const row of rows) {
                    const text = (row.textContent || '').toUpperCase();
                    if (text.includes(t.split(' ')[0])) {
                        row.click();
                        return true;
                    }
                }
                return false;
            }""", warehouse_name)

            if matched:
                log.info("  FCS: Existing address matched and selected")
                page.wait_for_timeout(1000)
                # Confirm dialog save
                _save_address_dialog(page)
                return True
    except Exception as e:
        log.warning("  FCS: Address search step failed: %s", e)

    # Step 2: No match — click "New" to create a new address
    log.info("  FCS: No match found — creating new address")
    try:
        # Codegen: page.get_by_role("dialog").locator("div")
        #              .filter(has_text="Select addressNew").first.click()
        # The "New" button is inside the dialog header area
        new_clicked = page.evaluate("""() => {
            // Look for a button/element with text "New" inside dialog
            const dialog = document.querySelector('.el-dialog');
            if (!dialog) return false;
            const els = [...dialog.querySelectorAll('button, span, a, div')]
                .filter(el => el.offsetParent !== null);
            const newEl = els.find(el =>
                el.textContent.trim() === 'New' ||
                el.textContent.trim() === '+ New' ||
                el.textContent.trim() === 'New address'
            );
            if (newEl) { newEl.click(); return true; }
            return false;
        }""")

        if not new_clicked:
            # Try the exact codegen pattern
            try:
                page.get_by_role("dialog").locator("div").filter(
                    has_text=re.compile(r"Select address.*New", re.DOTALL)
                ).first.click()
                new_clicked = True
            except Exception:
                pass

        if not new_clicked:
            # Try by button text
            try:
                page.get_by_role("dialog").get_by_role("button", name="New").click(timeout=3000)
                new_clicked = True
            except Exception:
                pass

        if new_clicked:
            page.wait_for_timeout(1500)
            log.info("  FCS: 'New' address form opened")
    except Exception as e:
        log.warning("  FCS: Could not click 'New': %s", e)

    # Step 3: Fill the address form (whether from "New" or directly in dialog)
    return _fill_new_address_form(page, warehouse)


def _fill_new_address_form(page: Page, warehouse: dict) -> bool:
    """
    Fill the address creation form with warehouse details.
    Fields: Name (autocomplete), Address Line 1, Postal Code, City, Country (select)
    """
    name      = (warehouse.get("warehouse_name") or "").strip()
    addr1     = (warehouse.get("address_line_1")  or "").strip()
    postal    = (warehouse.get("postal_code")      or "").strip()
    city      = (warehouse.get("city")             or "").strip()
    country   = (warehouse.get("country")          or "").strip()

    try:
        # ── Name field (autocomplete) ──
        if name:
            name_input = page.locator(
                ".el-autocomplete > .el-input > .el-input__inner"
            ).first
            try:
                name_input.wait_for(state="visible", timeout=3000)
                name_input.click()
                name_input.fill("")
                name_input.fill(name)
                page.wait_for_timeout(1000)
                # Dismiss any dropdown that opens (no existing match expected)
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
            except Exception:
                # JS fallback — first text input in dialog
                page.evaluate("""(val) => {
                    const dialog = document.querySelector('.el-dialog');
                    if (!dialog) return;
                    const inp = dialog.querySelector(
                        '.el-autocomplete input, input.el-input__inner');
                    if (!inp) return;
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    setter.call(inp, val);
                    inp.dispatchEvent(new Event('input', { bubbles: true }));
                    inp.dispatchEvent(new Event('change', { bubbles: true }));
                }""", name)

        # ── Fill remaining fields by label ──
        field_map = [
            ("address", addr1),
            ("postal",  postal),
            ("city",    city),
        ]
        for keyword, value in field_map:
            if not value:
                continue
            page.evaluate("""([kw, val]) => {
                const dialog = document.querySelector('.el-dialog');
                if (!dialog) return;
                const labels = [...dialog.querySelectorAll(
                    '.el-form-item__label, label')];
                const lbl = labels.find(l =>
                    l.textContent.trim().toLowerCase().includes(kw));
                if (!lbl) return;
                const item = lbl.closest('.el-form-item');
                if (!item) return;
                const inp = item.querySelector(
                    'input.el-input__inner:not([readonly])');
                if (!inp) return;
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                setter.call(inp, val);
                inp.dispatchEvent(new Event('input', { bubbles: true }));
                inp.dispatchEvent(new Event('change', { bubbles: true }));
            }""", [keyword, value])
            page.wait_for_timeout(200)

        # ── Country (Select dropdown) ──
        if country:
            try:
                # Find country select placeholder and click it
                country_sel = page.get_by_role("dialog").locator(
                    ".el-select .el-input__inner[placeholder='Select']"
                ).first
                if country_sel.is_visible(timeout=2000):
                    country_sel.click()
                    page.wait_for_timeout(500)
                    country_sel.fill(country)
                    page.wait_for_timeout(1000)
                    # Pick from dropdown
                    page.locator("li").filter(
                        has_text=re.compile(rf"^{re.escape(country)}$", re.IGNORECASE)
                    ).first.click(timeout=3000)
                    page.wait_for_timeout(500)
            except Exception as ce:
                log.warning("  FCS: Country selection failed: %s", ce)
                # JS fallback
                page.evaluate("""(val) => {
                    const dialog = document.querySelector('.el-dialog');
                    if (!dialog) return;
                    const labels = [...dialog.querySelectorAll(
                        '.el-form-item__label, label')];
                    const lbl = labels.find(l =>
                        l.textContent.trim().toLowerCase().includes('country'));
                    if (!lbl) return;
                    const item = lbl.closest('.el-form-item');
                    if (!item) return;
                    const inp = item.querySelector('input');
                    if (!inp) return;
                    inp.click();
                }""", country)
                page.wait_for_timeout(400)
                try:
                    page.locator("li").filter(
                        has_text=re.compile(rf"^{re.escape(country)}$", re.IGNORECASE)
                    ).first.click(timeout=2000)
                except Exception:
                    pass

        # ── Save the address form ──
        _save_address_dialog(page)
        log.info("  FCS: New address form filled and saved")
        return True

    except Exception as e:
        log.warning("  FCS: _fill_new_address_form failed: %s", e)
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


def _save_address_dialog(page: Page):
    """Click Save inside the address dialog."""
    try:
        save_btn = page.get_by_role("dialog").get_by_role("button", name="Save").first
        if save_btn.is_visible(timeout=3000):
            save_btn.click()
            page.wait_for_timeout(2000)
            log.info("  FCS: Address dialog saved")
            return
    except Exception:
        pass
    # JS fallback
    try:
        page.evaluate("""() => {
            const dialog = document.querySelector('.el-dialog');
            if (!dialog) return;
            const btns = [...dialog.querySelectorAll('button')]
                .filter(b => b.offsetParent !== null);
            const save = btns.find(b => b.textContent.trim().toLowerCase() === 'save');
            if (save) { save.click(); return; }
            // Last primary button
            const primary = [...dialog.querySelectorAll(
                'button.el-button--primary')].filter(b => b.offsetParent !== null);
            if (primary.length) primary[primary.length - 1].click();
        }""")
        page.wait_for_timeout(2000)
    except Exception as e:
        log.warning("  FCS: _save_address_dialog fallback failed: %s", e)


def _save_destination_tab(page: Page) -> bool:
    """Save the Destination tab after adding the warehouse address."""
    # Try the floated toolbar save button (long selector from original code)
    try:
        btn = page.locator(
            "#pane-destination > div > .el-card > .el-card__body > .el-form > div > "
            ".routing-tab-panel__body > div:nth-child(2) > .wrap > div:nth-child(2) > "
            ".el-form-item > .el-form-item__content > div > "
            ".address-select__body--select > .address-select__toolbar--floated > "
            "button > span > .more-btn__content > .address-select-toolbar > "
            "button:nth-child(2)"
        ).first
        if btn.is_visible(timeout=2000):
            btn.click()
            page.wait_for_timeout(2000)
            log.info("  FCS: Destination tab saved (floated toolbar)")
            return True
    except Exception:
        pass

    # Generic save button in destination pane
    try:
        saved = page.evaluate("""() => {
            const pane = document.querySelector('#pane-destination');
            if (!pane) return false;
            const btns = [...pane.querySelectorAll('button')]
                .filter(b => b.textContent.trim().toLowerCase() === 'save'
                          && b.offsetParent !== null);
            if (btns.length) { btns[0].click(); return true; }
            return false;
        }""")
        if saved:
            page.wait_for_timeout(2000)
            log.info("  FCS: Destination tab saved (fallback)")
            return True
    except Exception:
        pass

    # Last resort: generic routing save
    return _save_routing_lane(page)


# ══════════════════════════════════════════════════════════════════════
#  READ HELPERS — scrape current Jordex values for mismatch check
# ══════════════════════════════════════════════════════════════════════

def _read_current_arrival_date(page: Page) -> str:
    """
    Read the current Arrival Original/Update date from the Lane tab (inside routing).
    Returns the value as a string like '2026-07-19', or '' if empty.
    """
    try:
        val = page.evaluate("""() => {
            // #arrival-start = ETA Original, #arrival-end = ETA Update
            // We check both; prefer update if set
            const start = document.querySelector('#arrival-start');
            const end   = document.querySelector('#arrival-end');
            const v_end   = (end   && end.value)   ? end.value.trim()   : '';
            const v_start = (start && start.value)  ? start.value.trim() : '';
            return v_end || v_start;
        }""") or ""
        return val.strip()
    except Exception:
        return ""


def _arrival_date_matches(current: str, expected: str) -> bool:
    """
    Compare the Jordex arrival date (YYYY-MM-DD) with the extracted date
    (DD/MM/YY or DD/MM/YYYY). Returns True if they represent the same date.
    """
    if not current or not expected:
        return False
    try:
        parts = expected.split("/")
        if len(parts) != 3:
            return False
        day, month, year = parts
        if len(year) == 2:
            year = "20" + year
        expected_iso = f"{year}-{month.zfill(2)}-{day.zfill(2)}"
        # Normalize current (may be DD/MM/YYYY or YYYY-MM-DD or DD MMM YYYY)
        current_clean = current.strip()
        # Try YYYY-MM-DD
        import re as _re
        m = _re.match(r'(\d{4})-(\d{2})-(\d{2})', current_clean)
        if m:
            current_iso = current_clean[:10]
            return current_iso == expected_iso
        # Try DD/MM/YYYY
        m2 = _re.match(r'(\d{1,2})/(\d{1,2})/(\d{2,4})', current_clean)
        if m2:
            d, mo, y = m2.group(1), m2.group(2), m2.group(3)
            if len(y) == 2:
                y = "20" + y
            current_iso = f"{y}-{mo.zfill(2)}-{d.zfill(2)}"
            return current_iso == expected_iso
    except Exception:
        pass
    return False


def _read_current_warehouse_names(page: Page) -> list:
    """
    Read the warehouse/address names already added in the Destination tab.
    Returns a list of name strings (uppercased).
    """
    try:
        names = page.evaluate("""() => {
            const pane = document.querySelector('#pane-destination');
            if (!pane) return [];
            // Address cards show names in .full-address-name or address card text
            const nameEls = pane.querySelectorAll(
                '.full-address-name, .address-card__name, '
                '.address-select__item-name, .el-card .name'
            );
            if (nameEls.length) {
                return [...nameEls].map(el => el.textContent.trim().toUpperCase());
            }
            // Fallback: read all visible text nodes inside address items
            const items = pane.querySelectorAll(
                '.address-select__body--select .el-card, '
                '.address-select__item'
            );
            return [...items].map(el => el.textContent.trim().toUpperCase());
        }""") or []
        return [n for n in names if n]
    except Exception:
        return []


def _warehouse_already_added(existing_names: list, warehouse_name: str) -> bool:
    """Return True if the warehouse appears to already be in the Destination list."""
    if not warehouse_name or not existing_names:
        return False
    target = warehouse_name.upper().strip()
    # Check if first significant word of target appears in any existing entry
    first_word = target.split()[0] if target.split() else ""
    for name in existing_names:
        if target in name or (first_word and first_word in name):
            return True
    return False


# ══════════════════════════════════════════════════════════════════════
#  MAIN ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════════

def handle_fcs_post_upload(page: Page, extraction: dict) -> bool:
    """
    After uploading the Arrival Notice for an FCS carrier, perform additional
    Jordex updates.

    CORRECT FLOW:
      ① Main page → Carrier tab → update vessel name → Save
      ② Open View Routing
      ③ Inside routing → Lane tab → update Arrival date → Save
      ④ Inside routing → Destination tab → add warehouse address → Save
      ⑤ Go back from routing

    Args:
        page:       Playwright page on the shipment detail (after AN upload)
        extraction: dict from extract_arrival_notice with keys:
                      vessel_name, arrival_date,
                      warehouse_name, address_line_1, postal_code, city, country

    Returns:
        True if at least one update succeeded
    """
    any_success = False

    vessel_name  = (extraction.get("vessel_name") or "").strip()
    arrival_date = (extraction.get("arrival_date") or "").strip()
    warehouse = {
        "warehouse_name": extraction.get("warehouse_name"),
        "address_line_1": extraction.get("address_line_1"),
        "postal_code":    extraction.get("postal_code"),
        "city":           extraction.get("city"),
        "country":        extraction.get("country"),
    }
    has_warehouse = any(v for v in warehouse.values() if v)

    if not vessel_name and not arrival_date and not has_warehouse:
        log.info("  FCS: No additional data to update")
        return False

    # ── ① Carrier tab: vessel name (on MAIN page, before routing) ──────
    if vessel_name:
        log.info("  FCS: [Step 1] Updating vessel name on main Carrier tab...")
        if update_vessel_on_carrier_tab(page, vessel_name):
            any_success = True
            log.info("  FCS: [Step 1] Vessel name updated ✓")
        else:
            log.warning("  FCS: [Step 1] Vessel name update failed — continuing")

    # ── ② Open View Routing ─────────────────────────────────────────────
    if not (arrival_date or has_warehouse):
        return any_success

    log.info("  FCS: [Step 2] Opening View Routing...")
    if not _open_view_routing(page):
        log.warning("  FCS: [Step 2] Could not open View Routing — aborting routing steps")
        return any_success

    # ── ③ Lane tab: arrival date (only if mismatched) ───────────────────
    if arrival_date:
        log.info("  FCS: [Step 3] Checking arrival date on Lane tab...")
        if _navigate_to_lane_tab_in_routing(page):
            current_date = _read_current_arrival_date(page)
            log.info("  FCS: [Step 3] Current='%s'  Expected='%s'", current_date, arrival_date)

            if _arrival_date_matches(current_date, arrival_date):
                log.info("  FCS: [Step 3] Arrival date already matches — skipping ✓")
            else:
                log.info("  FCS: [Step 3] Mismatch — updating arrival date...")
                if _update_arrival_date_in_routing(page, arrival_date):
                    if _save_routing_lane(page):
                        any_success = True
                        log.info("  FCS: [Step 3] Arrival date updated ✓")
                    else:
                        log.warning("  FCS: [Step 3] Lane save failed")
                else:
                    log.warning("  FCS: [Step 3] Arrival date fill failed")
        else:
            log.warning("  FCS: [Step 3] Could not open Lane tab")

    # ── ④ Destination tab: warehouse address (only if not already added) ─
    if has_warehouse:
        log.info("  FCS: [Step 4] Checking warehouse address on Destination tab...")
        if _navigate_to_destination_tab_in_routing(page):
            existing_names = _read_current_warehouse_names(page)
            wh_name = (warehouse.get("warehouse_name") or "").strip()
            log.info("  FCS: [Step 4] Existing addresses: %s", existing_names)

            if _warehouse_already_added(existing_names, wh_name):
                log.info("  FCS: [Step 4] Warehouse '%s' already present — skipping ✓", wh_name)
            else:
                log.info("  FCS: [Step 4] Not found — adding warehouse address...")
                if _click_add_address_button_in_routing(page):
                    if _handle_address_dialog(page, warehouse):
                        _save_destination_tab(page)
                        any_success = True
                        log.info("  FCS: [Step 4] Warehouse address added ✓")
                    else:
                        log.warning("  FCS: [Step 4] Address dialog fill failed")
                else:
                    log.warning("  FCS: [Step 4] Add address button not found")
        else:
            log.warning("  FCS: [Step 4] Could not open Destination tab")

    # ── ⑤ Navigate back from View Routing ──────────────────────────────
    log.info("  FCS: [Step 5] Returning from View Routing...")
    try:
        back_ok = page.evaluate("""() => {
            const hdr = document.querySelector('.el-page-header__left');
            if (hdr) { hdr.click(); return true; }
            const icon = document.querySelector('.el-icon-back');
            if (icon) {
                const btn = icon.closest('button') || icon.closest('[role="button"]');
                if (btn) { btn.click(); return true; }
                icon.click(); return true;
            }
            const els = [...document.querySelectorAll('button, span, a')];
            const b = els.find(e => (e.innerText || '').trim() === 'Back');
            if (b) { b.click(); return true; }
            return false;
        }""")
        if back_ok:
            page.wait_for_timeout(2000)
        else:
            page.go_back(timeout=8000)
            page.wait_for_timeout(2000)
    except Exception as e:
        log.warning("  FCS: Back navigation failed: %s", e)

    return any_success