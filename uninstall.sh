#!/bin/bash
# ──────────────────────────────────────────────────────────
# Clash IPv6 Relay — 卸载脚本
# ──────────────────────────────────────────────────────────
set -e

PLIST_DEST="$HOME/Library/LaunchAgents/local.clash6-relay.plist"
RELAY_DIR="$HOME/clash6-relay"

echo "======================================"
echo "  Clash IPv6 Relay — 卸载"
echo "======================================"

# ── 1. 停止并卸载 LaunchAgent ──
if [ -f "$PLIST_DEST" ]; then
    launchctl unload "$PLIST_DEST" 2>/dev/null && echo "✓  已停止 LaunchAgent"
    launchctl unload -w "$PLIST_DEST" 2>/dev/null || true
    rm "$PLIST_DEST"
    echo "✓  已删除 LaunchAgent plist"
fi

# ── 2. 停止 relay 进程 ──
python3 "$RELAY_DIR/relay.py" --stop 2>/dev/null || true


echo "✅  卸载完成"
