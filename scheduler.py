"""
scheduler.py — background auto-scan scheduler with macOS notifications.

Scans all saved watchlist accounts automatically:
  - Every 60 min during NYSE market hours  (Mon–Fri 09:30–16:00 ET)
  - Every 6 hours outside market hours
  - Uses a rolling 24-hour window (not since-midnight) to avoid missing
    posts when scans happen near midnight or across timezones
  - Sends a native macOS notification (and optionally Telegram, if
    TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are set) when any ticker is
    mentioned by 2+ distinct accounts
  - Sends a separate notification when the X session expires so you can
    reconnect before the next market open
  - Persists every auto-scan through the shared pipeline so the velocity /
    scorecard time series stays current without manual dashboard scans

Also runs a nightly forward-returns backfill (store.update_forward_returns)
at 2 AM ET to keep the account scorecard up to date.

Import-safe: app.py wraps the import in try/except so any failure here
never prevents the Flask server from starting.
"""
import asyncio
import json
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

MARKET_OPEN      = (9, 30)    # NYSE open
MARKET_CLOSE     = (16, 0)    # NYSE close
INTERVAL_MARKET  = 60 * 60    # 1 hour during market hours
INTERVAL_OFF     = 6 * 60 * 60  # 6 hours outside market hours
MIN_ACCOUNTS     = 2          # minimum distinct accounts to trigger notification

WATCHLISTS_FILE = Path(__file__).parent / "data" / "watchlists.json"

_state: dict = {
    "enabled": True,
    "last_scan_at": None,       # unix timestamp
    "next_scan_at": None,       # unix timestamp
    "last_tickers": [],         # [{ticker, accounts, total_mentions}]
    "last_error": None,
    "last_returns_update": None,  # unix timestamp of last forward-return backfill
}
_lock = threading.Lock()
_started = False


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_market_hours(dt=None) -> bool:
    """Return True if NYSE is currently open (Mon–Fri 09:30–16:00 ET)."""
    now = dt or datetime.now(ET)
    if now.weekday() >= 5:
        return False
    t = (now.hour, now.minute)
    return MARKET_OPEN <= t < MARKET_CLOSE


def market_session(dt=None) -> str:
    """Return a human-readable label for the current market session."""
    now = dt or datetime.now(ET)
    if now.weekday() >= 5:
        return "weekend"
    t = (now.hour, now.minute)
    if t < MARKET_OPEN:
        return "pre-market"
    if t >= MARKET_CLOSE:
        return "after-hours"
    return "market-open"


def _next_interval(dt=None) -> int:
    return INTERVAL_MARKET if is_market_hours(dt) else INTERVAL_OFF


def _next_due_at(anchor: float, now=None) -> float:
    """
    Unix timestamp when the next auto-scan is due: the last scan (anchor) plus
    the interval for the CURRENT market session. Recomputed every tick, so an
    off-hours 6-hour wait that spans the 9:30 ET open collapses to the 1-hour
    market interval as soon as the market opens, instead of sleeping past it.
    """
    now_dt = now or datetime.now(ET)
    return anchor + _next_interval(now_dt)


def _load_watchlist_accounts() -> list:
    """Return deduplicated list of all accounts across every saved watchlist."""
    try:
        with open(WATCHLISTS_FILE) as f:
            wl = json.load(f)
        return list({a for accs in wl.values() for a in accs})
    except Exception:
        return []


def _osascript(title: str, body: str, sound: str = "Ping") -> None:
    """Fire a native macOS notification. Best-effort — never raises.
    Inputs are sanitized to prevent AppleScript injection from scraped data."""
    def _sanitize(s: str) -> str:
        # Use json.dumps to safely encode the string as an AppleScript string
        # literal — this escapes quotes, backslashes and control characters.
        return json.dumps(str(s)[:200])

    try:
        script = (
            f'display notification {_sanitize(body)} '
            f'with title {_sanitize(title)} '
            f'sound name {_sanitize(sound)}'
        )
        subprocess.run(
            ["osascript", "-e", script],
            timeout=5,
            capture_output=True,
        )
    except Exception:
        pass


def _telegram_config():
    """Return (bot_token, chat_id) if Telegram delivery is configured, else None.

    Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env to receive auto-scan
    alerts on your phone — unlike macOS notifications, these reach you when
    you're away from the machine.
    """
    import os
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if token and chat_id:
        return token, chat_id
    return None


def _send_telegram(text: str) -> None:
    """Send a Telegram message. Best-effort — never raises."""
    import urllib.parse
    import urllib.request

    cfg = _telegram_config()
    if not cfg:
        return
    token, chat_id = cfg
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text[:4000]}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass


def _build_notification(tickers: list) -> tuple:
    """(title, body) for a list of cross-account ticker signals."""
    parts = [f"${t['ticker']} ({t['accounts']} accts)" for t in tickers[:4]]
    body = ", ".join(parts)
    if len(tickers) > 4:
        body += f" +{len(tickers) - 4} more"
    title = f"X Monitor · {len(tickers)} signal{'s' if len(tickers) != 1 else ''}"
    return title, body


def _notify(tickers: list) -> None:
    """Send notifications listing tickers mentioned by 2+ accounts."""
    if not tickers:
        return
    title, body = _build_notification(tickers)
    _osascript(title, body)
    if _telegram_config():
        _send_telegram(f"{title}\n{body}")


def _notify_session_expired() -> None:
    """Alert the user that the X session expired during an auto-scan."""
    title = "X Monitor · Session expired"
    body = "Open the dashboard and reconnect your X account before market open."
    _osascript(title, body, sound="Basso")
    if _telegram_config():
        _send_telegram(f"{title}\n{body}")


# ── Public API ────────────────────────────────────────────────────────────────

def get_status() -> dict:
    """Return a snapshot of the current scheduler state (thread-safe)."""
    with _lock:
        return dict(_state)


def set_enabled(value: bool) -> None:
    with _lock:
        _state["enabled"] = value


# ── Scan logic ────────────────────────────────────────────────────────────────

def _run_scan(scrape_fn=None, accounts=None, valid_tickers=None, price_lookup=None):
    """
    Scrape the last 24h of posts for all watchlist accounts through the SAME
    pipeline as a manual scan (so auto-scans feed the time-series DB that
    powers velocity, "new today" and the account scorecard), and return the
    tickers mentioned by MIN_ACCOUNTS or more distinct accounts.

    Returns None (without scraping) if another scan currently holds the scrape
    lock — running two Playwright sessions on the same X account concurrently
    risks getting it flagged, and both would race to rewrite session.json.

    scrape_fn / accounts / valid_tickers / price_lookup are injectable for
    offline tests.
    """
    import pipeline

    if not pipeline.SCRAPE_LOCK.acquire(blocking=False):
        return None
    try:
        if accounts is None:
            accounts = _load_watchlist_accounts()
        if not accounts:
            return []

        # Rolling 24-hour window avoids missing posts near midnight or when
        # the user is in a timezone ahead of UTC.
        since = datetime.now(timezone.utc) - timedelta(hours=24)

        if scrape_fn is None:
            from scraper import scrape_accounts

            def scrape_fn(accs, count, since_date):
                return asyncio.run(
                    scrape_accounts(accs, count=count, since_date=since_date, progress=None)
                )

        scraped = scrape_fn(accounts, 40, since)

        run = pipeline.process_scrape_results(
            scraped,
            accounts,
            count=40,
            since_raw=since.isoformat(),
            valid_tickers=valid_tickers,
            price_lookup=price_lookup,
        )
    finally:
        pipeline.SCRAPE_LOCK.release()

    qualified = [
        {
            "ticker": t["ticker"],
            "accounts": t["accounts"],
            "total_mentions": t["total_mentions"],
        }
        for t in run["combined_tickers"]
        if t["accounts"] >= MIN_ACCOUNTS
    ]
    qualified.sort(key=lambda x: (-x["accounts"], -x["total_mentions"]))
    return qualified


# ── Nightly forward-return backfill ──────────────────────────────────────────

def _should_run_returns_update() -> bool:
    """True once per calendar day, at or after 2 AM ET."""
    now = datetime.now(ET)
    if now.hour < 2:
        return False
    with _lock:
        last_ts = _state["last_returns_update"]
    if last_ts is None:
        return True
    last_date = datetime.fromtimestamp(last_ts, ET).date()
    return last_date < now.date()


def _run_returns_update() -> None:
    try:
        import store  # optional — may not be available
        if store is None:
            return
        store.update_forward_returns()
        with _lock:
            _state["last_returns_update"] = time.time()
    except Exception:
        pass  # backfill is best-effort; never crash the scheduler


# ── Background loop ───────────────────────────────────────────────────────────

TICK_SECONDS = 30


def _loop() -> None:
    # Anchor = when the last scan ran (loop start until the first one).
    # The loop wakes every TICK_SECONDS and re-evaluates the due time, so
    # market open/close and enable/disable take effect within a tick instead
    # of after a stale multi-hour sleep.
    anchor = time.time()
    while True:
        due = _next_due_at(anchor)
        with _lock:
            _state["next_scan_at"] = due
            enabled = _state["enabled"]

        # Nightly returns backfill — at most once per day at 2 AM ET; checked
        # every tick so it no longer waits for the next scan cycle to land.
        if _should_run_returns_update():
            _run_returns_update()

        if time.time() < due:
            time.sleep(TICK_SECONDS)
            continue

        if not enabled:
            # Paused: keep sliding the anchor so re-enabling doesn't fire
            # a burst of "overdue" scans immediately.
            anchor = time.time()
            time.sleep(TICK_SECONDS)
            continue

        try:
            from scraper import SessionExpired, InteractiveLoginRequired
            tickers = _run_scan()
            if tickers is None:
                # Another scan (manual, most likely) holds the scrape lock —
                # leave the anchor alone and retry on the next tick.
                time.sleep(TICK_SECONDS)
                continue
            anchor = time.time()
            with _lock:
                _state["last_scan_at"] = anchor
                _state["last_error"] = None
                _state["last_tickers"] = tickers
            _notify(tickers)
        except (SessionExpired, InteractiveLoginRequired):
            anchor = time.time()
            _notify_session_expired()
            with _lock:
                _state["last_scan_at"] = anchor
                _state["last_error"] = "X session expired — open the dashboard to reconnect"
        except Exception as exc:
            anchor = time.time()
            with _lock:
                _state["last_scan_at"] = anchor
                _state["last_error"] = str(exc)


def start() -> None:
    """Start the background scheduler daemon thread. Safe to call multiple times."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    t = threading.Thread(target=_loop, daemon=True, name="auto-scan-scheduler")
    t.start()
    print("[✓] Auto-scan scheduler started")
