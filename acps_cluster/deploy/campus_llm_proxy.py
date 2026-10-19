#!/usr/bin/env python3
"""校园网出口的白名单 CONNECT 代理。

只放行 CONNECT 到 CAMPUS_PROXY_ALLOWED 里的主机:端口，其余一律 403，
避免本机变成通用出口节点。域名在本机解析，因此校内 DNS 与校内源 IP 限制都成立。
"""
import os
import socket
import select
import threading
import logging
from datetime import datetime, timezone

LISTEN_HOST = os.environ.get("CAMPUS_PROXY_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("CAMPUS_PROXY_PORT", "18888"))
ALLOWED = {
    h.strip()
    for h in os.environ.get("CAMPUS_PROXY_ALLOWED", "myai.bupt.edu.cn:443").split(",")
    if h.strip()
}
RELAY_BUF = 65536
HANDSHAKE_TIMEOUT = 15
LOG_PATH = os.environ.get("CAMPUS_PROXY_LOG", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()] + ([logging.FileHandler(LOG_PATH)] if LOG_PATH else []),
)
log = logging.getLogger("campus-proxy")


def _relay(client, upstream):
    socks = (client, upstream)
    while True:
        try:
            readable, _, dead = select.select(socks, [], socks, 1800)
        except OSError:
            return
        if dead:
            return
        for src in readable:
            dst = upstream if src is client else client
            try:
                data = src.recv(RELAY_BUF)
            except OSError:
                return
            if not data:
                return
            try:
                dst.sendall(data)
            except OSError:
                return


def _read_request(client):
    buf = b""
    client.settimeout(HANDSHAKE_TIMEOUT)
    while b"\r\n\r\n" not in buf:
        chunk = client.recv(4096)
        if not chunk:
            return None
        buf += chunk
        if len(buf) > 65536:
            return None
    return buf.split(b"\r\n\r\n", 1)[0].decode("latin-1", "replace")


def _target(request_line):
    parts = request_line.split()
    if len(parts) < 2:
        return None, None
    host, _, port = parts[1].rpartition(":")
    return host.strip().lower(), port.strip()


def _reply(client, code, reason):
    body = b""
    client.sendall(
        f"HTTP/1.1 {code} {reason}\r\nContent-Length: 0\r\nProxy-Agent: campus-llm-proxy\r\n\r\n"
        .encode("latin-1")
    )


def handle(client, addr):
    peer = f"{addr[0]}:{addr[1]}"
    try:
        raw = _read_request(client)
        if raw is None:
            return
        request_line = raw.split("\r\n", 1)[0]
        method, _, rest = request_line.partition(" ")
        if method.upper() != "CONNECT":
            log.warning("拒绝非 CONNECT 请求 %r from %s", request_line, peer)
            _reply(client, 403, "Forbidden")
            return
        host, port = _target(request_line)
        key = f"{host}:{port or '443'}"
        if key not in ALLOWED:
            log.warning("拒绝白名单外目标 %s from %s", key, peer)
            _reply(client, 403, "Forbidden")
            return
        ip = socket.gethostbyname(host)
        upstream = socket.create_connection((ip, int(port or 443)), timeout=HANDSHAKE_TIMEOUT)
        upstream.settimeout(None)
        client.settimeout(None)
        _reply(client, 200, "Connection Established")
        log.info("隧道建立 %s → %s(%s:%s)", peer, host, ip, port or "443")
        _relay(client, upstream)
    except Exception as exc:
        log.info("会话结束 %s: %s", peer, exc.__class__.__name__)
    finally:
        try:
            client.close()
        except OSError:
            pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, LISTEN_PORT))
    srv.listen(64)
    log.info(
        "校园代理监听 %s:%s 白名单=%s 启动时间=%s",
        LISTEN_HOST, LISTEN_PORT, sorted(ALLOWED), datetime.now(timezone.utc).isoformat(),
    )
    while True:
        client, addr = srv.accept()
        threading.Thread(target=handle, args=(client, addr), daemon=True).start()


if __name__ == "__main__":
    main()
