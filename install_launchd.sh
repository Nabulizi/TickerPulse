#!/bin/bash
# Install x-ticker-scraper as a launchd agent so the web app + auto-scan
# scheduler start on login and restart if they crash.
#
# Usage:   ./install_launchd.sh          # install + start
#          ./install_launchd.sh remove   # stop + uninstall
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.x-ticker-scraper"
PLIST_SRC="$PROJECT_DIR/launchd/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ "${1:-}" == "remove" ]]; then
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    rm -f "$PLIST_DST"
    echo "[✓] Removed $LABEL"
    exit 0
fi

if [[ ! -x "$PROJECT_DIR/venv/bin/python3" ]]; then
    echo "[✗] No venv found. Run the Setup steps in README.md first."
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/data"
sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$PLIST_SRC" > "$PLIST_DST"
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"
echo "[✓] Installed and started $LABEL"
echo "    Dashboard: http://localhost:8080"
echo "    Logs:      $PROJECT_DIR/data/launchd.log"
