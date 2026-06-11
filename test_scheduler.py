"""
Offline tests for the scheduler's due-time computation.

The old loop slept the full interval in one time.sleep() call, so an
off-hours 6-hour sleep that started at 8 AM ET slept straight through the
9:30 market open. Due time must be recomputed against the CURRENT session.

Run:
    python3 test_scheduler.py
"""
from datetime import datetime

import scheduler


def _et(y, mo, d, h, mi) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=scheduler.ET)


def test_next_due_uses_market_interval_when_market_open():
    # Tuesday 2026-06-09, anchor 09:00 ET, now 10:05 ET (market open)
    anchor = _et(2026, 6, 9, 9, 0).timestamp()
    now = _et(2026, 6, 9, 10, 5)
    due = scheduler._next_due_at(anchor, now=now)
    assert due == anchor + scheduler.INTERVAL_MARKET
    assert due <= now.timestamp(), "scan should be due during market hours"


def test_next_due_uses_offhours_interval_at_night():
    # Tuesday 02:00 ET anchor, now 03:00 ET — next due 08:00 ET, not yet due
    anchor = _et(2026, 6, 9, 2, 0).timestamp()
    now = _et(2026, 6, 9, 3, 0)
    due = scheduler._next_due_at(anchor, now=now)
    assert due == anchor + scheduler.INTERVAL_OFF
    assert due > now.timestamp()


def test_market_open_is_not_slept_through():
    # Anchor 08:00 ET (off-hours). At 09:31 ET the market interval applies,
    # so the scan is already due — the old fixed sleep would have waited
    # until 14:00 ET.
    anchor = _et(2026, 6, 9, 8, 0).timestamp()
    now = _et(2026, 6, 9, 9, 31)
    due = scheduler._next_due_at(anchor, now=now)
    assert due == anchor + scheduler.INTERVAL_MARKET
    assert due <= now.timestamp()


def test_weekend_uses_offhours_interval():
    # Saturday midday — off-hours interval even at 10:00 "market time"
    anchor = _et(2026, 6, 13, 10, 0).timestamp()
    now = _et(2026, 6, 13, 11, 0)
    due = scheduler._next_due_at(anchor, now=now)
    assert due == anchor + scheduler.INTERVAL_OFF


def test_build_notification_formats_tickers():
    tickers = [
        {"ticker": "NVDA", "accounts": 3, "total_mentions": 7},
        {"ticker": "TSLA", "accounts": 2, "total_mentions": 4},
        {"ticker": "AMD", "accounts": 2, "total_mentions": 3},
        {"ticker": "PLTR", "accounts": 2, "total_mentions": 2},
        {"ticker": "SOFI", "accounts": 2, "total_mentions": 2},
    ]
    title, body = scheduler._build_notification(tickers)
    assert "5 signals" in title
    assert "$NVDA (3 accts)" in body
    assert "+1 more" in body


def _with_patched_notify_channels(env: dict, fn):
    """Run fn() with _send_telegram/_osascript recorded and env vars overridden."""
    import os

    sent = []
    orig_send = scheduler._send_telegram
    orig_osa = scheduler._osascript
    orig_env = {k: os.environ.get(k) for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
    try:
        scheduler._send_telegram = lambda text: sent.append(text)
        scheduler._osascript = lambda *a, **k: None
        for k in orig_env:
            os.environ.pop(k, None)
        os.environ.update(env)
        fn()
    finally:
        scheduler._send_telegram = orig_send
        scheduler._osascript = orig_osa
        for k, v in orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return sent


def test_notify_sends_telegram_when_configured():
    sent = _with_patched_notify_channels(
        {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42"},
        lambda: scheduler._notify([{"ticker": "NVDA", "accounts": 2, "total_mentions": 3}]),
    )
    assert len(sent) == 1
    assert "$NVDA" in sent[0]


def test_notify_skips_telegram_without_config():
    sent = _with_patched_notify_channels(
        {},
        lambda: scheduler._notify([{"ticker": "NVDA", "accounts": 2, "total_mentions": 3}]),
    )
    assert sent == []


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"passed {test.__name__}")
    print(f"{len(tests)}/{len(tests)} passed")


if __name__ == "__main__":
    _run_all()
