# TickerPulse Local-Only Conversion — Design

**Date:** 2026-07-04
**Status:** Approved
**Context:** TickerPulse was deployed on Render's free tier, whose 0.15-CPU quota cannot run Chromium against x.com (scans starved the web server; Render's health checker killed the service mid-scan). Rather than pay for hosting, the tool becomes local-only. The original motivation for hosting — "I don't want to run a terminal command to launch the scanner" — is solved locally with the existing launchd agent.

## Goals

1. Zero-terminal daily use: app always running in the background, reachable at `http://localhost:8080` via bookmark.
2. Remove all cloud/Render-specific code and files.
3. Faster local scans via condition-based waits (no behavior/result changes).
4. Reliability: survive crashes, reboots, and port conflicts without user intervention.

## Non-Goals (out of scope)

- Parallel account scanning (bot-detection risk, complexity)
- macOS notifications, menu-bar UI, any dashboard UI changes
- Any remote/cloud access path

## 1. Cloud code removal

**Delete files:** `Dockerfile`, `.dockerignore`, `render.yaml`, `refresh_session.py`.

**`scraper.py`:** remove `_bootstrap_session_from_env()` and every `XTS_SESSION_B64` reference (existed solely to seed session.json on ephemeral cloud disks).

**`app.py`:** remove the temporary diagnostic endpoints `/debug/x-probe` and `/debug/threads` (added during the Render investigation, marked temporary).

**Keep:** `/paste-cookies` + `/import-session` (local session-recovery path), `import_cookies.py`, the GitHub Actions CI workflow, all scan/store/digest/scheduler logic, `XTS_SESSION_FILE`/`XTS_OUTPUT_DIR` env overrides (harmless, used by tests).

**Tests:** `test_refresh_session_raises_without_session_b64` is deleted with the feature; `_refresh_session()` simplifies to always raising `SessionExpired` (no env fallback) — verify remaining callers agree.

**`README.md`:** rewrite setup and usage for local-only (venv setup, one-time login, `./install_launchd.sh`, bookmark). Delete the "Deploying to Render" section.

**User action (outside repo):** delete the Render service in the dashboard — its env vars contain the X password; nothing should keep running there.

## 2. Always-on background service (launchd)

Use the existing `launchd/com.x-ticker-scraper.plist` + `install_launchd.sh` (runs `venv/bin/python3 app.py` at login, `KeepAlive` restarts on crash, logs to `data/launchd.log` / `data/launchd.err.log`).

**Port-conflict fix:** on startup, before binding, check whether port 8080 is already in use. If so, print one clear line naming the conflict (and the `lsof -iTCP:8080` command to identify it) and exit nonzero. launchd's `KeepAlive.SuccessfulExit=false` then retries, which self-heals transient conflicts (e.g., old instance still shutting down during restart). This addresses the stale-process-on-8080 failure hit twice during development.

## 3. Scan speed — condition-based waits in `scraper.py`

| Site | Today | Change |
|---|---|---|
| Scroll loop in `_fetch_posts` | `scrollBy` then flat `asyncio.sleep(1.3)` per scroll | Wait until `article` count strictly increases, polling ~150 ms, capped at 1.3 s. Advance immediately when new content renders. |
| Post-load settle | flat `sleep(1.5)` after first article appears | Reduce to a short poll for article-count stability (two consecutive equal counts), capped at 1.5 s |
| Between accounts | flat `sleep(1.5)` | Keep a floor of 0.8 s (polite pacing), no condition needed |
| `slow_mo=60` | anti-detection pacing | **Unchanged** |

Result identity requirement: for the same account and count, before/after scans must return the same posts (X timeline nondeterminism aside). Expected wall-time win: 30–50% locally.

## 4. Testing & verification

- Full suite (`pytest -q test_*.py`) passes after each phase; the B64 test is removed with its feature.
- End-to-end scan before/after the speed change: same account, same count — compare post counts (identical) and wall time (reduced).
- Grep-verify no dangling references: `XTS_SESSION_B64`, `refresh_session`, `render`, `/debug/` in code, docs, and CI.
- launchd verification: `./install_launchd.sh`, confirm dashboard responds without any terminal; `launchctl kickstart -k` (simulated crash) → service back within seconds; second-instance start → clear error, nonzero exit.

## Rollout order

1. Cloud-code removal (mechanical, test-gated)
2. Port-conflict startup check
3. Condition-based waits (behavioral, verified end-to-end)
4. README rewrite + launchd install/verify
