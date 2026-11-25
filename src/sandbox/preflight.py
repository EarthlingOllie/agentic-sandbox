"""Prerequisite checks: ``up`` fails before creating anything on a machine that could not
reach the set by name.

The ``prerequisites`` validation step runs for a set with services and names everything
that is wrong, each with its fix:

- the per-domain resolver configuration ``sudo sandbox setup`` installs
  (``sandbox.resolver_config``) is there, and it is for the configured domain. Without it
  the operating system never asks the resolver; with another domain's, it asks the resolver
  about names no set has. On Linux without systemd-resolved setup installs nothing (the
  developer adds one line to their own DNS server), so there is nothing to read: ``up``
  warns that it cannot check and carries on;
- on macOS, Docker is run by OrbStack, with its direct container access on. A set publishes
  nothing, so its services are reached at their containers' own addresses; of the macOS
  runtimes only OrbStack routes those from the Mac. The runtime is what the Docker daemon
  reports as its operating system (``docker info``). The setting is ``network_bridge`` in
  ``orbctl config show`` ("Allow access to container domains & IPs" in OrbStack's network
  settings); an OrbStack that does not report it is taken to have it on, its default.

The mkcert CA (``rootCA.pem``) is checked by the ``web-apps`` step, for sets that serve
HTTPS (``sandbox.web``). Sets without services need none of this.
"""

from typing import Final

from sandbox.docker import Docker
from sandbox.errors import SandboxError
from sandbox.pipeline import UpContext
from sandbox.platforms import Host
from sandbox.resolver_config import has_systemd_resolved, installed_domains, manual_line
from sandbox.setup import SETUP_COMMAND

ORBSTACK: Final = "OrbStack"
ORBSTACK_SETTINGS_COMMAND: Final = ("orbctl", "config", "show")
ORBSTACK_BRIDGE_KEY: Final = "network_bridge"
ORBSTACK_BRIDGE_LABEL: Final = "Allow access to container domains & IPs"


async def check_prerequisites(ctx: UpContext) -> None:
    """Validation step: fail naming every reason this machine could not reach the set."""
    if not ctx.graph.services:
        return
    problems: list[str] = []
    if ctx.host.is_macos:
        problem = await runtime_problem(ctx.host, ctx.docker)
        if problem:
            problems.append(problem)
    problem = await resolver_problem(ctx)
    if problem:
        problems.append(problem)
    if problems:
        raise SandboxError("this machine cannot reach the set:\n  " + "\n  ".join(problems))


async def resolver_problem(ctx: UpContext) -> str | None:
    """Why the operating system would not send the domain to the resolver; None when it
    does, or when nothing on this machine could tell."""
    domain = ctx.config.domain
    installed = installed_domains(ctx.host)
    if domain in installed:
        return None
    if installed:
        return (
            f"the configured domain is `{domain}`, but setup pointed this machine at the "
            f"resolver for `{'`, `'.join(installed)}`; rerun setup: "
            f"`{SETUP_COMMAND} --domain {domain}`"
        )
    if not ctx.host.is_macos and not await has_systemd_resolved(ctx.host):
        ctx.warn(
            f"cannot check that this machine sends names under `{domain}` to sandbox's "
            "resolver (there is no systemd-resolved for setup to configure); your DNS "
            f"server needs the line `{manual_line(domain)}`"
        )
        return None
    return (
        f"it does not send names under `{domain}` to sandbox's resolver (there is no "
        f"per-domain resolver configuration); run `{SETUP_COMMAND}`"
    )


async def runtime_problem(host: Host, docker: Docker) -> str | None:
    """Why containers are not reachable at their own addresses from this Mac; None when
    they are."""
    runtime = (await docker.operating_system()) or "an unnamed runtime"
    if runtime != ORBSTACK:
        return (
            f"Docker on this Mac is {runtime}, where containers are not reachable at their "
            "own addresses, which is where set hostnames point; on macOS sandbox needs "
            f"{ORBSTACK} (https://orbstack.dev)"
        )
    status, output = await host.run(list(ORBSTACK_SETTINGS_COMMAND))
    if status == 0 and _setting(output, ORBSTACK_BRIDGE_KEY) == "false":
        return (
            f"{ORBSTACK}'s direct container access is off; turn on "
            f'"{ORBSTACK_BRIDGE_LABEL}" in its network settings, or run '
            f"`orbctl config set {ORBSTACK_BRIDGE_KEY} true`"
        )
    return None


def _setting(settings: str, key: str) -> str | None:
    """``key``'s value in ``orbctl config show`` output (``key: value`` lines)."""
    for line in settings.splitlines():
        name, separator, value = line.partition(":")
        if separator and name.strip() == key:
            return value.strip().lower()
    return None
