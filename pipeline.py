"""
pipeline.py — shared post-scrape processing for manual scans (app.py) and
auto-scans (scheduler.py).

Before this module existed the scheduler had its own ad-hoc ticker counting
and never called store.record_run(), so hourly auto-scans contributed nothing
to the time-series DB — velocity sparklines, "new today" flags and the account
scorecard were built only from whenever the user happened to click Scan.
Both entry points now run the exact same combine → enrich → finalize →
persist sequence.

SCRAPE_LOCK serializes Playwright scrapes across entry points: two concurrent
headless sessions logged into the same X account is a flag-worthy signal for
X, and both would race to overwrite session.json at the end of the run.
"""
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from price_lookup import lookup_prices
from ticker_extractor import extract_tickers
from tickers_db import load_tickers

try:
    import store  # optional SQLite persistence (time series + scorecard)
except Exception:  # pragma: no cover
    store = None

# Held for the duration of any scrape (manual or auto). A plain Lock is
# deliberate — it may be acquired in the Flask request thread and released
# in the worker thread that finishes the scan.
SCRAPE_LOCK = threading.Lock()

# Thread-safe lazy-loaded ticker DB (double-checked locking)
_tickers_db = None
_tickers_db_lock = threading.Lock()


def get_tickers_db() -> set:
    global _tickers_db
    with _tickers_db_lock:
        if _tickers_db is None:
            _tickers_db = load_tickers()
        return _tickers_db


def _enrich(t: dict, price_map: dict) -> None:
    sym = t["ticker"]
    p = price_map.get(sym, {})
    # Sector/industry/company came from yfinance `.info`, a slow + unreliable
    # call that was dropped (low value for daily monitoring). Keep the keys
    # with light defaults so downstream consumers don't KeyError.
    t["sector"]       = "Unknown"
    t["industry"]     = "Unknown"
    t["company"]      = sym
    t["price"]        = p.get("price")
    t["change_pct"]   = p.get("change_pct")
    t["change_abs"]   = p.get("change_abs")
    t["currency"]     = p.get("currency",     "USD")
    t["market_state"] = p.get("market_state", "UNKNOWN")
    t["price_suspicious"] = p.get("suspicious", False)


def process_scrape_results(
    scraped: dict,
    usernames: list,
    *,
    count: int,
    since_raw: Optional[str] = None,
    client_timezone: Optional[str] = None,
    valid_tickers: Optional[set] = None,
    price_lookup: Optional[Callable] = None,
    emit: Optional[Callable] = None,
) -> dict:
    """
    Turn raw scrape output into a finished run dict:
    extract tickers per account, combine across accounts, enrich with prices,
    finalize signal/conviction fields, and persist to the store (best-effort).

    valid_tickers / price_lookup are injectable for offline tests; they default
    to the SEC ticker DB and the cached yfinance batch fetch.
    """
    if valid_tickers is None:
        valid_tickers = get_tickers_db()
    if price_lookup is None:
        price_lookup = lookup_prices

    def _emit(msg: dict) -> None:
        if emit:
            try:
                emit(msg)
            except Exception:
                pass

    run: dict = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "accounts_analyzed": usernames,
        "scan_settings": {
            "max_posts": count,
            "since_date": since_raw or None,
            "client_timezone": client_timezone or None,
        },
        "results": {},
        "combined_tickers": [],
    }

    combined: dict = {}

    for username, data in scraped.items():
        if data["error"]:
            run["results"][username] = {
                "posts_analyzed": 0,
                "posts": [],
                "tickers": [],
                "error": data["error"],
            }
            continue

        tickers = extract_tickers(data["posts"], valid_tickers)
        run["results"][username] = {
            "posts_analyzed": len(data["posts"]),
            "stopped_by": data.get("stopped_by"),
            "last_post_date": data.get("last_post_date"),
            "follower_count": data.get("follower_count"),
            "posts": data["posts"],
            "tickers": tickers,
            "error": None,
        }

        for t in tickers:
            entry = combined.setdefault(
                t["ticker"],
                {"ticker": t["ticker"], "total_mentions": 0,
                 "cashtag_mentions": 0, "signal_score": 0.0,
                 "net_sentiment": 0.0, "sources": {}},
            )
            entry["total_mentions"] += t["mentions"]
            entry["cashtag_mentions"] += t.get("cashtag_mentions", 0)
            entry["signal_score"] += t.get("signal_score", 0.0)
            entry["net_sentiment"] += t.get("net_sentiment", 0.0)
            entry["sources"][username] = t["occurrences"]

    all_ticker_symbols = list(combined.keys())

    if all_ticker_symbols:
        _emit({"type": "progress",
               "message": f"Fetching prices for {len(all_ticker_symbols)} ticker(s)..."})
        price_map = price_lookup(all_ticker_symbols)
        run["price_fetch_time"] = datetime.now(timezone.utc).isoformat()
    else:
        price_map = {}

    for data in run["results"].values():
        if not data["error"]:
            for t in data["tickers"]:
                _enrich(t, price_map)

    # Finalize derived signal fields and rank by CONVICTION, not raw counts:
    # distinct accounts first (kills single-account cashtag spam like $TSLA),
    # then aggregate signal_score, then total mentions as a tiebreaker.
    for entry in combined.values():
        entry["accounts"] = len(entry["sources"])
        # Normalize signal_score to avg-per-mention so it's comparable across
        # tickers with different mention counts and numbers of accounts.
        # Without this, summing per-account signal scores creates an arbitrary
        # number that grows with mention volume, not signal quality.
        entry["signal_score"] = round(
            entry["signal_score"] / max(entry["total_mentions"], 1), 3
        )
        entry["net_sentiment"] = round(entry["net_sentiment"], 2)
        entry["sentiment_label"] = (
            "bullish" if entry["net_sentiment"] > 0.15
            else "bearish" if entry["net_sentiment"] < -0.15
            else "mixed/neutral"
        )
        # Low-confidence: one account, no cashtag, weak signal — surface but de-rank.
        entry["low_confidence"] = (
            entry["accounts"] == 1
            and entry["cashtag_mentions"] == 0
        )
        # Conviction score: fraction of occurrences that are high-quality signals
        # (cashtag confidence + not a trailing tag). Used to rank TOP CONVICTION.
        total_occ = sum(len(occs) for occs in entry["sources"].values())
        high_conv = sum(
            1 for occs in entry["sources"].values()
            for occ in occs
            if occ.get("confidence") == "cashtag" and not occ.get("is_trailing_tag")
        )
        entry["conviction_score"] = round(high_conv / max(total_occ, 1), 3)

    combined_list = sorted(
        combined.values(),
        key=lambda x: (-x["accounts"], -x["signal_score"], -x["total_mentions"]),
    )
    for entry in combined_list:
        _enrich(entry, price_map)

    run["combined_tickers"] = combined_list

    if store is not None:
        try:
            store.record_run(run)
        except Exception as exc:  # persistence must never break a scan
            print(f"[!] store.record_run failed (non-fatal): {exc}")
            run["digest_warning"] = "Database persistence failed; digest signals may be incomplete."

    return run
