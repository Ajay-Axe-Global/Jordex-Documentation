"""
jordex/login.py — Per-Label Jordex Session Manager
====================================================
Each service creates its own JordexSession with a unique session_dir.
This gives every label its own isolated Jordex browser session.

Usage:
    from jordex.login import JordexSession
    session = JordexSession(service_key="arrival_notice", pw=pw_instance)
    page = session.start()
    ...
    session.close()
"""
import os
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
import logging

from config import (
    JORDEX_EMAIL, JORDEX_PASSWORD, JORDEX_BASE_URL, JORDEX_OCEAN_URL,
    BROWSER_HEADLESS, BROWSER_ZOOM,
    get_jordex_profile,
)

log = logging.getLogger("jordex.login")


class JordexSession:
    def __init__(self, service_key: str, headless: bool = BROWSER_HEADLESS, pw=None):
        self.service_key = service_key
        self.headless    = headless
        self.pw          = pw
        self.owns_pw     = pw is None
        self.context     = None
        self.page        = None
        self.session_dir = get_jordex_profile(service_key)

    def start(self):
        log.info(f"[{self.service_key}] Starting Jordex session...")
        Path(self.session_dir).mkdir(parents=True, exist_ok=True)

        if self.owns_pw and self.pw is None:
            self.pw = sync_playwright().start()

        self.context = self.pw.chromium.launch_persistent_context(
            user_data_dir=self.session_dir,
            headless=self.headless,
            channel="chrome",
            viewport=None,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
            ignore_default_args=["--enable-automation"],
        )

        self.page = self.context.new_page()
        self.page.set_default_timeout(30000)

        log.info(f"[{self.service_key}] Navigating to {JORDEX_BASE_URL}...")
        try:
            self.page.goto(JORDEX_BASE_URL, wait_until="commit", timeout=60000)
            self.page.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception as e:
            log.warning(f"[{self.service_key}] initial goto timeout or error: {e}")

        self.apply_zoom()
        self.page.wait_for_timeout(1000)

        if self._is_on_app():
            if self._wait_for_dashboard_ready(timeout=5000):
                log.info(f"[{self.service_key}] Jordex session reused. Dashboard ready.")
                return self.page
            elif not self._is_on_app():
                pass  # Redirected away, continue to login flow
            else:
                try:
                    self.page.reload(wait_until="commit", timeout=15000)
                    self.page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception as e:
                    log.warning(f"[{self.service_key}] Reload failed: {e}")
                    try:
                        self.page.goto(JORDEX_BASE_URL, wait_until="commit", timeout=15000)
                    except Exception:
                        pass

                if self._wait_for_dashboard_ready(timeout=10000):
                    log.info(f"[{self.service_key}] Dashboard ready after refresh.")
                    return self.page

        # Login flow
        if self._is_on_auth0():
            self._handle_auth0()

        if self._is_on_microsoft():
            self._handle_microsoft_login()
        elif self._is_on_auth0():
            self._handle_auth0()
            if self._is_on_microsoft():
                self._handle_microsoft_login()

        self._wait_for_dashboard_ready(timeout=15000)
        if not self._is_on_app():
            self.page.goto(JORDEX_BASE_URL, wait_until="load")
            self.page.wait_for_timeout(3000)

        if self._is_on_app():
            log.info(f"[{self.service_key}] Jordex Login complete.")
            return self.page

        log.error(f"[{self.service_key}] Jordex Login failed.")
        return self.page

    def apply_zoom(self):
        try:
            self.page.evaluate(f"document.body.style.zoom = '{BROWSER_ZOOM}'")
        except Exception:
            pass

    def _is_on_app(self):
        url = self.page.url.lower()
        return "jit.jordex.com" in url and not any(
            x in url for x in ["auth.myfreight.nl", "login.microsoftonline", "login.live.com"]
        )

    def _is_on_auth0(self):
        return "auth.myfreight.nl" in self.page.url.lower()

    def _is_on_microsoft(self):
        url = self.page.url.lower()
        return any(x in url for x in ["login.microsoftonline", "login.live.com"])

    def _wait_for_dashboard_ready(self, timeout=20000):
        try:
            self.page.locator(
                'text=Shipments , th:has-text("Shipment") , table , nav'
            ).first.wait_for(state="visible", timeout=timeout)
            return True
        except PlaywrightTimeout:
            return False

    def _visible(self, selector, timeout=3000):
        try:
            self.page.locator(selector).first.wait_for(state="visible", timeout=timeout)
            return True
        except PlaywrightTimeout:
            return False

    def _click_if_visible(self, selector, timeout=3000):
        if self._visible(selector, timeout):
            self.page.locator(selector).first.click()
            return True
        return False

    def _handle_auth0(self):
        try:
            self.page.wait_for_url(
                lambda url: "auth.myfreight.nl" not in url.lower(), timeout=3000
            )
            return
        except PlaywrightTimeout:
            pass

        log.info(f"[{self.service_key}] Clicking Continue with Azure...")
        self.page.wait_for_load_state("domcontentloaded")
        try:
            self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass

        for sel in [
            'button:has-text("Continue with Azure")',
            'a:has-text("Continue with Azure")',
            '[data-provider="windowslive"]',
            '[data-provider="waad"]',
        ]:
            if self._click_if_visible(sel, timeout=3000):
                break

        try:
            self.page.wait_for_url(
                lambda url: "login.microsoftonline" in url or "login.live" in url,
                timeout=10000,
            )
        except Exception:
            pass

    def _handle_microsoft_login(self):
        log.info(f"[{self.service_key}] Microsoft login page...")
        self.page.wait_for_load_state("domcontentloaded")

        try:
            self.page.wait_for_url(
                lambda url: not any(x in url for x in ["login.microsoftonline", "login.live.com"]),
                timeout=3000,
            )
            return
        except PlaywrightTimeout:
            pass

        self.page.wait_for_timeout(1000)

        # Pick an account
        if self._is_on_microsoft() and self._visible('text="Pick an account"', timeout=3000):
            target = JORDEX_EMAIL.lower()
            body = (self.page.locator("body").inner_text() or "").lower()
            if target in body:
                for loc in [
                    self.page.get_by_text(target, exact=False),
                    self.page.locator(f'small:has-text("{target}")'),
                ]:
                    if loc.count() > 0:
                        try:
                            loc.first.click(timeout=3000)
                            break
                        except Exception:
                            pass
            else:
                self._click_if_visible("#otherTile", timeout=2000) or \
                    self._click_if_visible('text="Use another account"', timeout=2000)
            self.page.wait_for_load_state("domcontentloaded")
            self.page.wait_for_timeout(1000)

        # Email
        if self._is_on_microsoft() and self._visible('input[type="email"], input[name="loginfmt"]', timeout=2000):
            log.info(f"[{self.service_key}] Entering Jordex email")
            inp = self.page.locator('input[type="email"], input[name="loginfmt"]').first
            inp.fill("")
            inp.fill(JORDEX_EMAIL)
            self.page.wait_for_timeout(500)
            self.page.keyboard.press("Enter")
            self.page.wait_for_timeout(2000)

        # Password
        if self._is_on_microsoft() and self._visible('input[type="password"], input[name="passwd"]', timeout=3000):
            log.info(f"[{self.service_key}] Entering Jordex password")
            inp = self.page.locator('input[type="password"], input[name="passwd"]').first
            inp.fill(JORDEX_PASSWORD)
            self.page.wait_for_timeout(500)
            self.page.keyboard.press("Enter")
            self.page.wait_for_timeout(2000)

        # Stay signed in
        if self._is_on_microsoft() and self._visible('text="Stay signed in?"', timeout=3000):
            try:
                self.page.evaluate("document.querySelector('#idSIButton9').click()")
            except Exception:
                pass
            self.page.wait_for_timeout(1000)

        # Permissions
        if self._visible("#idBtn_Accept", timeout=2000):
            try:
                self.page.evaluate("document.querySelector('#idBtn_Accept').click()")
            except Exception:
                pass
            self.page.wait_for_timeout(2000)

        if not self._is_on_app():
            self._handle_mfa()

    def _handle_mfa(self):
        if self._is_on_app():
            return
        log.warning(f"[{self.service_key}] MFA required. Approve on phone within 120s.")
        try:
            self.page.wait_for_url(lambda url: "jit.jordex.com" in url, timeout=120000)
            log.info(f"[{self.service_key}] MFA approved.")
        except PlaywrightTimeout:
            log.error(f"[{self.service_key}] MFA timeout.")

    def close(self):
        if self.context:
            try:
                self.context.close()
            except Exception:
                pass
        if self.pw and self.owns_pw:
            try:
                self.pw.stop()
            except Exception:
                pass
