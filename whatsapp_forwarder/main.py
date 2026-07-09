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
    'div[data-ref] canvas',
    'canvas',
]
_LOGGED_IN_SELECTORS = [
    '[aria-label="Chat list"]',
    '#pane-side',
    'div[data-testid="chat-list"]',
]
_CONTINUE_TO_CHAT_SELECTORS = [
    'text="Continue to Chat"',
    'a:has-text("Continue to Chat")',
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
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            self._page = page
            page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")

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

        while time.monotonic() < deadline:
            self._refresh_login_state()
            if self.state.logged_in:
                logger.info("[login] QR scanned — logged in")
                _tg_send_text("✅ WhatsApp forwarder is logged in.")
                return

            qr_loc, sel = _first_match(page, _QR_CANVAS_SELECTORS, timeout_ms=3000)
            if qr_loc is None:
                logger.warning("[login] QR canvas not found with any known selector — retrying")
                page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")
                time.sleep(3)
                continue

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

            # Invite links often land on an interstitial page with a
            # "Continue to Chat" link before the actual conversation opens.
            continue_loc, _ = _first_match(page, _CONTINUE_TO_CHAT_SELECTORS, timeout_ms=4000)
            if continue_loc is not None:
                continue_loc.click()

            box, sel = _first_match(page, _MESSAGE_BOX_SELECTORS, timeout_ms=15000)
            if box is None:
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
