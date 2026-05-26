#!/usr/bin/env python3
"""
Clash IPv6 Relay — 把 IPv6 流量转发到本地 Clash
支持 .local 域名智能直连（绕过 Clash）
支持 HTTP CONNECT / HTTP / SOCKS5 三种代理协议
"""

import socket
import threading
import sys
import os
import signal
import logging
import argparse
import time
import resource
import re
import struct

# ─── 常量 ───────────────────────────────────────────────
HOME = os.path.expanduser("~")
DEFAULT_PORTS = [7890]
PID_FILE = os.path.join(HOME, "clash6-relay", "relay.pid")
LOG_FILE = os.path.join(HOME, "Library", "Logs", "clash6-relay.log")

# 不走代理的域名后缀
BYPASS_DOMAINS = (".local", ".lan", ".localdomain")

log = logging.getLogger("clash6-relay")


def setup_logging(daemon: bool):
    handlers = [logging.FileHandler(LOG_FILE, mode="a")]
    if not daemon:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )


# ─── 核心转发 ──────────────────────────────────────────

def pipe(src, dst, label, initial_data: bytes = b""):
    """双向数据搬运，可选带初始数据"""
    try:
        if initial_data:
            dst.sendall(initial_data)
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    except Exception as e:
        log.debug(f"{label} pipe error: {e}")
    finally:
        for s in (src, dst):
            try:
                s.close()
            except Exception:
                pass


def is_bypass_host(host: str) -> bool:
    """判断是否应该直连（不走 Clash）"""
    if not host:
        return False
    host = host.strip().lower()
    return any(host.endswith(suffix) for suffix in BYPASS_DOMAINS)


# ─── SOCKS5 直连处理 ─────────────────────────────────

def handle_socks5_inline(client, client_addr, initial_data, target_addr) -> bool:
    """
    完整处理 SOCKS5 协议（initial_data 包含首字节已读数据）。
    返回 True 表示连接已处理完毕。
    """
    try:
        data = initial_data  # 已包含首字节

        # ── 1. SOCKS5 握手 ──
        # 如果 initial_data 包含了完整握手，直接解析
        offset = 0
        if len(data) >= 2 and data[0] == 0x05:
            num_methods = data[1]
            expected_len = 2 + num_methods
            if len(data) < expected_len:
                # 需要读取更多握手数据
                data += client.recv(expected_len - len(data))
            # 不需要认证方法
        else:
            # 读取完整的握手
            data = client.recv(2)
            if len(data) < 2 or data[0] != 0x05:
                return False
            num_methods = data[1]
            client.recv(num_methods)

        # 回复：无认证
        client.sendall(b"\x05\x00")

        # ── 2. 读取 SOCKS5 请求 ──
        header = client.recv(4)
        if len(header) < 4:
            return False
        ver, cmd, rsv, atyp = header

        if atyp == 0x03:  # 域名
            nlen = client.recv(1)[0]
            host = client.recv(nlen).decode(errors="replace")
        elif atyp == 0x01:  # IPv4
            host = socket.inet_ntop(socket.AF_INET, client.recv(4))
        elif atyp == 0x04:  # IPv6
            host = socket.inet_ntop(socket.AF_INET6, client.recv(16))
        else:
            return False

        port_bytes = client.recv(2)
        port = int.from_bytes(port_bytes, "big")

        log.info(f"← [{client_addr[0]}]:{client_addr[1]} → SOCKS5 {host}:{port}")

        if is_bypass_host(host):
            return _socks5_direct(client, host, port, cmd, target_addr)
        else:
            return _socks5_via_clash(client, host, port, cmd, atyp, target_addr)

    except Exception as e:
        log.error(f"SOCKS5 error [{client_addr[0]}]:{client_addr[1]} — {e}")
        return True


def _socks5_direct(client, host, port, cmd, target_addr):
    """SOCKS5 直连目标服务器"""
    try:
        addr = socket.gethostbyname(host)
        target = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        target.settimeout(10)
        target.connect((addr, port))
        target.settimeout(None)
        client.settimeout(None)

        # SOCKS5 成功响应
        bnd = target.getsockname()
        bind_addr = socket.inet_aton(bnd[0]) if bnd[0] else b"\x00\x00\x00\x00"
        bind_port = bnd[1] if bnd[1] else 0
        client.sendall(b"\x05\x00\x00\x01" + bind_addr + bind_port.to_bytes(2, "big"))

        log.info(f"  → DIRECT {host}:{port} (SOCKS5 bypass)")

        # 双向管道
        t1 = threading.Thread(target=pipe, args=(client, target, f"→{host}:{port}"), daemon=True)
        t2 = threading.Thread(target=pipe, args=(target, client, f"←{host}:{port}"), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        return True

    except ConnectionRefusedError:
        log.warning(f"✗ DIRECT {host}:{port} — 连接被拒绝")
        client.sendall(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")  # connection refused
    except socket.timeout:
        log.warning(f"✗ DIRECT {host}:{port} — 连接超时")
        client.sendall(b"\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00")  # host unreachable
    except Exception as e:
        log.error(f"✗ DIRECT {host}:{port} — {e}")
        try:
            client.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")  # general failure
        except Exception:
            pass
    return True


def _socks5_via_clash(client, host, port, cmd, atyp, target_addr):
    """SOCKS5 非 .local 请求 → 通过 Clash"""
    try:
        clash = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        clash.settimeout(10)
        clash.connect(target_addr)

        # 以 SOCKS5 客户端身份连接到 Clash
        clash.sendall(b"\x05\x01\x00")
        clash.settimeout(5)
        try:
            clash_resp = clash.recv(2)
        except socket.timeout:
            raise Exception("Clash SOCKS5 握手超时")
        if len(clash_resp) < 2 or clash_resp[1] != 0x00:
            raise Exception(f"Clash 拒绝 SOCKS5 握手: {clash_resp.hex()}")

        # 重建 SOCKS5 请求转发给 Clash
        req = b"\x05" + bytes([cmd, 0x00, atyp])
        if atyp == 0x03:
            hb = host.encode()
            req += bytes([len(hb)]) + hb
        elif atyp == 0x01:
            req += socket.inet_aton(host)
        elif atyp == 0x04:
            req += socket.inet_pton(socket.AF_INET6, host)
        req += port.to_bytes(2, "big")
        clash.sendall(req)

        # 将 Clash 的 SOCKS5 响应透传给客户端（可能分多次到达）
        # 用双向 pipe 代替单次 recv，避免阻塞
        t1 = threading.Thread(target=pipe, args=(client, clash, f"→:{target_addr[1]}"), daemon=True)
        t2 = threading.Thread(target=pipe, args=(clash, client, f"←:{target_addr[1]}"), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    except ConnectionRefusedError:
        log.warning(f"✗ →:{target_addr[1]} — Clash 未运行")
    except Exception as e:
        log.error(f"✗ →:{target_addr[1]} (SOCKS5 {host}:{port}) — {e}")
    return True


# ─── HTTP 直连处理 ────────────────────────────────────

def extract_http_destination(data: bytes, client_addr: tuple) -> tuple:
    """
    解析 HTTP 代理请求数据，返回 (host, port, data) 或 (None, None, data)
    """
    if not data:
        return (None, None, b"")

    # HTTP CONNECT（HTTPS 代理）
    m = re.match(rb"CONNECT\s+([^:\s]+):(\d+)\s+HTTP", data, re.I)
    if m:
        host = m.group(1).decode()
        port = int(m.group(2))
        if is_bypass_host(host):
            log.info(f"← [{client_addr[0]}]:{client_addr[1]} → DIRECT {host}:{port} (CONNECT bypass)")
            return (host, port, data)
        return (None, None, data)

    # 普通 HTTP 请求
    m = re.match(rb"(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+\S+\s+HTTP", data, re.I)
    if m:
        hm = re.search(rb"Host:\s*([^\r\n]+)", data, re.I)
        if hm:
            raw = hm.group(1).decode().strip()
            host_only = raw.split(":")[0] if ":" in raw else raw
            if is_bypass_host(host_only):
                port = 80
                if ":" in raw:
                    port = int(raw.split(":")[1])
                log.info(f"← [{client_addr[0]}]:{client_addr[1]} → DIRECT {host_only}:{port} (HTTP bypass)")
                return (host_only, port, data)
        return (None, None, data)

    # 不是 HTTP → 透传
    return (None, None, data)


def http_direct_connect(client, host, port, initial_data):
    """HTTP 直连处理"""
    try:
        addr = socket.gethostbyname(host)
        target = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        target.settimeout(10)
        target.connect((addr, port))
        target.settimeout(None)
        client.settimeout(None)

        if initial_data.startswith(b"CONNECT"):
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            t1 = threading.Thread(target=pipe, args=(client, target, f"→{host}:{port}"), daemon=True)
            t2 = threading.Thread(target=pipe, args=(target, client, f"←{host}:{port}"), daemon=True)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
        else:
            target.sendall(initial_data)
            t1 = threading.Thread(target=pipe, args=(client, target, f"→{host}:{port}"), daemon=True)
            t2 = threading.Thread(target=pipe, args=(target, client, f"←{host}:{port}"), daemon=True)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

    except ConnectionRefusedError:
        log.warning(f"✗ DIRECT {host}:{port} — 连接被拒绝")
    except socket.timeout:
        log.warning(f"✗ DIRECT {host}:{port} — 连接超时")
    except Exception as e:
        log.error(f"✗ DIRECT {host}:{port} — {e}")
    finally:
        for s in (client,):
            try:
                s.close()
            except Exception:
                pass


def http_proxy_connect(client, target_addr, initial_data):
    """HTTP 代理转发到 Clash"""
    port_label = target_addr[1]
    try:
        target = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        target.settimeout(10)
        target.connect(target_addr)
        target.settimeout(None)
        client.settimeout(None)

        t1 = threading.Thread(
            target=pipe,
            args=(client, target, f"→:{port_label}"),
            kwargs={"initial_data": initial_data} if initial_data else {},
            daemon=True,
        )
        t2 = threading.Thread(
            target=pipe,
            args=(target, client, f"←:{port_label}"),
            daemon=True,
        )
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    except ConnectionRefusedError:
        log.warning(f"✗ →:{port_label} — Clash 未运行")
    except Exception as e:
        log.error(f"✗ →:{port_label} — {e}")


# ─── 路由调度 ──────────────────────────────────────────

def handle_client(client, addr, target_addr):
    """处理单个客户端连接 — 智能路由"""
    port_label = target_addr[1]
    log.info(f"← [{addr[0]}]:{addr[1]} → :{port_label}")

    try:
        # 读第一批数据来判断协议（超时2秒，超时则盲转 Clash）
        client.settimeout(2)
        try:
            data = client.recv(65536)
        except socket.timeout:
            # 客户端没发数据 → 原始转发
            client.settimeout(None)
            http_proxy_connect(client, target_addr, b"")
            return
        finally:
            client.settimeout(None)

        if not data:
            http_proxy_connect(client, target_addr, b"")
            return

        # ── 检测 SOCKS5（首字节 0x05）──
        if data[0] == 0x05:
            if handle_socks5_inline(client, addr, data, target_addr):
                return

        # ── 检测 HTTP CONNECT / HTTP ──
        host, port, _ = extract_http_destination(data, addr)
        if host and port:
            http_direct_connect(client, host, port, data)
            return

        # ── 走 Clash ──
        http_proxy_connect(client, target_addr, data)

    except Exception as e:
        log.error(f"✗ [{addr[0]}]:{addr[1]} → :{port_label} — {e}")
    finally:
        try:
            client.close()
        except Exception:
            pass
        log.info(f"→ [{addr[0]}]:{addr[1]} → :{port_label} disconnected")


# ─── 进程管理 ───────────────────────────────────────────

def read_pid():
    if os.path.exists(PID_FILE):
        with open(PID_FILE) as f:
            return int(f.read().strip())
    return None


def write_pid():
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def stop_daemon():
    pid = read_pid()
    if pid:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pid = None

    if not pid:
        import subprocess
        try:
            result = subprocess.run(
                ["pgrep", "-f", "relay.py"],
                capture_output=True, text=True
            )
            pids = [int(p) for p in result.stdout.strip().split() if p]
            pids = [p for p in pids if p != os.getpid()]
            if pids:
                pid = pids[0]
        except Exception:
            pass

    if not pid:
        print("⚠️   relay 未在运行")
        return
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"✓  已发送 SIGTERM 到 PID {pid}")
        for _ in range(10):
            time.sleep(0.5)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except ProcessLookupError:
        print(f"⚠️  进程 {pid} 不存在")
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)


def cleanup(signum, frame):
    log.info(f"收到信号 {signum}，正在关闭...")
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)
    sys.exit(0)


# ─── 服务 ───────────────────────────────────────────────

def serve_port(port):
    listen_addr = ("::", port)
    target_addr = ("127.0.0.1", port)

    server = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(listen_addr)
    except OSError as e:
        log.error(f"绑定 [::]:{port} 失败: {e}")
        return

    server.listen(256)
    log.info(f"  [::]:{port}  →  127.0.0.1:{port}  (S+S+HTTP, .local bypass)")

    while True:
        try:
            client, addr = server.accept()
            threading.Thread(
                target=handle_client,
                args=(client, addr, target_addr),
                daemon=True,
            ).start()
        except Exception as e:
            log.error(f"Accept error on :{port}: {e}")


def serve_forever(ports):
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < 4096:
            target = min(65536, hard)
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            log.info(f"  RLIMIT_NOFILE: {soft} → {target}")
    except Exception as e:
        log.warning(f"  设置 RLIMIT_NOFILE 失败: {e}")

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)
    write_pid()

    log.info("=" * 54)
    log.info("  Clash IPv6 Relay started")
    log.info(f"  PID:      {os.getpid()}")
    log.info(f"  Log:      {LOG_FILE}")
    for p in ports:
        log.info(f"  [::]:{p}  →  127.0.0.1:{p} (SOCKS5+HTTP, .local bypass)")
    log.info("=" * 54)

    threads = []
    for port in ports:
        t = threading.Thread(target=serve_port, args=(port,), daemon=True)
        t.start()
        threads.append(t)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log.info("收到 KeyboardInterrupt，正在关闭...")
        sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description="Clash IPv6 Relay — 支持 .local 直连")
    parser.add_argument("--daemon", "-d", action="store_true", help="后台运行（已废弃，请用 launchd）")
    parser.add_argument("--stop", action="store_true", help="停止后台运行的 relay")
    parser.add_argument(
        "--ports", "-p",
        default=",".join(str(p) for p in DEFAULT_PORTS),
        help="转发端口列表，逗号分隔（默认: 7890）"
    )
    args = parser.parse_args()

    if args.stop:
        stop_daemon()
        return

    try:
        ports = [int(p.strip()) for p in args.ports.split(",") if p.strip()]
    except ValueError:
        print(f"❌ 无效的端口列表: {args.ports}")
        sys.exit(1)

    if args.daemon:
        print("⚠️  已废弃: launchd 会自动管理进程，不需要 --daemon。直接运行即可。")

    setup_logging(daemon=False)
    serve_forever(ports)


if __name__ == "__main__":
    main()
