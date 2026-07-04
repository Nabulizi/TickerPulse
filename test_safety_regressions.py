"""
Offline regression tests for safety and persistence behavior.

Run:
    python3 test_safety_regressions.py
"""
import asyncio
import json
import os
import queue
import tempfile

import pytest
from datetime import timezone
from pathlib import Path

import app
import import_cookies
import store


def test_parse_since_date_accepts_zulu_timestamp():
    parsed = app._parse_since_date("2026-06-11T12:00:00Z", "America/New_York")
    assert parsed.tzinfo == timezone.utc
    assert parsed.isoformat() == "2026-06-11T12:00:00+00:00"


def test_register_scan_rejects_second_active_scan():
    original_scans = dict(app._scans)
    original_max_active = app.MAX_ACTIVE_SCANS
    try:
        app._scans.clear()
        app.MAX_ACTIVE_SCANS = 1

        q1 = queue.Queue()
        q2 = queue.Queue()
        assert app._register_scan("scan-1", q1) is True
        assert app._register_scan("scan-2", q2) is False

        app._complete_scan("scan-1", {"type": "error", "message": "done"})
        assert q1.get_nowait()["type"] == "error"
        assert app._register_scan("scan-2", q2) is True
    finally:
        app._scans.clear()
        app._scans.update(original_scans)
        app.MAX_ACTIVE_SCANS = original_max_active


def test_store_keeps_same_source_post_for_each_account():
    original_db_path = store.DB_PATH
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store.DB_PATH = Path(tmp) / "scraper.db"
            post = {
                "text": "Watching $NVDA here",
                "posted_at": "2026-06-11T12:00:00.000Z",
                "url": "https://x.com/source/status/12345",
                "likes": 1,
                "reposts": 2,
                "replies": 3,
                "views": 4,
                "is_repost": True,
            }
            occurrence = {
                "post_index": 1,
                "posted_at": post["posted_at"],
                "confidence": "cashtag",
                "sentiment": "bullish",
                "sentiment_score": 0.5,
                "signal_weight": 1.0,
                "is_trailing_tag": False,
            }
            run = {
                "combined_tickers": [{"ticker": "NVDA", "price": 100.0}],
                "results": {
                    "alpha": {
                        "error": None,
                        "follower_count": 10,
                        "posts": [post],
                        "tickers": [{"ticker": "NVDA", "occurrences": [occurrence]}],
                    },
                    "beta": {
                        "error": None,
                        "follower_count": 20,
                        "posts": [post],
                        "tickers": [{"ticker": "NVDA", "occurrences": [occurrence]}],
                    },
                },
            }

            store.record_run(run)

            with store._conn() as conn:
                rows = conn.execute(
                    "SELECT account, post_id, ticker FROM mentions ORDER BY account"
                ).fetchall()

            assert rows == [
                ("alpha", "alpha:12345", "NVDA"),
                ("beta", "beta:12345", "NVDA"),
            ]
    finally:
        store.DB_PATH = original_db_path


def test_import_cookies_writes_owner_only_session_file():
    original_session_file = import_cookies.SESSION_FILE
    try:
        with tempfile.TemporaryDirectory() as tmp:
            import_cookies.SESSION_FILE = Path(tmp) / "session.json"
            import_cookies._write_session_secure({"cookies": [], "origins": []})
            mode = os.stat(import_cookies.SESSION_FILE).st_mode & 0o777
            assert mode == 0o600
    finally:
        import_cookies.SESSION_FILE = original_session_file


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"passed {test.__name__}")
    print(f"{len(tests)}/{len(tests)} passed")


def test_velocity_endpoint_accepts_share_class_tickers():
    original_db_path = store.DB_PATH
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store.DB_PATH = Path(tmp) / "scraper.db"
            client = app.app.test_client()
            assert client.get("/velocity/BRK.B").status_code == 200
            assert client.get("/velocity/NVDA").status_code == 200
            assert client.get("/velocity/AB%2FCD").status_code in (400, 404)
            assert client.get("/velocity/TOOLONGG").status_code == 400
    finally:
        store.DB_PATH = original_db_path


def test_refresh_session_always_raises():
    """Local-only: _refresh_session has no fallback and must raise."""
    import scraper

    with pytest.raises(scraper.SessionExpired):
        asyncio.run(scraper._refresh_session())


def test_headless_mode_can_prefer_google_login():
    import scraper

    saved = {k: os.environ.get(k) for k in ("XTS_CONNECT_HEADLESS", "X_USERNAME", "X_PASSWORD", "X_EMAIL", "GOOGLE_EMAIL", "GOOGLE_PASSWORD", "X_LOGIN_METHOD")}
    try:
        # A plain X username must NOT be treated as a Google email (old bug).
        os.environ["XTS_CONNECT_HEADLESS"] = "1"
        os.environ["X_USERNAME"] = "user"
        os.environ["X_PASSWORD"] = "pass"
        os.environ.pop("X_EMAIL", None)
        os.environ.pop("GOOGLE_EMAIL", None)
        os.environ.pop("GOOGLE_PASSWORD", None)
        os.environ.pop("X_LOGIN_METHOD", None)
        assert scraper._should_prefer_google_login(x_username="user", x_password="pass") is False, \
            "X_USERNAME alone must not trigger Google login"

        # Google login IS preferred when X_LOGIN_METHOD=google is explicit.
        os.environ["X_LOGIN_METHOD"] = "google"
        os.environ["GOOGLE_EMAIL"] = "user@gmail.com"
        os.environ["GOOGLE_PASSWORD"] = "gpass"
        assert scraper._should_prefer_google_login(x_username="user", x_password="pass") is True, \
            "X_LOGIN_METHOD=google with GOOGLE_EMAIL should prefer Google"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _FakeContext:
    """Stand-in for a Playwright BrowserContext — storage_state() only ever
    writes {cookies, origins}, exactly like the real API."""

    async def storage_state(self, path):
        Path(path).write_text(json.dumps({"cookies": [{"name": "ct0", "value": "x"}], "origins": []}))


def test_scan_session_save_preserves_user_agent_pin():
    """
    Regression test for the Render hang: scrape_accounts() used to persist
    the session with a bare `context.storage_state()` call, which discards
    the `_user_agent` field. Cloudflare's cf_clearance cookie is bound to
    the UA that solved the challenge (see commit 6fea45d), so losing that
    field after the first scan on Render causes every subsequent scan to
    present a mismatched UA and hang behind a Cloudflare interstitial.
    """
    import scraper

    original_session_file = scraper.SESSION_FILE
    try:
        with tempfile.TemporaryDirectory() as tmp:
            scraper.SESSION_FILE = Path(tmp) / "session.json"
            # Seed an existing session that already has a pinned UA, as it
            # would after a real login.
            scraper.SESSION_FILE.write_text(json.dumps({
                "cookies": [], "origins": [], "_user_agent": "Mozilla/5.0 (pinned)"
            }))

            asyncio.run(scraper._persist_session_state(_FakeContext(), "Mozilla/5.0 (pinned)"))

            saved = json.loads(scraper.SESSION_FILE.read_text())
            assert saved.get("_user_agent") == "Mozilla/5.0 (pinned)", \
                "Session save must preserve the pinned UA so cf_clearance stays valid"
    finally:
        scraper.SESSION_FILE = original_session_file


def test_port_in_use_detection():
    """_port_in_use reports a bound port as busy and a free port as free."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    busy_port = s.getsockname()[1]
    try:
        assert app._port_in_use(busy_port) is True
    finally:
        s.close()
    assert app._port_in_use(busy_port) is False


if __name__ == "__main__":
    _run_all()
