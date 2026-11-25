"""A minimal DNS client: one A-record question over UDP, sent to the resolver directly.

The project has no DNS library and the tests must not go through the operating system's
resolver (pointing that at the resolver is ``setup``'s job), so the question is built and
the answer parsed by hand (RFC 1035).
"""

import secrets
import socket
import struct
import time
from dataclasses import dataclass

NOERROR, SERVFAIL, NXDOMAIN, REFUSED = 0, 2, 3, 5
_TYPE_A, _CLASS_IN = 1, 1


@dataclass(frozen=True)
class Answer:
    rcode: int
    # The IPv4 addresses answered, and the largest TTL among them (None without answers).
    addresses: tuple[str, ...]
    ttl: int | None
    seconds: float  # how long the server took to answer


def lookup(name: str, server: str, port: int, timeout: float = 5.0) -> Answer:
    """Ask ``server:port`` for the A records of ``name``; ``TimeoutError`` if it stays silent."""
    query_id = secrets.randbits(16)
    question = b"".join(
        bytes([len(label)]) + label.encode("ascii") for label in name.rstrip(".").split(".")
    )
    packet = (
        struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0)  # recursion desired, one question
        + question
        + b"\0"
        + struct.pack("!HH", _TYPE_A, _CLASS_IN)
    )
    started = time.monotonic()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(packet, (server, port))
        while True:
            reply, _ = sock.recvfrom(4096)
            if len(reply) >= 12 and struct.unpack("!H", reply[:2])[0] == query_id:
                break
    seconds = time.monotonic() - started
    _, flags, questions, answers, _, _ = struct.unpack("!HHHHHH", reply[:12])
    offset = 12
    for _ in range(questions):
        offset = _skip_name(reply, offset) + 4
    addresses: list[str] = []
    ttls: list[int] = []
    for _ in range(answers):
        offset = _skip_name(reply, offset)
        rtype, _, ttl, length = struct.unpack("!HHIH", reply[offset : offset + 10])
        offset += 10
        if rtype == _TYPE_A and length == 4:
            addresses.append(socket.inet_ntoa(reply[offset : offset + 4]))
            ttls.append(ttl)
        offset += length
    return Answer(flags & 0x000F, tuple(addresses), max(ttls) if ttls else None, seconds)


def _skip_name(reply: bytes, offset: int) -> int:
    """The offset just past the (possibly compressed) name starting at ``offset``."""
    while True:
        length = reply[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0 == 0xC0:  # a pointer ends the name
            return offset + 2
        offset += 1 + length
