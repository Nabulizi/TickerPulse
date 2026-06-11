"""
Offline tests for the scan.py CLI helpers (account resolution + table output).

Run:
    python3 test_cli.py
"""
import scan


def test_resolve_accounts_expands_watchlist_names():
    watchlists = {"traders": ["alpha", "beta"], "macro": ["gamma"]}
    accounts = scan._resolve_accounts(["traders", "extra_user"], watchlists)
    assert accounts == ["alpha", "beta", "extra_user"]


def test_resolve_accounts_dedupes_preserving_order():
    watchlists = {"a": ["one", "two"], "b": ["two", "three"]}
    accounts = scan._resolve_accounts(["a", "b", "one"], watchlists)
    assert accounts == ["one", "two", "three"]


def test_resolve_accounts_strips_at_and_validates():
    accounts = scan._resolve_accounts(["@some_user"], {})
    assert accounts == ["some_user"]
    try:
        scan._resolve_accounts(["bad name!"], {})
        raise AssertionError("expected ValueError for invalid username")
    except ValueError:
        pass


def test_format_table_renders_ranked_tickers():
    combined = [
        {"ticker": "NVDA", "accounts": 2, "total_mentions": 5,
         "signal_score": 0.9, "sentiment_label": "bullish",
         "price": 100.0, "change_pct": 1.5, "low_confidence": False},
        {"ticker": "TSLA", "accounts": 1, "total_mentions": 2,
         "signal_score": 0.4, "sentiment_label": "bearish",
         "price": None, "change_pct": None, "low_confidence": True},
    ]
    out = scan._format_table(combined)
    assert "NVDA" in out
    assert "TSLA" in out
    assert "bullish" in out
    assert "100.0" in out
    assert "—" in out or "-" in out  # missing price renders as a placeholder


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"passed {test.__name__}")
    print(f"{len(tests)}/{len(tests)} passed")


if __name__ == "__main__":
    _run_all()
