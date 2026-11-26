"""The sandbox's egress proxy (B0-02 fix): public addresses on 80/443 only."""

from __future__ import annotations

import socket
import socketserver
import threading

import pytest

from mandate_runner import proxy
from mandate_runner.proxy import CONNECT_PORTS, HTTP_PORTS, Refused, origin_request, public, split_hostport, vet

LAB = [
    "172.18.0.3",  # lab-postgres on lab_local
    "192.168.65.254",  # Docker Desktop's host address (published lab ports)
    "10.250.250.1",  # the sandbox network's gateway
    "127.0.0.1",
    "169.254.169.254",
    "100.64.0.1",
    "0.0.0.0",
    "::1",
    "fd00::1",
    "::ffff:192.168.65.254",
    "224.0.0.1",
]


@pytest.mark.parametrize("address", LAB)
def test_lab_host_and_private_addresses_are_not_public(address):
    assert not public(address)


@pytest.mark.parametrize("address", ["151.101.0.223", "140.82.112.3", "2a04:4e42::223"])
def test_internet_addresses_are_public(address):
    assert public(address)


def test_a_name_that_resolves_to_the_lab_is_refused():
    with pytest.raises(Refused) as e:
        vet("host.docker.internal", 443, CONNECT_PORTS, lambda h, p: ["192.168.65.254"])
    assert e.value.code == 403


def test_one_private_answer_among_public_ones_is_refused():
    with pytest.raises(Refused):
        vet("rebind.example", 443, CONNECT_PORTS, lambda h, p: ["151.101.0.223", "10.0.0.5"])


def test_only_443_for_connect_and_80_for_http():
    ok = lambda h, p: ["151.101.0.223"]  # noqa: E731
    assert vet("pypi.org", 443, CONNECT_PORTS, ok) == "151.101.0.223"
    assert vet("deb.debian.org", 80, HTTP_PORTS, ok) == "151.101.0.223"
    for port in (5432, 6379, 4000, 3100, 22, 80):
        with pytest.raises(Refused):
            vet("pypi.org", port, CONNECT_PORTS, ok)


def test_unresolvable_name_is_a_502():
    def fail(h, p):
        raise OSError("nxdomain")

    with pytest.raises(Refused) as e:
        vet("nope.invalid", 443, CONNECT_PORTS, fail)
    assert e.value.code == 502


def test_hostport_parsing():
    assert split_hostport("pypi.org:443", 443) == ("pypi.org", 443)
    assert split_hostport("pypi.org", 443) == ("pypi.org", 443)
    assert split_hostport("[2a04:4e42::223]:443", 443) == ("2a04:4e42::223", 443)


def test_http_request_is_rewritten_for_the_origin():
    host, port, head = origin_request(
        "GET", "http://deb.debian.org/debian/dists/bookworm/InRelease", "HTTP/1.1",
        ["Host: deb.debian.org", "Proxy-Connection: keep-alive", "Connection: keep-alive", "User-Agent: apt"],
    )  # fmt: skip
    assert (host, port) == ("deb.debian.org", 80)
    text = head.decode()
    assert text.startswith("GET /debian/dists/bookworm/InRelease HTTP/1.1\r\n")
    assert "Proxy-Connection" not in text and "keep-alive" not in text
    assert text.endswith("Connection: close\r\n\r\n")


def test_https_url_without_connect_is_refused():
    with pytest.raises(Refused):
        origin_request("GET", "https://pypi.org/simple/", "HTTP/1.1", [])


# --- end to end over real sockets ---------------------------------------------------


@pytest.fixture
def running_proxy(monkeypatch):
    def start(resolver, allow=None):
        if allow is not None:
            monkeypatch.setattr(proxy, "public", allow)

        class H(proxy.Handler):
            pass

        H.resolver = staticmethod(resolver)
        server = proxy.Server(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    servers = []
    yield lambda *a, **k: servers.append(start(*a, **k)) or servers[-1]
    for s in servers:
        s.shutdown()
        s.server_close()


def _ask(port: int, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
        c.sendall(request)
        data = b""
        while chunk := c.recv(4096):
            data += chunk
        return data


def test_connect_to_the_lab_through_the_proxy_gets_403(running_proxy):
    server = running_proxy(lambda h, p: ["192.168.65.254"])
    reply = _ask(server.server_address[1], b"CONNECT host.docker.internal:443 HTTP/1.1\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 403")


def test_connect_to_postgres_port_gets_403(running_proxy):
    server = running_proxy(lambda h, p: ["151.101.0.223"])
    reply = _ask(server.server_address[1], b"CONNECT example.org:5432 HTTP/1.1\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 403")


def test_allowed_connect_relays_bytes_both_ways(running_proxy, monkeypatch):
    class Echo(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.sendall(b"echo:" + self.request.recv(1024))

    upstream = socketserver.TCPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    port = upstream.server_address[1]
    monkeypatch.setattr(proxy, "CONNECT_PORTS", frozenset({port}))
    server = running_proxy(lambda h, p: ["127.0.0.1"], allow=lambda a: True)
    reply = _ask(server.server_address[1], f"CONNECT pypi.org:{port} HTTP/1.1\r\n\r\nhello".encode())
    upstream.shutdown()
    upstream.server_close()
    assert reply == b"HTTP/1.1 200 Connection established\r\n\r\necho:hello"


def test_connect_and_close_without_a_request_is_silent(running_proxy, caplog):
    server = running_proxy(lambda h, p: ["151.101.0.223"])
    socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5).close()
    import time

    time.sleep(0.2)
    assert "refused" not in caplog.text
