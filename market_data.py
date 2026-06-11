"""
market_data.py — single source of truth for price enrichment.

Why this replaced the guts of the old price_lookup:
  The old code called yfinance `Ticker(t).info` per ticker with 15 concurrent
  workers. `.info` is yfinance's flakiest, most rate-limited endpoint, so under
  that concurrency it frequently returned partial/empty data and bad prints
  (like MU $925) could slip through.

Approach here:
  * All prices come from ONE batched `yf.download()` call per scan.
  * Results are cached for PRICE_TTL; failures only briefly (FAIL_TTL) so
    they self-heal on the next run instead of poisoning the cache.
  * Basic sanity flags (non-positive price, absurd % move) so bad prints are
    visible downstream instead of silently trusted.

(Sector/industry profile lookups used to live here too. They depended on
`.info`, were dropped from the scan pipeline as low-value, and the dead code
has been removed.)
"""
import json
import os
import tempfile
import threading
import time
import warnings
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
PRICE_CACHE = DATA_DIR / "price_cache.json"

PRICE_TTL = 5 * 60            # prices: near real-time
FAIL_TTL = 10 * 60            # failed lookups: retry in 10 min, don't poison
SANITY_PCT = 60.0            # |daily %| above this is flagged suspicious

_lock = threading.Lock()


def _atomic_write(path: Path, data) -> None:
    path.parent.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _load(path: Path) -> dict:
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except json.JSONDecodeError:
            print(f"[!] Corrupted cache at {path} — deleting and rebuilding")
            path.unlink(missing_ok=True)
        except OSError as e:
            print(f"[!] Could not read cache at {path}: {e}")
    return {}


def _yf_session():
    """Return a curl_cffi session impersonating Chrome — bypasses Yahoo Finance
    rate limiting. SSL verification is kept enabled to prevent MITM attacks."""
    try:
        from curl_cffi import requests as cffi_requests
        return cffi_requests.Session(impersonate="chrome110", verify=True)
    except ImportError:
        import requests as _req
        s = _req.Session()
        # If behind a corporate proxy with a custom CA, set the REQUESTS_CA_BUNDLE
        # environment variable to the path of the CA bundle instead of disabling
        # verification entirely.
        s.verify = os.environ.get("REQUESTS_CA_BUNDLE", True)
        return s


def _fetch_prices_batch(tickers: list) -> dict:
    """
    Fetch prices for all tickers in a single yf.download() call.
    One HTTP request avoids per-ticker rate limiting from fast_info.
    Returns {ticker: price_dict} — None for any ticker that failed.
    """
    import yfinance as yf
    now = time.time()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        raw = yf.download(
            tickers,
            period="5d",
            interval="1d",
            progress=False,
            auto_adjust=True,
            session=_yf_session(),
        )

    results = {}
    if raw.empty:
        return {t: None for t in tickers}

    # yf.download always returns a MultiIndex-columned DataFrame with Ticker level
    close_df = raw["Close"]  # DataFrame: rows=dates, cols=tickers

    for t in tickers:
        try:
            if t not in close_df.columns:
                results[t] = None
                continue
            series = close_df[t].dropna()
            if len(series) < 2:
                results[t] = None
                continue
            price = float(series.iloc[-1])
            prev  = float(series.iloc[-2])
            change_abs = price - prev
            change_pct = (change_abs / prev) * 100
            ok = price > 0
            results[t] = {
                "price":       round(price, 2),
                "prev_close":  round(prev, 2),
                "change_abs":  round(change_abs, 2),
                "change_pct":  round(change_pct, 2),
                "currency":    "USD",
                "market_state": "REGULAR",
                "ok":          ok,
                "suspicious":  bool(ok and abs(change_pct) > SANITY_PCT),
                "_ts":         now,
            }
        except Exception:
            results[t] = None
    return results


def _fresh(rec: dict, good_ttl: float) -> bool:
    if not rec:
        return False
    ttl = good_ttl if rec.get("ok") else FAIL_TTL
    return (time.time() - rec.get("_ts", 0)) < ttl


def get_market_data(tickers: list) -> dict:
    """
    Return {ticker: {price, prev_close, change_abs, change_pct, currency,
    market_state, suspicious}} for each ticker.
    """
    if not tickers:
        return {}
    DATA_DIR.mkdir(exist_ok=True)
    tickers = list(dict.fromkeys(tickers))  # de-dupe, preserve order

    with _lock:
        price_cache = _load(PRICE_CACHE)

    need_price = [t for t in tickers if not _fresh(price_cache.get(t), PRICE_TTL)]
    new_price = {}

    # Prices: single batch download (avoids per-ticker rate limiting)
    if need_price:
        print(f"[\u2192] Prices: batch-fetching {len(need_price)} tickers via yf.download...")
        try:
            batch = _fetch_prices_batch(need_price)
            new_price.update(batch)
        except Exception as exc:
            print(f"[!] Price batch fetch failed: {exc}")
            new_price.update({t: None for t in need_price})

    with _lock:
        price_cache = _load(PRICE_CACHE)
        for t, rec in new_price.items():
            if rec is not None:
                price_cache[t] = rec
        _atomic_write(PRICE_CACHE, price_cache)

    out = {}
    for t in tickers:
        p = price_cache.get(t, {})
        out[t] = {
            "price": p.get("price"),
            "prev_close": p.get("prev_close"),
            "change_abs": p.get("change_abs"),
            "change_pct": p.get("change_pct"),
            "currency": p.get("currency", "USD"),
            "market_state": p.get("market_state", "UNKNOWN"),
            "suspicious": p.get("suspicious", False),
        }
    return out
