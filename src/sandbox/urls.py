"""A set's host-resolvable addresses, for ``urls``: web apps as ``https://`` URLs, backing
services as ``host:port``, each with its loopback IP.

Everything comes from the set's plan, its repos' compose files (normalised and merged the
way ``up`` merges them, ``sandbox.compose.dedup``) and its sticky lease (``sandbox.leases``),
so a stopped set lists the same addresses it will have when it is brought back up. Docker
is asked only whether the set is running.
"""

from ipaddress import IPv4Address

from sandbox.agent import load_plan
from sandbox.bindings import lease_store
from sandbox.compose import project_name
from sandbox.compose.cli import Compose
from sandbox.compose.dedup import ServiceGraph
from sandbox.compose.dedup import resolve as resolve_services
from sandbox.compose.network import ContainerPort, container_ports, hostname
from sandbox.compose.services import load_repo_compose
from sandbox.compose.web import WebApp
from sandbox.config import Config
from sandbox.docker import Docker
from sandbox.git import Git
from sandbox.leases import SetLease
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
        apps = web_apps(set_name, set_dir, repos)
        lease = lease_store(self._config).read(set_name)
        state = await self._state(set_name)
        return SetUrls(
            name=set_name,
            state=state,
            running=state is SetStateName.RUNNING,
            web=[url for app in apps for url in _web_urls(app, lease)],
            services=_service_addresses(set_name, graph, {app.service for app in apps}, lease),
        )

    async def _state(self, set_name: str) -> SetStateName:
        listing = await StatusReader(self._config, self._git, self._docker).list_sets()
        for entry in listing.sets:
            if entry.name == set_name:
                return entry.state
        # The plan exists, so the set is listed; a plan deleted since reads as stopped.
        return SetStateName.STOPPED


def _ip(lease: SetLease | None, host: str) -> IPv4Address | None:
    return lease.ips.get(host) if lease is not None else None


def _web_urls(app: WebApp, lease: SetLease | None) -> list[WebUrl]:
    ip = _ip(lease, app.hostname)
    found = [WebUrl(url=f"https://{host}", service=app.service, ip=ip) for host in app.hostnames]
    if app.vite is not None:
        found.append(WebUrl(url=f"{app.url}:{app.vite}", service=app.service, ip=ip))
    return found


def _service_addresses(
    set_name: str, graph: ServiceGraph, web_services: set[str], lease: SetLease | None
) -> list[ServiceAddress]:
    found: list[ServiceAddress] = []
    for name in graph.names():
        if name in web_services:
            continue
        host = hostname(name, set_name)
        ip = _ip(lease, host)
        ports = container_ports(name, graph.services[name].definition)
        for address in [_address(host, port) for port in ports] or [host]:
            found.append(ServiceAddress(address=address, service=name, ip=ip))
    return found


def _address(host: str, port: ContainerPort) -> str:
    suffix = "" if port.protocol == "tcp" else f"/{port.protocol}"
    return f"{host}:{port.port}{suffix}"
