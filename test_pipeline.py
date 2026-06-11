"""
Offline tests for the shared scan pipeline (pipeline.py) and the
scan lock that prevents auto-scans and manual scans from overlapping.

Run:
    python3 test_pipeline.py
"""
import tempfile
from pathlib import Path

import pipeline
import scheduler
import store

_POSTS_A = [
    {
        "text": "Loving $NVDA here, adding more on this dip",
        "posted_at": "2026-06-11T13:00:00.000Z",
        "url": "https://x.com/trader_a/status/111",
        "is_repost": False,
        "likes": 5, "reposts": 1, "replies": 0, "views": 100,
    },
]
_POSTS_B = [
    {
        "text": "$NVDA breakout looks strong, $TSLA weak",
        "posted_at": "2026-06-11T14:00:00.000Z",
        "url": "https://x.com/trader_b/status/222",
        "is_repost": False,
        "likes": 2, "reposts": 0, "replies": 1, "views": 50,
    },
]


def _fake_scraped() -> dict:
    return {
        "trader_a": {"posts": list(_POSTS_A), "stopped_by": "count",
                     "last_post_date": None, "follower_count": 100, "error": None},
        "trader_b": {"posts": list(_POSTS_B), "stopped_by": "count",
                     "last_post_date": None, "follower_count": 200, "error": None},
        "broken": {"posts": [], "stopped_by": None, "error": "Account not found"},
    }


def _fake_price_lookup(tickers: list) -> dict:
    return {t: {"price": 100.0, "prev_close": 99.0, "change_abs": 1.0,
                "change_pct": 1.01, "currency": "USD",
                "market_state": "REGULAR", "suspicious": False}
            for t in tickers}


_VALID_TICKERS = {"NVDA", "TSLA"}


def test_process_scrape_results_combines_and_enriches():
    original_db_path = store.DB_PATH
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store.DB_PATH = Path(tmp) / "scraper.db"
            run = pipeline.process_scrape_results(
                _fake_scraped(),
                ["trader_a", "trader_b", "broken"],
                count=10,
                valid_tickers=_VALID_TICKERS,
                price_lookup=_fake_price_lookup,
            )

            combined = run["combined_tickers"]
            assert combined, "expected combined tickers"
            top = combined[0]
            assert top["ticker"] == "NVDA"          # 2 accounts beats 1
            assert top["accounts"] == 2
            assert top["price"] == 100.0
            assert top["sentiment_label"] == "bullish"
            assert top["low_confidence"] is False

            tsla = next(t for t in combined if t["ticker"] == "TSLA")
            assert tsla["accounts"] == 1

            # per-account results preserved, error account passed through
            assert run["results"]["broken"]["error"] == "Account not found"
            assert run["results"]["trader_a"]["posts_analyzed"] == 1
    finally:
        store.DB_PATH = original_db_path


def test_process_scrape_results_persists_mentions_to_store():
    original_db_path = store.DB_PATH
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store.DB_PATH = Path(tmp) / "scraper.db"
            pipeline.process_scrape_results(
                _fake_scraped(),
                ["trader_a", "trader_b"],
                count=10,
                valid_tickers=_VALID_TICKERS,
                price_lookup=_fake_price_lookup,
            )
            with store._conn() as c:
                rows = c.execute(
                    "SELECT account, ticker FROM mentions ORDER BY account, ticker"
                ).fetchall()
            assert ("trader_a", "NVDA") in rows
            assert ("trader_b", "NVDA") in rows
            assert ("trader_b", "TSLA") in rows
    finally:
        store.DB_PATH = original_db_path


def test_scheduler_run_scan_persists_to_store():
    original_db_path = store.DB_PATH
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store.DB_PATH = Path(tmp) / "scraper.db"

            def fake_scrape(accounts, count, since_date):
                return _fake_scraped()

            qualified = scheduler._run_scan(
                scrape_fn=fake_scrape,
                accounts=["trader_a", "trader_b"],
                valid_tickers=_VALID_TICKERS,
                price_lookup=_fake_price_lookup,
            )

            assert qualified is not None
            assert qualified[0]["ticker"] == "NVDA"
            assert qualified[0]["accounts"] == 2

            with store._conn() as c:
                n = c.execute("SELECT COUNT(*) FROM mentions").fetchone()[0]
            assert n >= 3, "auto-scan must persist mentions to the time-series DB"
    finally:
        store.DB_PATH = original_db_path


def test_scheduler_run_scan_skips_when_scan_lock_held():
    calls = []

    def fake_scrape(accounts, count, since_date):
        calls.append(accounts)
        return _fake_scraped()

    assert pipeline.SCRAPE_LOCK.acquire(blocking=False)
    try:
        result = scheduler._run_scan(
            scrape_fn=fake_scrape,
            accounts=["trader_a"],
            valid_tickers=_VALID_TICKERS,
            price_lookup=_fake_price_lookup,
        )
    finally:
        pipeline.SCRAPE_LOCK.release()

    assert result is None, "auto-scan must skip while another scan holds the lock"
    assert calls == [], "scrape must not run while the lock is held"


def test_scrape_route_rejected_while_scan_lock_held():
    import app

    assert pipeline.SCRAPE_LOCK.acquire(blocking=False)
    try:
        client = app.app.test_client()
        resp = client.post("/scrape", json={"usernames": "trader_a", "count": 5})
        assert resp.status_code == 429
    finally:
        pipeline.SCRAPE_LOCK.release()


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"passed {test.__name__}")
    print(f"{len(tests)}/{len(tests)} passed")


if __name__ == "__main__":
    _run_all()
