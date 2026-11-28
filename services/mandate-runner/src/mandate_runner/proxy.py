"""The sandbox's only way out (B0-02 fix): an HTTP proxy that reaches the public internet only.

The sandbox sits on a Docker `internal` network with no route out. Image pulls,
pip and poetry go through this proxy, which refuses any destination that is not a
public (globally routable) address: the lab's containers, Docker Desktop's host
address (192.168.65.x, where the lab publishes Postgres, Redis, LiteLLM and
Paperclip), loopback, link-local and private ranges. The check runs outside the
sandbox, where PR code cannot change it, and on the address actually dialled, so
a name that resolves to a private address is refused too.

Only HTTPS (CONNECT to 443) and plain HTTP (port 80) are relayed.
"""

from __future__ import annotations

import ipaddress
import logging
import select
import socket
import socketserver
from collections.abc import Callable
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

CONNECT_PORTS = frozenset({443})
HTTP_PORTS = frozenset({80})
MAX_HEAD = 64 * 1024
IDLE = 300.0

Resolve = Callable[[str, int], list[str]]


class Refused(Exception):
    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def resolve(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def vet(host: str, port: int, allowed_ports: frozenset[int], resolver: Resolve = resolve) -> str:
    """The public address to dial, or Refused. Every address the name resolves to must be public."""
    if port not in allowed_ports:
        raise Refused(403, f"port {port} is not allowed")
    try:
        addresses = resolver(host, port)
    except OSError:
        raise Refused(502, f"cannot resolve {host}") from None
    if not addresses:
        raise Refused(502, f"cannot resolve {host}")
    for address in addresses:
        if not public(address):
            raise Refused(403, f"{host} resolves to a non-public address")
    return addresses[0]


def parse_head(head: bytes) -> tuple[str, str, str, list[str]]:
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3:
        raise Refused(400, "bad request line")
    method, target, version = parts
    return method, target, version, [h for h in lines[1:] if h]


def split_hostport(text: str, default: int) -> tuple[str, int]:
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        port = rest.removeprefix(":")
    else:
        host, _, port = text.rpartition(":") if text.count(":") == 1 else (text, "", "")
    try:
        return host, int(port) if port else default
    except ValueError:
        raise Refused(400, "bad port") from None


def origin_request(method: str, target: str, version: str, headers: list[str]) -> tuple[str, int, bytes]:
    """An absolute-form HTTP request rewritten for the origin, with Connection: close."""
    url = urlsplit(target)
    if url.scheme != "http" or not url.hostname:
        raise Refused(400, "only absolute http:// URLs or CONNECT are proxied")
    port = url.port or 80
    path = url.path or "/"
    if url.query:
        path += "?" + url.query
    kept = [
        h
        for h in headers
        if h.split(":", 1)[0].strip().lower() not in {"proxy-connection", "proxy-authorization", "connection"}
    ]
    head = "\r\n".join([f"{method} {path} {version}", *kept, "Connection: close", "", ""])
    return url.hostname, port, head.encode("latin-1")


def _pipe(a: socket.socket, b: socket.socket) -> None:
    sockets = [a, b]
    while True:
        ready, _, _ = select.select(sockets, [], [], IDLE)
        if not ready:
            return
        for s in ready:
            data = s.recv(65536)
            if not data:
                return
            (b if s is a else a).sendall(data)


class Handler(socketserver.BaseRequestHandler):
    resolver: Resolve = staticmethod(resolve)

    def handle(self) -> None:
        client: socket.socket = self.request
        client.settimeout(30)
        try:
            head, rest = self._read_head(client)
            method, target, version, headers = parse_head(head)
            if method == "CONNECT":
                host, port = split_hostport(target, 443)
                address = vet(host, port, CONNECT_PORTS, self.resolver)
                upstream = socket.create_connection((address, port), timeout=15)
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                if rest:
                    upstream.sendall(rest)
            else:
                host, port, request = origin_request(method, target, version, headers)
                address = vet(host, port, HTTP_PORTS, self.resolver)
                upstream = socket.create_connection((address, port), timeout=15)
                upstream.sendall(request + rest)
        except Refused as r:
            if r.code == 0:
                return  # connected and closed without a request (the healthcheck)
            log.info("refused %s: %s", self.client_address[0], r.reason)
            self._reply(client, r.code, r.reason)
            return
        except OSError as exc:
            self._reply(client, 502, f"upstream: {exc.__class__.__name__}")
            return
        with upstream:
            client.settimeout(None)
            upstream.settimeout(None)
            _pipe(client, upstream)

    @staticmethod
    def _read_head(client: socket.socket) -> tuple[bytes, bytes]:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = client.recv(4096)
            if not chunk:
                raise Refused(400 if data else 0, "incomplete request")
            data += chunk
            if len(data) > MAX_HEAD:
                raise Refused(431, "request head too large")
        head, _, rest = data.partition(b"\r\n\r\n")
        return head, rest

    @staticmethod
    def _reply(client: socket.socket, code: int, reason: str) -> None:
        body = f"mandate-runner-proxy: {reason}\n".encode()
        try:
            client.sendall(
                f"HTTP/1.1 {code} Refused\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
                f"Connection: close\r\n\r\n".encode()
                + body
            )
        except OSError:
            pass


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(port: int = 3128) -> int:
    with Server(("0.0.0.0", port), Handler) as server:
        log.info("mandate-runner-proxy up on :%d (public addresses only; ports 80, 443)", port)
        server.serve_forever()
    return 0
