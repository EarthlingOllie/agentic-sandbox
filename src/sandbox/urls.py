"""A set's addresses, for ``urls``: web apps as ``https://`` URLs, backing services as
``host:port`` on their conventional ports, and the agent's set hostname.

The hostnames and ports come from the set's plan and its repos' compose files (normalised
and merged the way ``up`` merges them, ``sandbox.compose.dedup``): a service is listed once
per container port it declares, whatever host-side binding the repo wrote next to it, and
without a port when it declares none. So a stopped set lists the same addresses it will
have when it is brought back up. Docker is asked whether the set is running and where each
running container is: that address is shown next to the hostname and is absent otherwise.

The agent is listed too, at ``agent.<set>.<domain>`` without a port: it declares none, and
something it serves is opened there on whatever port it listens on.
"""

from collections.abc import Mapping
from ipaddress import IPv4Address

from sandbox.agent import load_plan
from sandbox.compose import AGENT_SERVICE, project_name
from sandbox.compose.cli import Compose
from sandbox.compose.dedup import ServiceGraph
from sandbox.compose.dedup import resolve as resolve_services
from sandbox.compose.network import ContainerPort, container_ports, hostname
from sandbox.compose.services import load_repo_compose
from sandbox.compose.web import WebApp
from sandbox.config import Config
from sandbox.docker import Docker
from sandbox.git import Git
from sandbox.models.status import SetStateName
from sandbox.models.urls import ServiceAddress, SetUrls, WebUrl
from sandbox.names import validate_set_name
from sandbox.status import StatusReader
from sandbox.web import web_apps


class UrlsReader:
    def __init__(self, config: Config, git: Git, compose: Compose, docker: Docker) -> None:
        self._config = config
        self._git = git
        self._compose = compose
        self._docker = docker

    async def set_urls(self, set_name: str) -> SetUrls:
        validate_set_name(set_name)
        set_dir = self._config.sets_root / set_name
        plan = load_plan(self._config.sets_root, set_name)
        loaded = [load_repo_compose(name, spec.source) for name, spec in sorted(plan.repos.items())]
        repos = [repo for repo in loaded if repo is not None]
        graph = await resolve_services(self._compose, project_name(set_name), repos)
        domain = self._config.domain
        apps = web_apps(set_name, domain, set_dir, repos)
        reader = StatusReader(self._config, self._git, self._docker)
        state, addresses = await reader.addresses(set_name)
        return SetUrls(
            name=set_name,
            state=state,
            running=state is SetStateName.RUNNING,
            web=[url for app in apps for url in _web_urls(app, addresses.get(app.service))],
            services=_service_addresses(
                set_name, domain, graph, {app.service for app in apps}, addresses
            ),
            agent=ServiceAddress(
                address=hostname(AGENT_SERVICE, set_name, domain),
                service=AGENT_SERVICE,
                ip=addresses.get(AGENT_SERVICE),
            ),
        )


def _web_urls(app: WebApp, ip: IPv4Address | None) -> list[WebUrl]:
    found = [WebUrl(url=f"https://{host}", service=app.service, ip=ip) for host in app.hostnames]
    if app.vite is not None:
        found.append(WebUrl(url=f"{app.url}:{app.vite}", service=app.service, ip=ip))
    return found


def _service_addresses(
    set_name: str,
    domain: str,
    graph: ServiceGraph,
    web_services: set[str],
    addresses: Mapping[str, IPv4Address],
) -> list[ServiceAddress]:
    found: list[ServiceAddress] = []
    for name in graph.names():
        if name in web_services:
            continue
        host = hostname(name, set_name, domain)
        ip = addresses.get(name)
        ports = container_ports(name, graph.services[name].definition)
        for address in [_address(host, port) for port in ports] or [host]:
            found.append(ServiceAddress(address=address, service=name, ip=ip))
    return found


def _address(host: str, port: ContainerPort) -> str:
    suffix = "" if port.protocol == "tcp" else f"/{port.protocol}"
    return f"{host}:{port.port}{suffix}"
