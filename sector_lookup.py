"""
sector_lookup.py — thin adapter kept for backwards compatibility.

Sector/profile data now comes from market_data.py, which keeps a 30-day
"last known good" memory that a throttled fetch can't overwrite — so major
names no longer collapse to "Unknown" after a single rate-limit hit.
"""
from market_data import get_market_data


def lookup_sectors(tickers: list) -> dict:
    md = get_market_data(tickers)
    return {
        t: {
            "sector": d["sector"],
            "industry": d["industry"],
            "company": d["company"],
        }
        for t, d in md.items()
    }
