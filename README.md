# Clash IPv6 Relay

让旧版 Clash（如 clash）监听的 7890 端口也能接收 **IPv6** 连接。

## 原理

```
手机 ── IPv6 :7890 ──→ [relay.py] ── TCP 转发 ──→ 127.0.0.1:7890 ──→ clash
```

`relay.py` 监听 `[::]:7890`（IPv6 双栈，macOS 上会自动同时接听 IPv4），
收到连接后原封不动转发到 `127.0.0.1:7890`（clash 监听的地址）。

## 快速开始

```bash
# 1. 确保 clash 已启动并监听 127.0.0.1:7890

# 2. 前台运行（测试用）
python3 ~/clash6-relay/relay.py

# 3. 后台运行
python3 ~/clash6-relay/relay.py --daemon

# 4. 停止后台进程
python3 ~/clash6-relay/relay.py --stop
```

## 开机自启

```bash
bash ~/clash6-relay/install.sh
```

这会创建一个 LaunchAgent（`~/Library/LaunchAgents/local.clash6-relay.plist`），
登录时自动启动 relay，崩溃后自动重启。

## 卸载

```bash
bash ~/clash6-relay/uninstall.sh
```

## 手机配置

| 字段 | 值 |
|:----|:----|
| 代理类型 | HTTP |
| 服务器地址 | `[你的 IPv6 地址]` |
| 端口 | `7890` |

获取你的 IPv6 地址：

```bash
ifconfig en0 | grep "inet6.*secured" | awk '{print $2}'
```

## 日志

```bash
tail -f ~/Library/Logs/clash6-relay.log
```

## 依赖

- Python 3（macOS 自带）
- 无需安装任何第三方包（纯标准库）
