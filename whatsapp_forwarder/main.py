"""
whatsapp_forwarder/main.py
~~~~~~~~~~~~~~~~~~~~~~~~~~
Separate service — its own container, its own process, its own Railway
volume — that owns ALL WhatsApp Web browser automation (Playwright). This is
deliberately isolated from the main Tracker-alert bot: the main bot has no
Playwright dependency and only ever talks to this service over one small,
best-effort HTTP call (see ../whatsapp_client.py). If this service is down,
slow, crashes, or its WhatsApp session is logged out, the main bot's Telegram
alerts are completely unaffected — it just stops receiving 200s here.

One WhatsApp account is logged into ONE persistent browser profile (via
Playwright's launch_persistent_context — NOT storage_state, because WhatsApp
Web's session lives mostly in IndexedDB, which storage_state() does not
capture; only a full persistent profile directory survives a restart).
That one account posts into MANY different users' Channels/Communities,
one invite link per forwarded message — see database.whatsapp_channels on
the main bot side for how a user's own channel gets approved.

HTTP surface:
  POST /forward   Bearer-secret protected. Body: {"invite_link": str, "text": str}.
                   Enqueues the message and returns immediately (202) — the
                   actual browser work happens on a dedicated background
                   thread, sequentially, with a pacing delay between sends.
  GET  /status     Unauthenticated, no sensitive data: {"logged_in": bool,
                   "queue_length": int, "last_error": str|None}.

Bootstrapping a session (first deploy, or after a logout): this container
has no screen, so the login QR code is screenshotted and sent to the admin
as a Telegram photo (direct HTTP call to the Bot API — no aiogram dependency
here either). It re-sends an updated photo whenever the QR image actually
changes (WhatsApp rotates it roughly every ~20s) until a login is detected.

IMPORTANT — this file cannot be verified against the real WhatsApp Web DOM
from this environment (no live network/browser access here). The CSS
selectors below are best-effort and WILL likely need adjustment once run
live — see README.md for the iteration loop. Every DOM interaction is
wrapped so a wrong/stale selector logs clearly and fails one queue item
rather than crashing the worker thread.
"""

import io
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from flask import Flask, jsonify, request
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("whatsapp_forwarder")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PORT = int(os.getenv("PORT", "8080"))
FORWARDER_SECRET = os.getenv("WHATSAPP_FORWARDER_SECRET", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "")
# Railway volume mount point — the browser profile (and therefore the login
# session) lives here so it survives redeploys/restarts. Must be an attached
# volume, not ephemeral container storage.
PROFILE_DIR = os.getenv("WHATSAPP_PROFILE_DIR", "/data/whatsapp-profile")
DEBUG_DIR = os.getenv("WHATSAPP_DEBUG_DIR", "/data/debug")
HEADLESS = os.getenv("WHATSAPP_HEADLESS", "true").lower() != "false"
# Delay between processing consecutive queued messages — deliberately
# conservative to look less like a bot hammering WhatsApp Web and reduce ban
# risk. Tune via env once live behaviour is observed.
FORWARD_PACING_SECONDS = float(os.getenv("WHATSAPP_FORWARD_PACING_SECONDS", "12"))
QR_BOOTSTRAP_TIMEOUT_SECONDS = float(os.getenv("WHATSAPP_QR_TIMEOUT_SECONDS", "600"))
# WhatsApp Web is a heavy React SPA — domcontentloaded fires long before the
# QR canvas actually mounts. A first live deploy showed canvas_count=0 AND an
# empty document.title (normally "WhatsApp") right after navigation, which
# points at "JS hasn't finished mounting yet" rather than a stale selector.
# This is a floor wait on top of an explicit networkidle wait (best-effort;
# WhatsApp Web polls in the background so networkidle may never fully settle
# — see _goto_whatsapp).
INITIAL_LOAD_WAIT_SECONDS = float(os.getenv("WHATSAPP_INITIAL_LOAD_WAIT_SECONDS", "10"))

if not FORWARDER_SECRET:
    logger.warning(
        "WHATSAPP_FORWARDER_SECRET is not set — /forward will reject every "
        "request. Set it to the same value configured on the main bot."
    )
if not BOT_TOKEN or not ADMIN_USER_ID:
    logger.warning(
        "BOT_TOKEN / ADMIN_USER_ID not set — QR-bootstrap photos can't be "
        "sent to Telegram. Login will need another way to reach the QR."
    )

# ---------------------------------------------------------------------------
# Telegram helper (direct HTTP, no aiogram — mirrors dashboard.py's _tg_send)
# ---------------------------------------------------------------------------


def _tg_send_photo(photo_bytes: bytes, caption: str) -> None:
    if not BOT_TOKEN or not ADMIN_USER_ID:
        return
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data={"chat_id": ADMIN_USER_ID, "caption": caption},
            files={"photo": ("qr.png", io.BytesIO(photo_bytes), "image/png")},
            timeout=15.0,
        )
        if resp.status_code != 200:
            logger.error(f"[telegram] sendPhoto failed: HTTP {resp.status_code} {resp.text[:200]}")
    except Exception as exc:
        logger.error(f"[telegram] sendPhoto raised: {exc}")


def _tg_send_text(text: str) -> None:
    if not BOT_TOKEN or not ADMIN_USER_ID:
        return
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": ADMIN_USER_ID, "text": text},
            timeout=10.0,
        )
        if resp.status_code != 200:
            logger.error(f"[telegram] sendMessage failed: HTTP {resp.status_code} {resp.text[:200]}")
    except Exception as exc:
        logger.error(f"[telegram] sendMessage raised: {exc}")


# ---------------------------------------------------------------------------
# Selectors — BEST EFFORT, unverified against a live session. See README.md.
# Kept as ordered candidate lists so a DOM change breaks one entry, not the
# whole feature; _first_match() tries each in turn and logs what worked.
# ---------------------------------------------------------------------------
_QR_CANVAS_SELECTORS = [
    'canvas[aria-label="Scan this QR code to link a device!"]',
    'canvas[aria-label*="Scan"]',
    'div[data-ref] canvas',
    'div._akau canvas',
    'div[data-testid="qrcode"] canvas',
    'landing-window canvas',
    # Last resort: WhatsApp Web's login page has no other canvas on it, so
    # "any visible canvas at all" is a safe fallback when every more
    # specific selector above is stale.
    'canvas',
]
_LOGGED_IN_SELECTORS = [
    '[aria-label="Chat list"]',
    '#pane-side',
    'div[data-testid="chat-list"]',
]
_INTERSTITIAL_CONTINUE_SELECTORS = [
    # Group/Community invite links (chat.whatsapp.com/...) land on a
    # "Continue to Chat" interstitial before the conversation opens.
    'text="Continue to Chat"',
    'a:has-text("Continue to Chat")',
    # Channel invite links (whatsapp.com/channel/...) land on a different
    # preview/landing page instead, showing the channel icon plus "Open App"
    # and "Continue in web" buttons — confirmed via a live screenshot. We
    # want "Continue in web" specifically; "Open App" tries to hand off to a
    # native app deep link, which does nothing useful in a headless
    # container.
    'text="Continue in web"',
    'button:has-text("Continue in web")',
    'a:has-text("Continue in web")',
    'div[role="button"]:has-text("Continue in web")',
]
_MESSAGE_BOX_SELECTORS = [
    'div[contenteditable="true"][data-tab="10"]',
    'footer div[contenteditable="true"]',
    'div[title="Type a message"]',
]
_SEND_BUTTON_SELECTORS = [
    'button[aria-label="Send"]',
    'span[data-icon="send"]',
]


def _first_match(page: Page, selectors: list[str], timeout_ms: int = 5000):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout_ms)
            return loc, sel
        except PlaywrightTimeoutError:
            continue
    return None, None


# ---------------------------------------------------------------------------
# Anti-detection: headless Chromium is commonly fingerprinted by sites (the
# `navigator.webdriver` flag, a missing `window.chrome`, an empty plugins
# list, a non-standard user-agent string are the classic tells). A live
# deploy showed an empty document.title and zero canvas elements right after
# navigating to web.whatsapp.com, which is consistent with the app bundle
# never mounting at all — worth ruling this out even though it's only one of
# several possible causes (the other being "just needs more time to load",
# handled separately via INITIAL_LOAD_WAIT_SECONDS below).
# ---------------------------------------------------------------------------
_REALISTIC_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
if (originalQuery) {
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters)
    );
}
"""


def _goto_whatsapp(page: Page) -> None:
    """Navigate to WhatsApp Web and give its React bundle real time to mount
    before anything tries to find the QR canvas. domcontentloaded alone fires
    long before the app renders anything — a first live deploy hit
    canvas_count=0 with an empty document.title (normally "WhatsApp") right
    after a bare domcontentloaded goto, which points at "checked too early"
    as at least part of the problem."""
    page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        # WhatsApp Web polls in the background even once loaded, so
        # networkidle may never fully settle — not itself an error.
        logger.info("[login] networkidle wait timed out (page may still be polling) — continuing")
    time.sleep(INITIAL_LOAD_WAIT_SECONDS)


# ---------------------------------------------------------------------------
# Worker: owns the ONE Playwright browser/page. Everything that touches
# `page` runs on this worker's own thread — Playwright's sync API is not
# thread-safe, so Flask request handlers only ever touch the thread-safe
# queue, never `page` directly.
# ---------------------------------------------------------------------------

@dataclass
class WorkerState:
    logged_in: bool = False
    last_error: str | None = None
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class WhatsAppWorker:
    def __init__(self):
        self.queue: "queue.Queue[dict]" = queue.Queue()
        self.state = WorkerState()
        self._page: Page | None = None

    # -- lifecycle ----------------------------------------------------------

    def run_forever(self) -> None:
        """Entry point for the dedicated worker thread. Never returns."""
        os.makedirs(PROFILE_DIR, exist_ok=True)
        os.makedirs(DEBUG_DIR, exist_ok=True)
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                PROFILE_DIR,
                headless=HEADLESS,
                viewport={"width": 1280, "height": 800},
                user_agent=_REALISTIC_USER_AGENT,
                locale="en-US",
                args=["--disable-blink-features=AutomationControlled"],
            )
            context.add_init_script(_STEALTH_INIT_SCRIPT)
            page = context.pages[0] if context.pages else context.new_page()
            self._page = page
            _goto_whatsapp(page)

            self._ensure_logged_in()

            while True:
                try:
                    item = self.queue.get(timeout=5)
                except queue.Empty:
                    # Idle tick: confirm the session hasn't silently logged
                    # out (WhatsApp Web can log a device out remotely).
                    self._refresh_login_state()
                    continue

                if not self.state.logged_in:
                    self._ensure_logged_in()
                if self.state.logged_in:
                    self._process_forward(item)
                    time.sleep(FORWARD_PACING_SECONDS)
                else:
                    logger.error(
                        f"[forward] dropped one message (still not logged in): {item.get('invite_link')}"
                    )

    # -- login / QR bootstrap ------------------------------------------------

    def _refresh_login_state(self) -> None:
        page = self._page
        loc, _ = _first_match(page, _LOGGED_IN_SELECTORS, timeout_ms=3000)
        self.state.logged_in = loc is not None

    def _diagnostic_snapshot(self, context: str):
        """Log enough about current page state to diagnose a selector miss
        without needing shell/volume access to the Railway container: page
        title, URL, how many <canvas> elements exist at all (0 means the
        selectors aren't just stale — WhatsApp isn't even rendering a QR
        canvas), and navigator.webdriver (True is a strong signal the page
        detected automation before the real UI ever mounted). Returns the
        canvas count (int) so the caller can decide whether to escalate to a
        full-page screenshot immediately rather than waiting."""
        page = self._page
        try:
            title = page.title()
        except Exception as exc:
            title = f"<error: {exc}>"
        try:
            url = page.url
        except Exception as exc:
            url = f"<error: {exc}>"
        try:
            canvas_count = page.locator("canvas").count()
        except Exception as exc:
            canvas_count = f"<error: {exc}>"
        try:
            webdriver_flag = page.evaluate("navigator.webdriver")
        except Exception as exc:
            webdriver_flag = f"<error: {exc}>"
        logger.warning(
            f"[login] {context} — title={title!r} url={url!r} "
            f"canvas_count={canvas_count} navigator.webdriver={webdriver_flag}"
        )
        return canvas_count

    def _log_body_snapshot(self) -> None:
        """Dump enough of the real page structure to tell "still loading"
        apart from "showing something unexpected" (an error message, a
        captcha, a blocked-browser warning) — sent to Telegram as text too,
        since a screenshot of a blank/white page alone doesn't explain WHY
        it's blank. Called once per bootstrap attempt (paired with
        _send_full_page_fallback, same dedup guard) rather than on every
        single miss, to avoid repeating the same diagnosis every ~30s."""
        page = self._page
        try:
            info = page.evaluate(
                """() => {
                    const body = document.body;
                    if (!body) return {hasBody: false};
                    const children = Array.from(body.children).map(el => ({
                        tag: el.tagName,
                        id: el.id,
                        className: (el.className || '').toString(),
                    }));
                    return {
                        hasBody: true,
                        readyState: document.readyState,
                        bodyChildCount: body.children.length,
                        children: children,
                        bodyText: (body.innerText || '').slice(0, 500),
                        bodyHtml: body.innerHTML.slice(0, 4000),
                    };
                }"""
            )
        except Exception as exc:
            logger.error(f"[login] body snapshot evaluate failed: {exc}")
            return

        logger.warning(f"[login] body snapshot: {info}")
        summary = (
            f"readyState={info.get('readyState')}\n"
            f"bodyChildCount={info.get('bodyChildCount')}\n"
            f"children={info.get('children')}\n\n"
            f"text: {info.get('bodyText', '')!r}"
        )
        _tg_send_text(f"🔍 canvas_count=0 — page body snapshot:\n{summary}"[:4000])

    def _send_full_page_fallback(self) -> None:
        """Last resort when no canvas (QR-specific or generic) can be found
        at all: screenshot the WHOLE page and DM it, so the admin can see
        exactly what WhatsApp Web is actually showing (error dialog, cookie
        popup, different UI entirely) instead of just a log line saying
        "not found"."""
        try:
            shot = self._page.screenshot(full_page=True)
            _tg_send_photo(
                shot,
                "⚠️ Couldn't find any QR code canvas on the page. Here's the "
                "full page instead — check for a cookie/consent popup, an "
                "error dialog, or an unexpected screen.",
            )
        except Exception as exc:
            logger.error(f"[login] full-page fallback screenshot failed: {exc}")

    def _ensure_logged_in(self) -> None:
        page = self._page
        self._refresh_login_state()
        if self.state.logged_in:
            return

        logger.info("[login] not logged in — starting QR bootstrap")
        _tg_send_text(
            "📲 WhatsApp forwarder needs a fresh login. Sending the QR code "
            "now — open WhatsApp on the linked device > Linked Devices > "
            "Link a Device, and scan it. It refreshes automatically; a new "
            "photo is sent whenever it changes."
        )
        deadline = time.monotonic() + QR_BOOTSTRAP_TIMEOUT_SECONDS
        last_qr_bytes: bytes | None = None
        consecutive_misses = 0
        sent_full_page_fallback = False

        while time.monotonic() < deadline:
            self._refresh_login_state()
            if self.state.logged_in:
                logger.info("[login] QR scanned — logged in")
                _tg_send_text("✅ WhatsApp forwarder is logged in.")
                return

            qr_loc, sel = _first_match(page, _QR_CANVAS_SELECTORS, timeout_ms=3000)
            if qr_loc is None:
                consecutive_misses += 1
                canvas_count = self._diagnostic_snapshot(
                    f"QR canvas not found with any known selector (miss #{consecutive_misses})"
                )
                # canvas_count == 0 means this isn't a stale-selector problem
                # at all — nothing resembling a QR ever rendered — so escalate
                # to a full-page screenshot immediately rather than waiting
                # for 3 misses (~30s+) to find out something is more seriously
                # wrong (still loading, blocked, unexpected page).
                urgent = canvas_count == 0
                if not sent_full_page_fallback and (urgent or consecutive_misses >= 3):
                    if urgent:
                        self._log_body_snapshot()
                    self._send_full_page_fallback()
                    sent_full_page_fallback = True
                # Reload periodically rather than every single miss — reloading
                # every 3s never gives a slow-rendering page a chance to finish,
                # and constant reloads would themselves explain a permanent
                # "not found" (the QR barely has time to render before the next
                # goto tears it down). Reload goes through _goto_whatsapp so it
                # gets the same networkidle + floor wait as the initial load.
                if consecutive_misses % 10 == 0:
                    logger.info("[login] reloading page after repeated misses")
                    try:
                        _goto_whatsapp(page)
                    except Exception as exc:
                        logger.error(f"[login] reload failed: {exc}")
                    sent_full_page_fallback = False  # allow one fresh fallback shot post-reload
                time.sleep(3)
                continue

            consecutive_misses = 0
            logger.info(f"[login] QR canvas found via selector: {sel!r}")
            try:
                shot = qr_loc.screenshot()
            except Exception as exc:
                logger.error(f"[login] QR screenshot failed: {exc}")
                time.sleep(3)
                continue

            if shot != last_qr_bytes:
                last_qr_bytes = shot
                _tg_send_photo(shot, "Scan to link WhatsApp (refreshes automatically)")

            time.sleep(3)

        logger.error(
            f"[login] QR bootstrap timed out after {QR_BOOTSTRAP_TIMEOUT_SECONDS}s — "
            "will keep checking at a relaxed pace; hit /status or wait for the "
            "next queued message to retrigger a fresh QR send."
        )
        self.state.last_error = "qr_bootstrap_timeout"

    # -- sending --------------------------------------------------------------

    def _process_forward(self, item: dict) -> None:
        invite_link = item["invite_link"]
        text = item["text"]
        page = self._page
        try:
            page.goto(invite_link, wait_until="domcontentloaded", timeout=30000)
            # Give the landing/interstitial page a moment to render before
            # looking for its "continue" button — same class of SPA-render
            # delay as the QR bootstrap flow (domcontentloaded fires well
            # before the app has actually drawn anything).
            time.sleep(3)

            # Invite links land on an intermediate page before the real
            # conversation opens — which one depends on the link type (see
            # _INTERSTITIAL_CONTINUE_SELECTORS). Click whichever "continue"
            # control is present; if neither is, we're probably already on
            # the conversation itself (e.g. re-sending to a channel already
            # open/followed in this session).
            continue_loc, continue_sel = _first_match(
                page, _INTERSTITIAL_CONTINUE_SELECTORS, timeout_ms=6000
            )
            if continue_loc is not None:
                logger.info(f"[forward] clicking interstitial continue button: {continue_sel!r}")
                continue_loc.click()
                time.sleep(2)  # let the actual chat/channel view start rendering
            else:
                logger.info("[forward] no interstitial continue button found — assuming already on the conversation")

            box, sel = _first_match(page, _MESSAGE_BOX_SELECTORS, timeout_ms=15000)
            if box is None:
                self._log_editable_elements()
                raise RuntimeError("message box not found (selectors may be stale)")

            box.click()
            box.type(text, delay=15)

            send_btn, _ = _first_match(page, _SEND_BUTTON_SELECTORS, timeout_ms=5000)
            if send_btn is not None:
                send_btn.click()
            else:
                page.keyboard.press("Enter")

            logger.info(f"[forward] sent to {invite_link}")
            self.state.last_error = None
        except Exception as exc:
            logger.error(f"[forward] failed for {invite_link}: {exc}")
            self.state.last_error = str(exc)
            self._save_debug_screenshot(invite_link)

    def _log_editable_elements(self) -> None:
        """When the message-box selector fails, dump every contenteditable /
        input / textarea / role="textbox" element actually present on the
        page (tag, id, class, role, aria-label, placeholder, data-tab,
        title, visibility) to both the logs and a Telegram text message —
        so the next selector fix can be made from real DOM data instead of
        another guess."""
        page = self._page
        try:
            elements = page.evaluate(
                """() => {
                    const sel = '[contenteditable], input, textarea, [role="textbox"]';
                    return Array.from(document.querySelectorAll(sel)).slice(0, 20).map(el => ({
                        tag: el.tagName,
                        id: el.id,
                        className: (el.className || '').toString(),
                        role: el.getAttribute('role'),
                        ariaLabel: el.getAttribute('aria-label'),
                        placeholder: el.getAttribute('placeholder'),
                        dataTab: el.getAttribute('data-tab'),
                        title: el.getAttribute('title'),
                        contentEditable: el.getAttribute('contenteditable'),
                        visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
                        outerHtmlSnippet: el.outerHTML.slice(0, 200),
                    }));
                }"""
            )
        except Exception as exc:
            logger.error(f"[forward] editable-elements dump failed: {exc}")
            return

        logger.warning(f"[forward] editable/input-like elements on page ({len(elements)} found): {elements}")
        if not elements:
            summary = "No contenteditable/input/textarea/role=textbox elements found on the page at all."
        else:
            lines = []
            for i, el in enumerate(elements):
                lines.append(
                    f"{i + 1}. <{el['tag']}> id={el['id']!r} class={el['className']!r} "
                    f"role={el['role']!r} aria-label={el['ariaLabel']!r} "
                    f"placeholder={el['placeholder']!r} data-tab={el['dataTab']!r} "
                    f"title={el['title']!r} contenteditable={el['contentEditable']!r} "
                    f"visible={el['visible']}"
                )
            summary = "\n".join(lines)
        _tg_send_text(f"🔍 Message box not found — editable-like elements on page:\n{summary}"[:4000])

    def _save_debug_screenshot(self, invite_link: str) -> None:
        try:
            path = os.path.join(
                DEBUG_DIR, f"fail-{int(time.time())}.png"
            )
            self._page.screenshot(path=path)
            logger.info(f"[debug] saved failure screenshot to {path}")
            with open(path, "rb") as f:
                _tg_send_photo(
                    f.read(),
                    f"⚠️ WhatsApp forward failed for {invite_link} — see logs.",
                )
        except Exception as exc:
            logger.error(f"[debug] could not save/send failure screenshot: {exc}")


worker = WhatsAppWorker()


# ---------------------------------------------------------------------------
# Flask app — thin HTTP layer, never touches Playwright directly.
# ---------------------------------------------------------------------------

def create_app() -> Flask:
    app = Flask(__name__)

    @app.route("/forward", methods=["POST"])
    def forward():
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {FORWARDER_SECRET}"
        if not FORWARDER_SECRET or auth != expected:
            return jsonify({"error": "unauthorized"}), 401

        data = request.get_json(silent=True) or {}
        invite_link = (data.get("invite_link") or "").strip()
        text = data.get("text") or ""
        if not invite_link or not text:
            return jsonify({"error": "invite_link and text are required"}), 400

        worker.queue.put({"invite_link": invite_link, "text": text})
        return jsonify({"queued": True, "queue_length": worker.queue.qsize()}), 202

    @app.route("/status", methods=["GET"])
    def status():
        return jsonify({
            "logged_in": worker.state.logged_in,
            "queue_length": worker.queue.qsize(),
            "last_error": worker.state.last_error,
            "started_at": worker.state.started_at,
        })

    return app


def main() -> None:
    threading.Thread(target=worker.run_forever, name="whatsapp-worker", daemon=True).start()

    app = create_app()
    from waitress import serve
    logger.info(f"[http] serving on 0.0.0.0:{PORT}")
    serve(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
