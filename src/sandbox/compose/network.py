"""The set network: each service's IP-bound native ports and its ``.test`` aliases.

Every service of a set is a hostname, ``<service>.<set>.test``, with its own leased loopback
IP (``sandbox.leases``). Its container ports are published on that IP only, at the same
port number (``127.42.3.5:3306:3306``), so two sets running the same service never collide
on the host. The repo's own host-side bindings are replaced (Compose's ``!override``), not
merged: a ``0.0.0.0:3306`` from the repo would clash between sets.

On the set network (the project's ``default``) each service answers to its short name, its
set hostname and any extra hostnames (a web app's ``hosts``); networks the repo attaches it
to are kept (restated in full, so the entry also stands in for a relocated service's
``!override`` networks).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import Any, Final, cast

from sandbox.compose.dedup import Override
from sandbox.errors import SandboxError

SET_NETWORK: Final = "default"
TLD: Final = "test"


def hostname(service: str, set_name: str) -> str:
    return f"{service}.{set_name}.{TLD}"


@dataclass(frozen=True, order=True)
class ContainerPort:
    port: int
    protocol: str = "tcp"

    def __str__(self) -> str:
        return f"{self.port}/{self.protocol}"


def container_ports(service: str, definition: Mapping[str, Any]) -> list[ContainerPort]:
    """The container-side ports a service publishes, in declaration order, deduplicated.

    Accepts Compose's short syntax (``"[ip:][host:]container[/proto]"``, ranges included)
    and long syntax (``{target, protocol, ...}``), as written or as ``config`` prints it.
    """
    entries: object = definition.get("ports") or []
    if not isinstance(entries, list):
        raise SandboxError(f"service {service!r}: ports: expected a list")
    found: dict[ContainerPort, None] = {}
    for entry in cast(list[Any], entries):
        for port in _parse(service, entry):
            found[port] = None
    return list(found)


def _parse(service: str, entry: object) -> list[ContainerPort]:
    if isinstance(entry, bool):
        raise SandboxError(f"service {service!r}: invalid port {entry!r}")
    if isinstance(entry, int):
        return [ContainerPort(entry)]
    if isinstance(entry, dict):
        spec = cast(dict[str, Any], entry)
        protocol = str(spec.get("protocol") or "tcp")
        return [ContainerPort(port, protocol) for port in _range(service, str(spec["target"]))]
    if isinstance(entry, str):
        text, _, protocol = entry.partition("/")
        container = text.rsplit(":", 1)[-1]
        return [ContainerPort(port, protocol or "tcp") for port in _range(service, container)]
    raise SandboxError(f"service {service!r}: invalid port {entry!r}")


def _range(service: str, text: str) -> range:
    low, _, high = text.partition("-")
    try:
        first, last = int(low), int(high or low)
    except ValueError as exc:
        raise SandboxError(f"service {service!r}: invalid container port {text!r}") from exc
    return range(first, last + 1)


def network_fragment(
    set_name: str,
    definitions: Mapping[str, Mapping[str, Any]],
    ips: Mapping[str, IPv4Address],
    extra_aliases: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Ports and aliases for each service in ``definitions`` that has a leased IP;
    ``extra_aliases`` adds further set hostnames to a service (service name -> names)."""
    services: dict[str, Any] = {}
    for name, definition in sorted(definitions.items()):
        ip = ips.get(name)
        if ip is None or "network_mode" in definition:
            continue
        ports = [f"{ip}:{p.port}:{p.port}/{p.protocol}" for p in container_ports(name, definition)]
        networks = _networks(definition)
        own: dict[str, Any] = networks.get(SET_NETWORK) or {}
        aliases = [name, hostname(name, set_name), *(extra_aliases or {}).get(name, ())]
        extra = [a for a in cast(list[str], own.get("aliases") or []) if a not in aliases]
        networks[SET_NETWORK] = {**own, "aliases": [*aliases, *extra]}
        service: dict[str, Any] = {"networks": networks}
        if ports:
            service["ports"] = Override(ports)
        services[name] = service
    return {"services": services} if services else {}


def _networks(definition: Mapping[str, Any]) -> dict[str, Any]:
    """The service's networks in mapping form (``config`` prints a mapping; files may list)."""
    declared: object = definition.get("networks") or {}
    if isinstance(declared, list):
        return {str(name): None for name in cast(list[Any], declared)}
    if isinstance(declared, dict):
        return {
            str(name): (dict(cast(dict[str, Any], spec)) if isinstance(spec, dict) else None)
            for name, spec in cast(dict[Any, Any], declared).items()
        }
    raise SandboxError("networks: expected a list or a mapping")
