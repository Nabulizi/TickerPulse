"""
Offline unit tests for the browser-free (lxml) timeline parsing added in the
Scrapling-inspired refactor. These exercise `_parse_article` and friends against
synthetic HTML that mirrors X's article DOM — no browser, no network required.

Run:  source venv/bin/activate && python3 test_scraper_parse.py
"""
import asyncio
import scraper


def _article(*, text_html, status="/Mr_Derivatives/status/1790000000000000001",
             datetime_attr="2026-05-31T14:03:00.000Z", social=None,
             aria="12 replies, 34 reposts, 567 likes, 8901 views",
             show_more=False, with_text_node=True):
    """Build a synthetic <article> roughly shaped like X's timeline DOM."""
    social_html = (
        f'<div data-testid="socialContext"><span>{social}</span></div>'
        if social else ""
    )
    text_node = (
        f'<div data-testid="tweetText" dir="auto">{text_html}</div>'
        if with_text_node else ""
    )
    show_more_html = (
        '<button data-testid="tweet-text-show-more-link">Show more</button>'
        if show_more else ""
    )
    aria_html = f'<div role="group" aria-label="{aria}"></div>' if aria else ""
    return f"""
    <article data-testid="tweet">
      {social_html}
      <a href="{status}"><time datetime="{datetime_attr}">May 31</time></a>
      {text_node}
      {show_more_html}
      {aria_html}
    </article>
    """


def test_engagement_label_parsing():
    out = scraper._parse_engagement_label(
        "12 replies, 34 reposts, 567 likes, 8901 views")
    assert out == {"replies": 12, "reposts": 34, "likes": 567, "views": 8901}


def test_engagement_label_with_suffixes():
    out = scraper._parse_engagement_label("1.2K reposts, 3.4M likes, 5 views")
    assert out["reposts"] == 1200
    assert out["likes"] == 3_400_000
    assert out["views"] == 5
    assert out["replies"] is None


def test_engagement_label_empty():
    assert scraper._parse_engagement_label(None) == {
        "replies": None, "reposts": None, "likes": None, "views": None}


def test_parse_article_basic():
    html = _article(text_html='Watching $NVDA closely today')
    meta = scraper._parse_article(html)
    assert meta["has_text_node"] is True
    assert meta["text"] == "Watching $NVDA closely today"
    assert meta["url"] == "https://x.com/Mr_Derivatives/status/1790000000000000001"
    assert meta["posted_at"] == "2026-05-31T14:03:00.000Z"
    assert meta["is_repost"] is False
    assert meta["has_show_more"] is False
    assert meta["engagement"]["likes"] == 567


def test_parse_article_cashtag_link_and_emoji():
    # X renders cashtags as <a> and emoji as <img alt="…">; both must survive.
    text_html = 'Loading up on <a href="/search?q=%24TSLA">$TSLA</a> <img alt="🚀" src="x.png"/>'
    meta = scraper._parse_article(_article(text_html=text_html))
    assert "$TSLA" in meta["text"]
    assert "🚀" in meta["text"]


def test_parse_article_newline_from_br():
    meta = scraper._parse_article(_article(text_html='line one<br/>line two'))
    assert meta["text"] == "line one\nline two"


def test_parse_article_repost_flagged():
    meta = scraper._parse_article(_article(
        text_html='Great thread', social="Mr_Derivatives reposted"))
    assert meta["is_repost"] is True


def test_parse_article_pinned_is_not_repost():
    # Pinned posts carry a socialContext too, but must NOT be treated as reposts.
    meta = scraper._parse_article(_article(
        text_html='Pinned thesis', social="Pinned"))
    assert meta["is_repost"] is False


def test_parse_article_show_more_detected():
    meta = scraper._parse_article(_article(
        text_html='A very long thread that is truncated', show_more=True))
    assert meta["has_show_more"] is True


def test_parse_article_no_text_node():
    # Ads / "who to follow" cards have no tweetText — must be skippable.
    meta = scraper._parse_article(_article(
        text_html='', with_text_node=False))
    assert meta["has_text_node"] is False


def test_parse_article_url_normalized_absolute():
    meta = scraper._parse_article(_article(
        text_html='hi', status="https://x.com/foo/status/123"))
    assert meta["url"] == "https://x.com/foo/status/123"


def test_parse_article_malformed_returns_empty():
    # Garbage in → {} so the caller falls back to the async path (never raises).
    assert scraper._parse_article("\x00not html<<<") == {} or \
        scraper._parse_article("\x00not html<<<").get("has_text_node") in (None, False)


class _FakeLocator:
    def __init__(self, counts):
        self._counts = counts  # successive values to return
        self.calls = 0

    async def count(self):
        val = self._counts[min(self.calls, len(self._counts) - 1)]
        self.calls += 1
        return val


class _FakePage:
    def __init__(self, counts):
        self._locator = _FakeLocator(counts)

    def locator(self, selector):
        assert selector == 'article[data-testid="tweet"]'
        return self._locator


def test_wait_for_article_change_returns_on_growth():
    page = _FakePage([5, 5, 9])
    result = asyncio.run(scraper._wait_for_article_change(page, prev_count=5, cap_s=2.0))
    assert result == 9


def test_wait_for_article_change_caps_out_when_static():
    page = _FakePage([5])
    result = asyncio.run(scraper._wait_for_article_change(page, prev_count=5, cap_s=0.4))
    assert result == 5  # unchanged after cap — caller's end-of-timeline logic applies


def test_wait_for_render_settle_returns_on_stability():
    page = _FakePage([1, 3, 3])
    asyncio.run(scraper._wait_for_render_settle(page, cap_s=2.0))
    assert page._locator.calls >= 3  # needed two consecutive equal nonzero reads


def test_wait_for_article_change_survives_locator_errors():
    class _RaisingLocator:
        async def count(self):
            raise RuntimeError("navigation destroyed context")

    class _RaisingPage:
        def locator(self, selector):
            return _RaisingLocator()

    result = asyncio.run(scraper._wait_for_article_change(_RaisingPage(), prev_count=4, cap_s=5.0))
    assert result == 4  # returns prev_count immediately, no hang


def test_wait_for_article_change_respects_cap():
    import time as _time

    page = _FakePage([5])
    start = _time.monotonic()
    asyncio.run(scraper._wait_for_article_change(page, prev_count=5, cap_s=0.3))
    assert _time.monotonic() - start < 0.6  # returns at the cap, no overshoot


def test_wait_for_render_settle_caps_out_on_empty_timeline():
    import time as _time

    page = _FakePage([0])
    start = _time.monotonic()
    asyncio.run(scraper._wait_for_render_settle(page, cap_s=0.4))
    assert _time.monotonic() - start < 1.0  # capped out, did not hang


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"  ✓ {fn.__name__}")
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
