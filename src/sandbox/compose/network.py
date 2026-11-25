"""The set network: each service's set hostname aliases, and nothing published.

Every service of a set is a hostname, ``<service>.<set>.<domain>`` (the config's ``domain``,
``test`` by default). Nothing is published on the developer's machine: a service is reached
at its container's own address on its conventional port, so two sets running the same
service never collide, and the resolver (``sandbox.resolver``) answers the hostname with
that address. The repo's own host-side bindings are reset (Compose's ``!override`` with an
empty list replaces them; a plain list would be merged in): a ``0.0.0.0:3306`` from the repo
would clash between sets and open the port to the network. The container-side ports a repo
declares are still parsed (:func:`container_ports`): they are what ``urls`` and ``up``
report.

On the set network (the project's ``default``) each service answers to its short name, its
set hostname and any extra hostnames (a web app's ``hosts``); networks the repo attaches it
to are kept (restated in full, so the entry also stands in for a relocated service's
``!override`` networks).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

from sandbox.compose.dedup import Override
from sandbox.errors import SandboxError

SET_NETWORK: Final = "default"


def hostname(service: str, set_name: str, domain: str) -> str:
    """The set hostname of a service (or of a web app's extra host)."""
    return f"{service}.{set_name}.{domain}"


@dataclass(frozen=True, order=True)
class ContainerPort:
    port: int
    protocol: str = "tcp"

    def __str__(self) -> str:
        return f"{self.port}/{self.protocol}"


def container_ports(service: str, definition: Mapping[str, Any]) -> list[ContainerPort]:
    """The container-side ports a service declares, in declaration order, deduplicated.

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
    domain: str,
    definitions: Mapping[str, Mapping[str, Any]],
    extra_aliases: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """No published ports for any service in ``definitions``, and its aliases on the set
    network; ``extra_aliases`` adds further set hostnames to a service (service name ->
    names). A service with a ``network_mode`` is on no network, so it only loses its ports."""
    services: dict[str, Any] = {}
    for name, definition in sorted(definitions.items()):
        service: dict[str, Any] = {"ports": Override([])}
        services[name] = service
        if "network_mode" in definition:
            continue
        networks = _networks(definition)
        own: dict[str, Any] = networks.get(SET_NETWORK) or {}
        aliases = [name, hostname(name, set_name, domain), *(extra_aliases or {}).get(name, ())]
        extra = [a for a in cast(list[str], own.get("aliases") or []) if a not in aliases]
        networks[SET_NETWORK] = {**own, "aliases": [*aliases, *extra]}
        service["networks"] = networks
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
