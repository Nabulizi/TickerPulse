#!/usr/bin/env python3
"""
scan.py — run a scan from the terminal, no browser dashboard needed.

Usage:
    python3 scan.py mywatchlist                # scan a saved watchlist by name
    python3 scan.py elonmusk other_trader      # scan accounts directly
    python3 scan.py mywatchlist extra_user     # mix watchlists and accounts
    python3 scan.py                            # scan ALL saved watchlist accounts
    python3 scan.py traders --count 30 --since 2026-06-10
    python3 scan.py --list-watchlists

Results go through the same pipeline as the dashboard: tickers are extracted,
enriched with prices, ranked by conviction, and persisted to the time-series
DB (so CLI scans feed velocity/scorecard like any other scan).
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

WATCHLISTS_FILE = Path(__file__).parent / "data" / "watchlists.json"


def _load_watchlists() -> dict:
    try:
        with open(WATCHLISTS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _resolve_accounts(args: list, watchlists: dict) -> list:
    """
    Expand positional args into a deduplicated, validated account list.
    Each arg is a watchlist name if one matches, otherwise an X username.
    """
    from scraper import validate_username

    accounts: list = []
    for arg in args:
        if arg in watchlists:
            candidates = watchlists[arg]
        else:
            candidates = [arg]
        for c in candidates:
            username = validate_username(str(c))
            if username not in accounts:
                accounts.append(username)
    return accounts


def _fmt(value, width: int) -> str:
    if value is None:
        return "—".rjust(width)
    return str(value).rjust(width)


def _format_table(combined: list) -> str:
    """Render the ranked combined-ticker list as a fixed-width terminal table."""
    if not combined:
        return "No tickers found."
    header = (f"{'TICKER':<8}{'ACCTS':>6}{'MENTIONS':>10}{'SIGNAL':>8}"
              f"{'PRICE':>10}{'CHG%':>8}  SENTIMENT")
    lines = [header, "-" * len(header)]
    for t in combined:
        flag = " (low conf)" if t.get("low_confidence") else ""
        lines.append(
            f"{t['ticker']:<8}"
            f"{_fmt(t.get('accounts'), 6)}"
            f"{_fmt(t.get('total_mentions'), 10)}"
            f"{_fmt(t.get('signal_score'), 8)}"
            f"{_fmt(t.get('price'), 10)}"
            f"{_fmt(t.get('change_pct'), 8)}"
            f"  {t.get('sentiment_label', '')}{flag}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan X accounts for stock ticker mentions (terminal version)."
    )
    parser.add_argument("targets", nargs="*",
                        help="watchlist names and/or X usernames (default: all watchlists)")
    parser.add_argument("--count", type=int, default=20,
                        help="max posts per account (default 20, max 200)")
    parser.add_argument("--since", default=None,
                        help="only analyze posts on/after this date (YYYY-MM-DD)")
    parser.add_argument("--list-watchlists", action="store_true",
                        help="print saved watchlists and exit")
    args = parser.parse_args()

    watchlists = _load_watchlists()

    if args.list_watchlists:
        if not watchlists:
            print("No saved watchlists.")
        for name, accounts in watchlists.items():
            print(f"{name}: {', '.join(accounts)}")
        return 0

    if args.targets:
        try:
            accounts = _resolve_accounts(args.targets, watchlists)
        except ValueError as exc:
            print(f"[✗] Invalid username: {exc}")
            return 1
    else:
        accounts = sorted({a for accs in watchlists.values() for a in accs})

    if not accounts:
        print("[✗] No accounts to scan. Pass usernames or save a watchlist first.")
        return 1

    since_date = None
    if args.since:
        try:
            since_date = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        except ValueError:
            print("[✗] Invalid --since date. Use YYYY-MM-DD.")
            return 1

    count = max(1, min(args.count, 200))

    import pipeline
    from scraper import InteractiveLoginRequired, SessionExpired, scrape_accounts

    if not pipeline.SCRAPE_LOCK.acquire(blocking=False):
        print("[✗] Another scan is already running.")
        return 1
    try:
        print(f"[→] Scanning {len(accounts)} account(s), up to {count} posts each...")
        try:
            scraped = asyncio.run(
                scrape_accounts(accounts, count=count, since_date=since_date, progress=None)
            )
        except (InteractiveLoginRequired, SessionExpired) as exc:
            print(f"[✗] X session problem: {exc}")
            return 1

        run = pipeline.process_scrape_results(
            scraped, accounts, count=count, since_raw=args.since
        )
    finally:
        pipeline.SCRAPE_LOCK.release()

    for username, data in run["results"].items():
        if data["error"]:
            print(f"[✗] @{username}: {data['error']}")
        else:
            print(f"[✓] @{username}: {data['posts_analyzed']} posts")

    print()
    print(_format_table(run["combined_tickers"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
