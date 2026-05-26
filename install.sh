#!/bin/bash
# ──────────────────────────────────────────────────────────
# Clash IPv6 Relay — 安装脚本
# 用法: bash install.sh
# ──────────────────────────────────────────────────────────
set -e

RELAY_DIR="$HOME/clash6-relay"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_DEST="$LAUNCH_AGENTS_DIR/local.clash6-relay.plist"
LOG_DIR="$HOME/Library/Logs"

echo "======================================"
echo "  Clash IPv6 Relay — 安装"
echo "======================================"

# ── 1. 确保目录存在 ──
mkdir -p "$LAUNCH_AGENTS_DIR"
mkdir -p "$LOG_DIR"

# ── 2. 写入 LaunchAgent plist ──
cat > "$PLIST_DEST" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>local.clash6-relay</string>

    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>${RELAY_DIR}/relay.py</string>
        <string>--ports</string>
        <string>7890,7891</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${RELAY_DIR}</string>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/clash6-relay.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/clash6-relay.stderr.log</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>ThrottleInterval</key>
    <integer>5</integer>

    <!-- 提高文件描述符限制，避免 Too many open files -->
    <key>SoftResourceLimits</key>
    <dict>
        <key>NumberOfFiles</key>
        <integer>65536</integer>
    </dict>
    <key>HardResourceLimits</key>
    <dict>
        <key>NumberOfFiles</key>
        <integer>65536</integer>
    </dict>
</dict>
</plist>
PLIST

echo "✓  创建 LaunchAgent: $PLIST_DEST"

# ── 3. 加载 LaunchAgent ──
launchctl load "$PLIST_DEST" 2>/dev/null && echo "✓  LaunchAgent 已加载 (当前会话)" || echo "⚠️  加载失败，尝试 unload 后重试"
launchctl load -w "$PLIST_DEST" 2>/dev/null && echo "✓  LaunchAgent 已加载 (-w 覆盖)"

# ── 4. 等启动 + 验证 ──
sleep 3
if lsof -iTCP:7890 -sTCP:LISTEN -P -n 2>/dev/null | grep -q Python; then
    echo ""
    echo "✅  安装成功！relay 正在运行"
    echo ""
    echo "    IPv6 中继  [::]:7890  →  127.0.0.1:7890"
    echo ""
    echo "    在手机上配置 HTTP 代理:"
    echo "    ┌─────────────────────────────────────────────────┐"
    echo "    │  服务器:  [2408:8256:3103:55c0:d5:dd86:eed7:c91a]  │"
    echo "    │  端口:    7890                                     │"
    echo "    └─────────────────────────────────────────────────┘"
    echo ""
else
    echo ""
    echo "⚠️  已安装但未检测到 Python 进程监听 7890"
    echo "   请检查日志: tail -20 ~/Library/Logs/clash6-relay.log"
    echo "   确保 clash 正在运行并监听 127.0.0.1:7890"
fi
