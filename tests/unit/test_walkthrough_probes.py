"""The walkthrough harness's connection probes, checked with no machine: which probe each
address of ``urls --json`` gets, the probes that need no real service (Redis's protocol
against a stand-in server, a plain connect, a refusal), and the resolver diagnostic, which
may explain a failure and never decides one."""

import socket
import socketserver
import struct
import threading
from collections.abc import Callable, Iterator

import pytest

from tests.e2e import probes
from tests.e2e.probes import Target

SET_URLS = {
    "name": "jira-123",
    "state": "running",
    "running": True,
    "web": [
        {"url": "https://web.jira-123.test", "service": "web", "ip": "172.20.0.4"},
        {"url": "https://admin.jira-123.test", "service": "web", "ip": "172.20.0.4"},
        {"url": "https://web.jira-123.test:5173", "service": "web", "ip": "172.20.0.4"},
    ],
    "services": [
        {"address": "mysql.jira-123.test:3306", "service": "mysql", "ip": "172.20.0.2"},
        {"address": "redis.jira-123.test:6379", "service": "redis", "ip": "172.20.0.3"},
        {"address": "search.jira-123.test:9200", "service": "search", "ip": "172.20.0.6"},
        {"address": "worker.jira-123.test", "service": "worker", "ip": "172.20.0.7"},
        {"address": "toolbox.jira-123.test", "service": "toolbox", "ip": "172.20.0.8"},
    ],
    "agent": {"address": "agent.jira-123.test", "service": "agent", "ip": "172.20.0.5"},
}


def test_every_address_urls_reports_is_a_target() -> None:
    found = probes.targets(SET_URLS)

    assert [(t.address, t.service, t.hostname, t.port, t.ip) for t in found] == [
        ("https://web.jira-123.test", "web", "web.jira-123.test", 443, "172.20.0.4"),
        ("https://admin.jira-123.test", "web", "admin.jira-123.test", 443, "172.20.0.4"),
        ("https://web.jira-123.test:5173", "web", "web.jira-123.test", 5173, "172.20.0.4"),
        ("mysql.jira-123.test:3306", "mysql", "mysql.jira-123.test", 3306, "172.20.0.2"),
        ("redis.jira-123.test:6379", "redis", "redis.jira-123.test", 6379, "172.20.0.3"),
        ("search.jira-123.test:9200", "search", "search.jira-123.test", 9200, "172.20.0.6"),
        ("worker.jira-123.test", "worker", "worker.jira-123.test", None, "172.20.0.7"),
        ("toolbox.jira-123.test", "toolbox", "toolbox.jira-123.test", None, "172.20.0.8"),
        ("agent.jira-123.test", "agent", "agent.jira-123.test", None, "172.20.0.5"),
    ]


def test_the_probe_is_chosen_by_service_name_with_a_plain_connect_as_the_fallback() -> None:
    listens = {"worker": 9000, "agent": 9001}

    chosen = [
        (t.address, *probes.choose(t, listens)) for t in probes.targets(SET_URLS) if t.port != 5173
    ]

    assert chosen == [
        ("https://web.jira-123.test", "https", 443),
        ("https://admin.jira-123.test", "https", 443),
        ("mysql.jira-123.test:3306", "mysql", 3306),
        ("redis.jira-123.test:6379", "redis", 6379),
        # Not a fixture service the harness knows a protocol for.
        ("search.jira-123.test:9200", "tcp", 9200),
        # No declared port: the port the harness knows it listens on.
        ("worker.jira-123.test", "tcp", 9000),
        # No declared port and nothing listening: the container must refuse the connection.
        ("toolbox.jira-123.test", "refused", probes.UNUSED_PORT),
        ("agent.jira-123.test", "tcp", 9001),
    ]


def test_a_udp_address_has_no_probe_and_says_so() -> None:
    urls = {
        "web": [],
        "services": [{"address": "dns.jira-123.test:53/udp", "service": "dns", "ip": None}],
        "agent": {"address": "agent.jira-123.test", "service": "agent", "ip": None},
    }

    with pytest.raises(ValueError, match=r"dns\.jira-123\.test:53/udp"):
        probes.targets(urls)


class _Redis(socketserver.StreamRequestHandler):
    """Enough of a Redis server: PING, SET and GET over RESP, in memory."""

    store: dict[bytes, bytes] = {}  # noqa: RUF012 - one stand-in server per test process

    def handle(self) -> None:
        while (header := self.rfile.readline()).startswith(b"*"):
            parts: list[bytes] = []
            for _ in range(int(header[1:])):
                size = int(self.rfile.readline()[1:])
                parts.append(self.rfile.read(size + 2)[:-2])
            match parts:
                case [b"PING"]:
                    reply = b"+PONG\r\n"
                case [b"SET", key, value]:
                    self.store[key] = value
                    reply = b"+OK\r\n"
                case [b"GET", key] if key in self.store:
                    reply = b"$%d\r\n%s\r\n" % (len(self.store[key]), self.store[key])
                case [b"GET", _]:
                    reply = b"$-1\r\n"
                case _:
                    reply = b"-ERR unknown command\r\n"
            self.wfile.write(reply)


@pytest.fixture
def serve() -> Iterator[Callable[[type[socketserver.BaseRequestHandler]], int]]:
    """Start a TCP server on a free loopback port for the test; returns its port."""
    servers: list[socketserver.ThreadingTCPServer] = []

    def start(handler: type[socketserver.BaseRequestHandler]) -> int:
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return int(server.server_address[1])

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def free_port() -> int:
    """A loopback port nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_redis_answers_a_ping_and_returns_what_was_stored(
    serve: Callable[[type[socketserver.BaseRequestHandler]], int],
) -> None:
    port = serve(_Redis)

    assert probes.redis("localhost", port, "PING") == "PONG"
    assert probes.redis("localhost", port, "SET", "set", "jira-123") == "OK"
    assert probes.redis("localhost", port, "GET", "set") == "jira-123"
    assert probes.redis("localhost", port, "GET", "nothing") is None


def test_a_redis_error_reply_is_a_failure(
    serve: Callable[[type[socketserver.BaseRequestHandler]], int],
) -> None:
    port = serve(_Redis)

    with pytest.raises(probes.ProbeError, match="ERR unknown command"):
        probes.redis("localhost", port, "FLUSHALL")


def test_something_that_is_not_redis_fails_the_ping(
    serve: Callable[[type[socketserver.BaseRequestHandler]], int],
) -> None:
    class Banner(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            self.wfile.write(b"220 smtp ready\r\n")

    with pytest.raises(probes.ProbeError, match="220 smtp ready"):
        probes.redis("localhost", serve(Banner), "PING")


def test_a_plain_connect_passes_where_something_listens_and_fails_where_nothing_does(
    serve: Callable[[type[socketserver.BaseRequestHandler]], int],
) -> None:
    probes.connect("localhost", serve(socketserver.BaseRequestHandler))

    with pytest.raises(OSError, match="refused"):
        probes.connect("localhost", free_port())


def test_refused_passes_only_when_the_machine_at_the_address_turns_the_connection_down(
    serve: Callable[[type[socketserver.BaseRequestHandler]], int],
) -> None:
    probes.refused("localhost", free_port())

    with pytest.raises(probes.ProbeError, match="something listens"):
        probes.refused("localhost", serve(socketserver.BaseRequestHandler))


def test_a_name_the_operating_system_does_not_know_does_not_resolve() -> None:
    # `.invalid` is reserved (RFC 2606): no resolver may answer it.
    assert probes.lookup("mysql.jira-123.invalid") is None
    assert probes.lookup("localhost") == "127.0.0.1"


class _Dns(socketserver.BaseRequestHandler):
    """A DNS server that answers every A question with 172.20.0.2, TTL 1."""

    def handle(self) -> None:
        packet, sock = self.request
        question = packet[12:]
        answer = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 1, 4) + socket.inet_aton("172.20.0.2")
        sock.sendto(
            packet[:2] + struct.pack("!HHHHH", 0x8180, 1, 1, 0, 0) + question + answer,
            self.client_address,
        )


def test_the_diagnostic_reports_what_the_resolver_itself_answers() -> None:
    with socketserver.UDPServer(("127.0.0.1", 0), _Dns) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            said = probes.diagnose("mysql.jira-123.test", "127.0.0.1", server.server_address[1])
        finally:
            server.shutdown()

    assert said == (
        "diagnostic only, not a check: the operating system resolves mysql.jira-123.test to "
        f"nothing; asked directly, the resolver at 127.0.0.1:{server.server_address[1]} "
        "answers 172.20.0.2"
    )


def test_the_diagnostic_never_fails_even_when_the_resolver_is_silent() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as silent:
        silent.bind(("127.0.0.1", 0))
        port = int(silent.getsockname()[1])

        said = probes.diagnose("mysql.jira-123.invalid", "127.0.0.1", port, timeout=0.2)

    assert "the operating system resolves mysql.jira-123.invalid to nothing" in said
    assert f"the resolver at 127.0.0.1:{port} did not answer" in said


def test_a_target_is_what_the_harness_reports(  # the label a developer reads in the report
) -> None:
    target = Target("mysql.jira-123.test:3306", "mysql", "mysql.jira-123.test", 3306, None)

    assert (
        probes.describe(target, "mysql", 3306)
        == "mysql login and query at mysql.jira-123.test:3306"
    )
