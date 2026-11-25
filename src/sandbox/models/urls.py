"""What ``urls`` reports: one set's addresses, each with its container's current address
while it runs.

The table renderer and ``--json`` print the same model, so scripts see exactly the
addresses the table shows (``--json`` is ``model_dump_json``).
"""

from ipaddress import IPv4Address

from pydantic import BaseModel, ConfigDict

from sandbox.models.status import SetStateName


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class WebUrl(_Model):
    # ``https://<host>.<set>.<domain>``, or ``...:<port>`` for a Vite dev server.
    url: str
    service: str
    # The container's current address, read from Docker; absent unless it is running.
    ip: IPv4Address | None = None


class ServiceAddress(_Model):
    # ``<service>.<set>.<domain>:<port>`` for each container port the service declares
    # (``/udp`` appended for UDP), or the bare hostname for a service that declares none.
    address: str
    service: str
    ip: IPv4Address | None = None


class SetUrls(_Model):
    name: str
    state: SetStateName
    # False unless every service runs; a service that is not running has no ``ip``.
    running: bool
    web: list[WebUrl]
    services: list[ServiceAddress]
