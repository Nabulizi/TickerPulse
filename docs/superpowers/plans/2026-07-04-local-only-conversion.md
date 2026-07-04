# Local-Only Conversion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert TickerPulse from a Render-deployed app to a reliable, always-on, local-only macOS tool with faster scans.

**Architecture:** Delete all cloud-specific files and code paths; add a port-conflict startup check so the existing launchd agent self-heals; replace fixed sleeps in the scraper's timeline loop with condition-based waits capped at the old sleep durations (worst case identical to today).

**Tech Stack:** Python 3.9, Flask, Playwright (async), pytest, macOS launchd.

## Global Constraints

- All work happens in `/Users/nabulizi/Documents/TickerPulse` on branch `main`.
- Run tests with: `source venv/bin/activate && python3 -m pytest -q test_*.py` — full suite must pass at the end of every task.
- `slow_mo=60` in `scrape_accounts` must NOT be changed (anti-bot pacing).
- Commit messages end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- Do not push; commits stay local until the user reviews.

---

### Task 1: Remove cloud files and the XTS_SESSION_B64 bootstrap

**Files:**
- Delete: `Dockerfile`, `.dockerignore`, `render.yaml`, `refresh_session.py`
- Modify: `scraper.py` (lines ~36–91: `_bootstrap_session_from_env` + module-load call; lines ~764–780: `_refresh_session`)
- Modify: `test_safety_regressions.py` (delete `test_refresh_session_raises_without_session_b64`, lines ~140–153; update one comment ~line 210)

**Interfaces:**
- Produces: `_refresh_session(progress=None)` in `scraper.py` now unconditionally raises `SessionExpired` (same signature; still `async`). Its only caller is `scrape_accounts` (scraper.py ~line 1256), which already catches nothing — `SessionExpired` propagates to `app.py`'s handler, unchanged.

- [ ] **Step 1: Delete the four cloud files**

```bash
cd /Users/nabulizi/Documents/TickerPulse
git rm Dockerfile .dockerignore render.yaml refresh_session.py
```

- [ ] **Step 2: Remove the bootstrap function from scraper.py**

Delete the entire block from `def _bootstrap_session_from_env() -> bool:` (line ~36) through the module-load call and its comment (lines ~89–91):

```python
# Bootstrap on module load so the session is ready before any request is handled.
_bootstrap_session_from_env()
```

Both the function and the two module-level lines go. Keep `_secure_session_file()` (line ~26) — it is used elsewhere.

- [ ] **Step 3: Simplify _refresh_session**

Replace the whole function (lines ~764–780) with:

```python
async def _refresh_session(progress=None) -> None:
    """No cached session exists. Local-only app: the user must log in via
    /connect-x, paste cookies, or import a session.json via the web UI."""
    raise SessionExpired(
        "X session expired or missing. "
        "Paste fresh cookies via the 'Paste Cookies' button or import a session.json file."
    )
```

- [ ] **Step 4: Remove the B64 test and stale comment**

In `test_safety_regressions.py`: delete the entire `test_refresh_session_raises_without_session_b64` function. In `test_scan_session_save_preserves_user_agent_pin`, change the comment `# would after a real login or an XTS_SESSION_B64 bootstrap.` to `# would after a real login.`

Add a replacement test (same file, same spot) so the no-session path stays covered:

```python
def test_refresh_session_always_raises():
    """Local-only: _refresh_session has no fallback and must raise."""
    import scraper

    with pytest.raises(scraper.SessionExpired):
        asyncio.run(scraper._refresh_session())
```

- [ ] **Step 5: Verify nothing references the removed code**

```bash
grep -rn "XTS_SESSION_B64\|_bootstrap_session_from_env\|refresh_session.py" --include="*.py" --include="*.yml" --include="*.md" . | grep -v docs/superpowers | grep -v venv
```

Expected: only hits inside `README.md` (handled in Task 4) — if any `.py` or CI hit appears, fix it before proceeding.

- [ ] **Step 6: Run the full suite**

Run: `source venv/bin/activate && python3 -m pytest -q test_*.py`
Expected: 36 passed (36 before − 1 deleted + 1 replacement), 0 failures.

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "Remove Render/cloud code: Dockerfile, render.yaml, refresh_session.py, B64 bootstrap

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Remove temporary debug endpoints

**Files:**
- Modify: `app.py` (delete `/debug/threads` route, lines ~258–272, and `/debug/x-probe` route, lines ~274–385)

**Interfaces:**
- Consumes: nothing. Produces: nothing — these routes have no callers in templates or tests.

- [ ] **Step 1: Delete both routes**

Delete everything from `@app.route("/debug/threads")` through the end of `debug_x_probe` (the line `return jsonify(out)` immediately before `@app.route("/session-status")`). The file should go straight from `healthz` to `get_session_status`.

- [ ] **Step 2: Verify no references remain**

```bash
grep -rn "debug/threads\|debug/x-probe\|debug_threads\|debug_x_probe" --include="*.py" --include="*.html" . | grep -v venv | grep -v docs/superpowers
```

Expected: no output. Also check `import sys` in app.py — it was added for `debug_threads`; if `sys` is now unused (`grep -n "sys\." app.py`), remove the `import sys` line.

- [ ] **Step 3: Run suite and import check**

Run: `source venv/bin/activate && python3 -m pytest -q test_*.py && python3 -c "import app; print('OK')"`
Expected: all pass, `OK`.

- [ ] **Step 4: Commit**

```bash
git add app.py && git commit -m "Remove temporary /debug endpoints used for the Render investigation

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Port-conflict startup check

**Files:**
- Modify: `app.py` (the `if __name__ == "__main__":` block, ~line 845)
- Test: `test_safety_regressions.py` (append)

**Interfaces:**
- Produces: `_port_in_use(port: int) -> bool` in `app.py` (module level, above the `__main__` block).

- [ ] **Step 1: Write the failing test**

Append to `test_safety_regressions.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && python3 -m pytest -q test_safety_regressions.py::test_port_in_use_detection`
Expected: FAIL with `AttributeError: module 'app' has no attribute '_port_in_use'`

- [ ] **Step 3: Implement**

In `app.py`, add above the `__main__` block (needs `import socket` at the top of the file):

```python
def _port_in_use(port: int) -> bool:
    """True if something is already listening on 127.0.0.1:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0
```

Replace the `__main__` block with:

```python
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    if _port_in_use(port):
        print(f"[✗] Port {port} is already in use — another instance is likely running.")
        print(f"    Identify it with:  lsof -iTCP:{port} -sTCP:LISTEN")
        sys.exit(1)
    start_background_services()
    print(f"[✓] Ready — open http://localhost:{port}")
    app.run(debug=False, port=port)
```

(`sys` must be imported at the top of app.py; re-add `import sys` if Task 2 removed it.)

- [ ] **Step 4: Run test to verify it passes, then full suite**

Run: `source venv/bin/activate && python3 -m pytest -q test_*.py`
Expected: all pass.

- [ ] **Step 5: Behavior check — second instance exits cleanly**

```bash
source venv/bin/activate && (python3 app.py >/tmp/first.log 2>&1 &) && sleep 3 && python3 app.py; echo "exit=$?"; pkill -f "python3 app.py"
```

Expected: second instance prints the two `[✗]`/`lsof` lines and `exit=1`.

- [ ] **Step 6: Commit**

```bash
git add app.py test_safety_regressions.py && git commit -m "Fail fast with a clear message when port 8080 is already in use

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Condition-based waits in the scraper

**Files:**
- Modify: `scraper.py` (scroll loop ~line 1218, settle wait ~line 1088, inter-account sleep ~line 1352; two new helpers near `_wait_for_first_locator`)
- Test: `test_scraper_parse.py` (append)

**Interfaces:**
- Produces (both in `scraper.py`, both async, both poll every 0.15 s):
  - `_wait_for_article_change(page, prev_count: int, cap_s: float = 1.3) -> int` — returns current article count as soon as it differs from `prev_count`, else after `cap_s`.
  - `_wait_for_render_settle(page, cap_s: float = 1.5) -> None` — returns when two consecutive polls see the same nonzero article count, else after `cap_s`.
- Consumes: `page.locator('article[data-testid="tweet"]').count()` (Playwright API; the tests stub it).

- [ ] **Step 1: Write the failing tests**

Append to `test_scraper_parse.py`:

```python
import asyncio


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `source venv/bin/activate && python3 -m pytest -q test_scraper_parse.py -k "article_change or render_settle"`
Expected: 3 FAILED with `AttributeError: ... has no attribute '_wait_for_article_change'`

- [ ] **Step 3: Implement the helpers**

Add to `scraper.py`, directly after `_wait_for_first_locator`:

```python
_ARTICLE_SELECTOR = 'article[data-testid="tweet"]'


async def _wait_for_article_change(page, prev_count: int, cap_s: float = 1.3) -> int:
    """
    Poll the rendered article count until it differs from prev_count, or cap_s
    elapses. X virtualizes the timeline (offscreen articles are removed from
    the DOM), so any CHANGE — not just growth — means new content rendered.
    Worst case equals the old fixed sleep; fast machines advance in ~150 ms.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + cap_s
    count = prev_count
    while loop.time() < deadline:
        try:
            count = await page.locator(_ARTICLE_SELECTOR).count()
        except Exception:
            break  # navigation/context churn — let the caller's logic decide
        if count != prev_count:
            break
        await asyncio.sleep(0.15)
    return count


async def _wait_for_render_settle(page, cap_s: float = 1.5) -> None:
    """
    Wait until the article count is stable (two consecutive equal nonzero
    polls) so the initial viewport has finished painting, capped at cap_s.
    Replaces a fixed post-load sleep.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + cap_s
    prev = -1
    while loop.time() < deadline:
        try:
            current = await page.locator(_ARTICLE_SELECTOR).count()
        except Exception:
            return
        if current > 0 and current == prev:
            return
        prev = current
        await asyncio.sleep(0.15)
```

- [ ] **Step 4: Wire them into _fetch_posts and the account loop**

At scraper.py ~line 1086, replace:

```python
    # Let the full initial viewport render — wait_for_selector fires on the FIRST
    # article, but React may still be painting the rest of the visible posts.
    await asyncio.sleep(1.5)
```

with:

```python
    # Let the full initial viewport render — wait_for_selector fires on the FIRST
    # article, but React may still be painting the rest of the visible posts.
    await _wait_for_render_settle(page, cap_s=1.5)
```

At ~line 1218, replace:

```python
        await page.evaluate("window.scrollBy(0, 900)")
        await asyncio.sleep(1.3)
        scrolls += 1
```

with:

```python
        prev_article_count = await page.locator(_ARTICLE_SELECTOR).count()
        await page.evaluate("window.scrollBy(0, 900)")
        await _wait_for_article_change(page, prev_article_count, cap_s=1.3)
        scrolls += 1
```

At ~line 1352 (end of the per-account loop), replace `await asyncio.sleep(1.5)` with `await asyncio.sleep(0.8)  # polite pacing between accounts`.

- [ ] **Step 5: Run the full suite**

Run: `source venv/bin/activate && python3 -m pytest -q test_*.py`
Expected: all pass (40 total: 36 after Task 1 + 1 from Task 3 + 3 from this task).

- [ ] **Step 6: End-to-end speed/parity verification**

```bash
source venv/bin/activate && time python3 -c "
import asyncio
from scraper import scrape_accounts
r = asyncio.run(scrape_accounts(['elonmusk'], count=5))
v = r['elonmusk']
print('posts:', len(v['posts']), 'error:', v.get('error'))
"
```

Expected: `posts: 5 error: None`, wall time noticeably below a pre-change baseline run (record both numbers in the commit message). If post count drops below the requested count when the account clearly has ≥5 recent posts, the wait is advancing too eagerly — investigate before committing.

- [ ] **Step 7: Commit**

```bash
git add scraper.py test_scraper_parse.py && git commit -m "Replace fixed scraper sleeps with condition-based waits (capped at old durations)

<include before/after wall-time numbers here>

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: README rewrite + launchd install & verification

**Files:**
- Modify: `README.md` (delete "Deploying to Render" section, lines ~102–134; update "Running the App" and "Auto-start on login" sections)

**Interfaces:** none (docs + operational verification).

- [ ] **Step 1: Delete the Render section and scrub cloud references**

Remove the entire `## Deploying to Render` section. Then:

```bash
grep -n -i "render\|docker\|gunicorn\|XTS_SESSION_B64\|refresh_session" README.md
```

Expected after edits: no output (fix any stragglers — e.g., project-structure listings naming deleted files).

- [ ] **Step 2: Update the auto-start section**

Replace the `### Auto-start on login (macOS)` section body with:

```markdown
### Auto-start on login (macOS) — recommended

Run once:

```bash
./install_launchd.sh
```

This installs a launchd agent that starts the app in the background at login
and restarts it if it crashes. The dashboard is then always available at
**http://localhost:8080** — bookmark it. Logs: `data/launchd.log` and
`data/launchd.err.log`. To uninstall: `./install_launchd.sh remove`.
```

- [ ] **Step 3: Install and verify the launchd agent**

```bash
pkill -f "python3 app.py" 2>/dev/null; ./install_launchd.sh && sleep 5 && curl -s http://localhost:8080/healthz
```

Expected: `{"ok":true}` — served by the launchd-managed instance, no terminal session owning it.

- [ ] **Step 4: Verify crash recovery**

```bash
launchctl kickstart -k gui/$(id -u)/com.x-ticker-scraper && sleep 6 && curl -s http://localhost:8080/healthz
```

Expected: `{"ok":true}` again (service killed and came back on its own).

- [ ] **Step 5: Run the full suite one final time**

Run: `source venv/bin/activate && python3 -m pytest -q test_*.py`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add README.md && git commit -m "Rewrite README for local-only use; document launchd auto-start

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
