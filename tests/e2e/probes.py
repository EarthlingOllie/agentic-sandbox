"""The walkthrough's connection probes: what "this address is usable" means, per protocol.

``targets`` turns what ``urls --json`` printed into one target per reported address, and
``choose`` picks its probe by service name, as the fixture services are named: an HTTPS
request verified against the mkcert CA for web apps and their extra hosts, a login and a
query for ``mysql``, a ping for ``redis``, and a plain TCP connect for anything else.

Every probe is handed a *hostname* and lets the operating system resolve it, the way any
program on the machine would (``getaddrinfo``, under ``socket.create_connection``). Nothing
here asks the resolver for an address to connect to. ``diagnose`` does ask it directly, and
only to say in the report where a failed lookup went wrong; it returns text, never an
address, and cannot make a check pass.

Nothing here knows about a machine or a set; ``tests/unit/test_walkthrough_probes.py``
checks it without either.
"""

import http.client
import socket
import ssl
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, cast
from urllib.parse import urlsplit

import pymysql

from tests import dns

Protocol = Literal["https", "mysql", "redis", "tcp", "refused"]
TIMEOUT: Final = 10.0
# The discard port: nothing in the fixtures listens there. A container that is reached
# refuses a connection to it at once; an address that leads nowhere does not answer.
UNUSED_PORT: Final = 9
_BY_SERVICE: Final[Mapping[str, Protocol]] = {"mysql": "mysql", "redis": "redis"}
_LABELS: Final[Mapping[Protocol, str]] = {
    "https": "HTTPS request, verified against the mkcert CA, to",
    "mysql": "mysql login and query at",
    "redis": "redis ping at",
    "tcp": "TCP connect to",
    "refused": "TCP connect, to be refused by the container, to",
}


class ProbeError(Exception):
    """The address was reached, and what answered is not what should be there."""


@dataclass(frozen=True)
class Target:
    """One address of ``urls --json``: what was printed, and the parts a probe needs."""

    address: str  # as printed: a URL, ``host:port``, or a bare hostname
    service: str
    hostname: str
    port: int | None  # None: the service declares no port
    ip: str | None  # the container's address as `urls` reports it; None unless running
    web: bool = False


def targets(urls: Mapping[str, Any]) -> list[Target]:
    """Every address the document reports, in its order: web apps, services, the agent."""
    found: list[Target] = []
    for entry in cast(Sequence[Mapping[str, Any]], urls["web"]):
        url = urlsplit(str(entry["url"]))
        if url.scheme != "https" or url.hostname is None:
            raise ValueError(f"no probe for the web address {entry['url']}")
        found.append(
            Target(str(entry["url"]), str(entry["service"]), url.hostname, url.port or 443,
                   _ip(entry), web=True)
        )  # fmt: skip
    for entry in [*cast(Sequence[Mapping[str, Any]], urls["services"]), urls["agent"]]:
        address = str(entry["address"])
        hostname, _, port = address.partition(":")
        if port and not port.isdecimal():
            raise ValueError(f"no probe for the address {address} (not a TCP port)")
        found.append(
            Target(address, str(entry["service"]), hostname, int(port) if port else None,
                   _ip(entry))
        )  # fmt: skip
    return found


def _ip(entry: Mapping[str, Any]) -> str | None:
    return None if entry.get("ip") is None else str(entry["ip"])


def choose(target: Target, listens: Mapping[str, int]) -> tuple[Protocol, int]:
    """The probe for ``target`` and the port it goes to. ``listens`` has the port of each
    service that listens on one it does not declare."""
    if target.web:
        return "https", cast(int, target.port)
    if target.port is not None:
        return _BY_SERVICE.get(target.service, "tcp"), target.port
    if target.service in listens:
        return "tcp", listens[target.service]
    return "refused", UNUSED_PORT


def describe(target: Target, protocol: Protocol, port: int) -> str:
    """The probe as the report names it."""
    where = target.address if target.web else f"{target.hostname}:{port}"
    return f"{_LABELS[protocol]} {where}"


def lookup(hostname: str) -> str | None:
    """The address the operating system's resolver gives for ``hostname``; None if none."""
    try:
        found = socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror:
        return None
    return str(found[0][4][0])


def diagnose(hostname: str, server: str, port: int, timeout: float = 2.0) -> str:
    """For the report of a failed check: what the operating system says about ``hostname``
    next to what the resolver at ``server:port`` says when asked directly. It tells a
    machine that is not pointed at the resolver from a resolver that does not know the
    name. It never raises and returns no address."""
    system = lookup(hostname) or "nothing"
    try:
        answer = dns.lookup(hostname, server, port, timeout=timeout)
    except OSError as exc:  # a timeout included
        direct = f"did not answer ({type(exc).__name__})"
    else:
        direct = f"answers {', '.join(answer.addresses) or f'no address (rcode {answer.rcode})'}"
    return (
        f"diagnostic only, not a check: the operating system resolves {hostname} to {system}; "
        f"asked directly, the resolver at {server}:{port} {direct}"
    )


def connect(hostname: str, port: int) -> None:
    """A TCP connection to ``hostname:port`` opens."""
    socket.create_connection((hostname, port), timeout=TIMEOUT).close()


def refused(hostname: str, port: int) -> None:
    """The machine at ``hostname`` is reached and refuses ``port``, where nothing listens."""
    try:
        socket.create_connection((hostname, port), timeout=TIMEOUT).close()
    except ConnectionRefusedError:
        return
    raise ProbeError(f"something listens on {hostname}:{port}")


def https_get(url: str, ca: Path) -> bytes:
    """The body of ``GET url``, with the server's certificate verified against ``ca`` and
    nothing else (not the system's trust store), and against the hostname in ``url``. No
    proxy is used: ``http.client`` connects to the name itself."""
    parts = urlsplit(url)
    context = ssl.create_default_context(cafile=str(ca))
    connection = http.client.HTTPSConnection(
        cast(str, parts.hostname), parts.port or 443, timeout=TIMEOUT, context=context
    )
    try:
        connection.request("GET", parts.path or "/")
        response = connection.getresponse()
        body = response.read()
    finally:
        connection.close()
    if response.status != 200:
        raise ProbeError(f"GET {url}: HTTP {response.status}")
    return body


def mysql(hostname: str, port: int, password: str, *statements: str) -> list[tuple[Any, ...]]:
    """Log in to MySQL at ``hostname:port`` as root, run ``statements``, and return the rows
    of the last one. The session is encrypted with the server's own (self-signed)
    certificate, as the ``mysql`` client does by default; its identity is not what is
    being checked."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = pymysql.connect(
        host=hostname, port=port, user="root", password=password, ssl=context,
        connect_timeout=int(TIMEOUT), read_timeout=TIMEOUT, write_timeout=TIMEOUT,
        autocommit=True,
    )  # fmt: skip
    try:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
            return list(cursor.fetchall())
    finally:
        connection.close()


def redis(hostname: str, port: int, *command: str) -> str | None:
    """Send one command to Redis at ``hostname:port`` and return its reply: the text of a
    status or string reply, None for "no such key"."""
    request = b"*%d\r\n" % len(command) + b"".join(
        b"$%d\r\n%s\r\n" % (len(part.encode()), part.encode()) for part in command
    )
    with socket.create_connection((hostname, port), timeout=TIMEOUT) as connection:
        connection.sendall(request)
        with connection.makefile("rb") as replies:
            line = replies.readline().rstrip(b"\r\n")
            if line.startswith(b"+"):
                return line[1:].decode()
            if line.startswith(b"$"):
                size = int(line[1:])
                return None if size < 0 else replies.read(size).decode()
    raise ProbeError(f"{' '.join(command)} at {hostname}:{port} answered {line.decode()!r}")
