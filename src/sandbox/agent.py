"""Running commands in a set's containers: the agent (in its workspace) or a service.

Shared by the verbs that open something inside a set (``sh`` now, ``claude`` later). The
target is resolved against the set's plan and its repos' compose files, so a typo fails
with a helpful message before ``docker compose exec`` runs.
"""

from dataclasses import dataclass
from pathlib import Path

from sandbox.compose import AGENT_SERVICE, project_name
from sandbox.compose.agent import workspace
from sandbox.compose.cli import Compose
from sandbox.compose.services import load_repo_compose
from sandbox.errors import SandboxError
from sandbox.plan import SetPlan, load_set_plan, plan_path


class TargetError(SandboxError):
    """The user named a repo or service the set doesn't have (a usage error)."""


@dataclass(frozen=True)
class ExecTarget:
    project: str
    service: str
    workdir: str | None  # None: the service's own working directory


def load_plan(sets_root: Path, set_name: str) -> SetPlan:
    set_dir = sets_root / set_name
    if not plan_path(set_dir).exists():
        raise SandboxError(f"no set {set_name!r}: {plan_path(set_dir)} does not exist")
    return load_set_plan(set_dir)


def set_services(plan: SetPlan) -> list[str]:
    """Every service the set runs: the agent plus each repo's declared services."""
    names = {AGENT_SERVICE}
    for repo, spec in plan.repos.items():
        repo_compose = load_repo_compose(repo, spec.source)
        if repo_compose is not None:
            names.update(repo_compose.services)
    return sorted(names)


def agent_target(set_name: str, plan: SetPlan, repo: str | None = None) -> ExecTarget:
    """The agent, starting in the set's workspace or in ``repo``'s worktree."""
    workdir = workspace(set_name)
    if repo is not None:
        if repo not in plan.repos:
            raise TargetError(
                f"no repo {repo!r} in set {set_name!r} (repos: {', '.join(sorted(plan.repos))})"
            )
        workdir = f"{workdir}/{repo}"
    return ExecTarget(project_name(set_name), AGENT_SERVICE, workdir)


def service_target(set_name: str, plan: SetPlan, service: str) -> ExecTarget:
    if service == AGENT_SERVICE:
        return agent_target(set_name, plan)
    services = set_services(plan)
    if service not in services:
        hint = f"sandbox {set_name} sh --repo {service}"
        raise TargetError(
            f"no service {service!r} in set {set_name!r} (services: {', '.join(services)}); "
            f"to open a shell in a repo's worktree use: {hint}"
        )
    return ExecTarget(project_name(set_name), service, None)


async def run_in(compose: Compose, target: ExecTarget, command: list[str]) -> int:
    """Run ``command`` attached to this terminal; returns its exit status."""
    return await compose.exec(target.project, target.service, command, target.workdir)
