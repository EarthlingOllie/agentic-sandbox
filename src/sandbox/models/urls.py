"""What ``urls`` reports: one set's host-resolvable addresses, each with its IP.

The table renderer and ``--json`` print the same model, so scripts see exactly the
addresses the table shows (``--json`` is ``model_dump_json``).
"""

from ipaddress import IPv4Address

from pydantic import BaseModel, ConfigDict

from sandbox.models.status import SetStateName


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class WebUrl(_Model):
    # ``https://<host>.<set>.test``, or ``...:<port>`` for a Vite dev server.
    url: str
    service: str
    # None when the set has never leased its IPs.
    ip: IPv4Address | None


class ServiceAddress(_Model):
    # ``<service>.<set>.test:<port>`` (``/udp`` appended for UDP), or the bare hostname for
    # a service that publishes no port.
    address: str
    service: str
    ip: IPv4Address | None


class SetUrls(_Model):
    name: str
    state: SetStateName
    # False unless every service runs: the addresses then come from the set's sticky lease.
    running: bool
    web: list[WebUrl]
    services: list[ServiceAddress]
