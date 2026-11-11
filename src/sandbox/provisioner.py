"""Provisioner: orchestrates ``up`` and ``down`` for one set.

``up`` resolves the plan, then runs the step pipeline (``sandbox.pipeline``):

- ``VALIDATION_STEPS`` check every repo, its ``.sandbox/`` and compose file, deduplicate
  same-named services across repos (``sandbox.compose.dedup``), check the agent override
  and its image (``sandbox.agent_override``), check web apps, hostname clashes and the
  mkcert CA (``sandbox.web``), check what ``sudo sandbox setup`` installs
  (``sandbox.preflight``), check init hooks target the set's services
  (``sandbox.hooks``), then observe the set's state.
  Nothing is created until all of them pass.
- The state table (``sandbox.state``) decides: provision, resume, no-op or recreate.
- ``PROVISION_STEPS`` write the plan, ensure the agent's clones, issue or reuse the set's TLS
  certificate (``sandbox.web``) and bring services up healthy as
  Compose project ``sandbox-<set>``, run the repos' init hooks in them (``sandbox.hooks``),
  then write the set's hosts block (``sandbox.hosts``); on failure or Ctrl-C, started steps
  roll back.

A later concern adds its step in its own module and one line to one of the two lists.
After the steps (and on a no-op), ``up`` syncs each clone's ``agent/<set>`` into its source
repo (``sandbox.clones``). ``down`` stops the project, removes the hosts block, syncs the
same way and leaves the clones alone.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from sandbox.agent_image import DockerImageProbe, ImageProbe
from sandbox.agent_override import check_agent_override
from sandbox.bindings import (
    Binding,
    bindings,
    lease_ips,
    lease_store,
    service_ips,
    up_lock_path,
)
from sandbox.certs import Mkcert, SubprocessMkcert
from sandbox.clones import CloneManager, CloneOutcome, CloneResult
from sandbox.compose import AGENT_SERVICE, project_name
from sandbox.compose.cli import Compose
from sandbox.compose.dedup import resolve as resolve_services
from sandbox.compose.override import OverrideContext, write_override
from sandbox.compose.services import SANDBOX_DIRNAME, RepoCompose, load_repo_compose
from sandbox.compose.web import extra_hostnames
from sandbox.config import Config
from sandbox.errors import SandboxError
from sandbox.git import Git
from sandbox.hooks import check_hooks, run_hooks
from sandbox.hosts import HostsWriter, hosts_writer, remove_hosts_block, write_hosts_block
from sandbox.locking import file_lock
from sandbox.names import agent_branch, validate_repo_name, validate_set_name
from sandbox.pipeline import Step, UpContext, Warn, fail_fast, provision, shielded, validate
from sandbox.plan import (
    SetPlan,
    SetRepo,
    TemplatePlan,
    TemplateRepo,
    load_set_plan,
    plan_path,
    write_new_plan,
)
from sandbox.platforms import Host
from sandbox.preflight import check_prerequisites
from sandbox.state import UpAction, decide, observe
from sandbox.web import check_web_apps, issue_certificate

__all__ = ["PROVISION_STEPS", "VALIDATION_STEPS", "Provisioner", "Step", "UpContext"]


@dataclass(frozen=True)
class UpResult:
    set_name: str
    set_dir: Path
    plan_file: Path
    plan_created: bool
    clones: list[CloneResult]  # empty for a no-op
    project: str
    # Service names started under `project`; empty for a set without services.
    services: list[str]
    action: UpAction
    # Each service hostname with its leased IP and native ports.
    bindings: list[Binding]
    # One line per repo whose agent branch couldn't be synced into its source.
    sync_notes: list[str]


@dataclass(frozen=True)
class DownResult:
    set_name: str
    set_dir: Path
    project: str
    sync_notes: list[str]


# --- validation steps: create nothing ------------------------------------------------------


async def _validate_repos(ctx: UpContext) -> None:
    errors: list[str] = []
    for name, repo in ctx.plan.repos.items():
        problem = await _source_problem(ctx.git, name, repo.source)
        if problem:
            errors.append(problem)
            continue
        if not await ctx.git.branch_exists(
            repo.source, ctx.branch
        ) and not await ctx.git.revision_exists(repo.source, repo.base):
            errors.append(f"{name}: base {repo.base!r} does not exist in {repo.source}")
    if errors:
        raise SandboxError("\n".join(errors))


async def _load_composes(ctx: UpContext) -> None:
    """Each repo's compose file in repo-name order; repos without one have no services."""
    errors: list[str] = []
    found: list[RepoCompose] = []
    for name, repo in sorted(ctx.plan.repos.items()):
        try:
            repo_compose = load_repo_compose(name, repo.source)
        except SandboxError as exc:
            errors.append(str(exc))
            continue
        if repo_compose is not None:
            found.append(repo_compose)
    if errors:
        raise SandboxError("\n".join(errors))
    ctx.repo_composes = found
    ctx.services = sorted({service for rc in found for service in rc.services})


async def _dedup_services(ctx: UpContext) -> None:
    """One instance per same-named service, or fail with a diff naming both repos."""
    ctx.graph = await resolve_services(ctx.compose, ctx.project, ctx.repo_composes)
    ctx.services = ctx.graph.names()


async def _observe_state(ctx: UpContext) -> None:
    """Read the set's state (last, so it reflects Docker as close to acting as possible)."""
    running: set[str] = set()
    if ctx.plan_on_disk:
        running = await ctx.compose.running_services(ctx.project)
    ctx.running = running
    # Every set runs the agent next to its repos' services.
    expected = [*ctx.services, AGENT_SERVICE]
    ctx.state = observe(has_plan=ctx.plan_on_disk, expected=expected, running=running)
    ctx.action = decide(ctx.state, recreate=ctx.recreate)


# --- provision steps: create, and undo on failure ----------------------------------------


async def _write_plan(ctx: UpContext) -> None:
    """Write the plan unless one exists; never overwrite one. Kept on rollback."""
    ctx.set_dir.mkdir(parents=True, exist_ok=True)
    if ctx.plan_on_disk:
        return
    try:
        write_new_plan(ctx.plan_file, ctx.plan)
        ctx.plan_created = True
    except SandboxError:
        # Another process created this set first: its plan wins if it matches ours.
        on_disk = load_set_plan(ctx.set_dir)
        _refuse_if_different(ctx.set_name, ctx.plan_file, ctx.plan, on_disk)
        ctx.plan = on_disk


async def _ensure_clones(ctx: UpContext) -> None:
    """The agent's clone of each repo, concurrently; existing ones are left alone. Kept on
    rollback."""
    ctx.clones = await fail_fast(
        ctx.clone_manager.ensure(
            repo=name,
            source=repo.source,
            path=ctx.set_dir / name,
            branch=ctx.branch,
            base=repo.base,
        )
        for name, repo in sorted(ctx.plan.repos.items())
    )
    for result in ctx.clones:
        if result.outcome is CloneOutcome.REUSED_BRANCH:
            ctx.warn(
                f"{result.repo}: reusing existing branch {result.branch}; "
                f"base {result.base!r} from the plan was ignored"
            )


async def _stop_for_recreate(ctx: UpContext) -> None:
    if ctx.action is UpAction.RECREATE:
        await ctx.compose.down(ctx.project)


async def _compose_up(ctx: UpContext) -> None:
    # Every repo's own file, then the override: one instance per shared service, plus the
    # agent every set runs (so Compose runs even when no repo declares services).
    override = write_override(
        OverrideContext(
            ctx.set_name,
            ctx.set_dir,
            ctx.repo_composes,
            ctx.graph,
            ctx.plan.sources(),
            ctx.plan.agent,
            service_ips(ctx.set_name, ctx.graph, ctx.lease),
            web_apps=ctx.web_apps,
            root_ca=ctx.root_ca,
            tls_dir=ctx.certificate.directory if ctx.certificate else None,
        )
    )
    await ctx.compose.up(ctx.project, [*(rc.file for rc in ctx.repo_composes), override])


async def _compose_down(ctx: UpContext) -> None:
    await ctx.compose.down(ctx.project)


VALIDATION_STEPS: Final[list[Step]] = [
    Step("repos", _validate_repos),
    Step("compose-files", _load_composes),
    Step("dedup", _dedup_services),
    Step("web-apps", check_web_apps),
    Step("agent-override", check_agent_override),
    Step("prerequisites", check_prerequisites),
    Step("ip-leases", lease_ips),
    Step("init-hooks", check_hooks),
    # Register further checks (hostname clashes, prerequisites) above this line.
    Step("observe-state", _observe_state),
]

PROVISION_STEPS: Final[list[Step]] = [
    Step("plan", _write_plan),
    Step("clones", _ensure_clones),
    Step("stop-for-recreate", _stop_for_recreate),
    Step("certificate", issue_certificate),
    Step("compose-up", _compose_up, rollback=_compose_down),
    Step("init-hooks", run_hooks),
    Step("hosts-block", write_hosts_block, rollback=remove_hosts_block),
    # Register post-up steps (hosts block, init hooks) below this line.
]


class Provisioner:
    def __init__(
        self,
        config: Config,
        git: Git,
        compose: Compose,
        warn: Warn,
        images: ImageProbe | None = None,
        hosts: HostsWriter | None = None,
        mkcert: Mkcert | None = None,
        host: Host | None = None,
    ) -> None:
        self._config = config
        self._host = host if host is not None else Host()
        self._mkcert = mkcert if mkcert is not None else SubprocessMkcert()
        self._hosts = hosts if hosts is not None else hosts_writer(config)
        self._images = images if images is not None else DockerImageProbe()
        self._git = git
        self._compose = compose
        self._warn = warn
        self._clones = CloneManager(git, config.lock_dir)

    def set_dir(self, set_name: str) -> Path:
        return self._config.sets_root / set_name

    async def up(
        self,
        set_name: str,
        *,
        repos: Sequence[str] | None,
        cwd: Path,
        template: TemplatePlan | None = None,
        recreate: bool = False,
    ) -> UpResult:
        validate_set_name(set_name)
        set_dir = self.set_dir(set_name)
        plan_file = plan_path(set_dir)

        if repos and template is not None:
            raise SandboxError("--from and --repos are mutually exclusive")
        requested = await self._requested_plan(cwd, repos, template)
        on_disk = load_set_plan(set_dir) if plan_file.exists() else None
        if on_disk is not None and requested is not None:
            _refuse_if_different(set_name, plan_file, requested, on_disk)
        plan = on_disk or requested
        if plan is None:
            raise SandboxError(
                f"set {set_name!r} has no plan at {plan_file}; "
                f"create it with: sandbox {set_name} up --repos NAME... "
                f"or sandbox {set_name} up --from TEMPLATE"
            )

        ctx = UpContext(
            config=self._config,
            git=self._git,
            compose=self._compose,
            warn=self._warn,
            clone_manager=self._clones,
            set_name=set_name,
            set_dir=set_dir,
            plan_file=plan_file,
            branch=agent_branch(set_name),
            project=project_name(set_name),
            plan=plan,
            plan_on_disk=on_disk is not None,
            recreate=recreate,
            images=self._images,
            hosts=self._hosts,
            mkcert=self._mkcert,
            host=self._host,
        )
        # Held throughout, so bare `prune` leaves this set's lease alone while it has none of
        # the plan that would show the lease is in use.
        async with file_lock(up_lock_path(self._config, set_name)):
            try:
                await validate(VALIDATION_STEPS, ctx)
                if ctx.action is not UpAction.NOOP:
                    await provision(PROVISION_STEPS, ctx)
                synced = await self._clones.sync_set(set_dir, ctx.plan, ctx.branch)
            except BaseException:
                if ctx.lease_created and not plan_file.exists():
                    # Nothing else knows this set yet: give back what this `up` leased.
                    await shielded(lease_store(self._config).release(set_name))
                raise
        return UpResult(
            set_name,
            set_dir,
            plan_file,
            ctx.plan_created,
            ctx.clones,
            ctx.project,
            ctx.services,
            ctx.action,
            bindings(ctx.set_name, ctx.graph, ctx.lease, extra_hostnames(ctx.web_apps)),
            [s.note() for s in synced if not s.synced],
        )

    async def down(self, set_name: str) -> DownResult:
        """Stop the set's containers and remove its network and volumes, then sync the
        agent's branches into the source repos; the clones are kept."""
        validate_set_name(set_name)
        set_dir = self.set_dir(set_name)
        if not plan_path(set_dir).exists():
            raise SandboxError(f"no set {set_name!r}: {plan_path(set_dir)} does not exist")
        project = project_name(set_name)
        await self._compose.down(project)
        await self._hosts.remove(set_name)
        plan = load_set_plan(set_dir)
        synced = await self._clones.sync_set(set_dir, plan, agent_branch(set_name))
        return DownResult(set_name, set_dir, project, [s.note() for s in synced if not s.synced])

    async def _requested_plan(
        self, cwd: Path, repos: Sequence[str] | None, template: TemplatePlan | None
    ) -> SetPlan | None:
        if repos:
            return await self._plan_from_directory(cwd, repos)
        if template is not None:
            return await self._plan_from_template(template)
        return None

    async def template_from_directory(self, cwd: Path, names: Sequence[str]) -> TemplatePlan:
        """Resolve ``save --repos`` names against ``cwd``: absolute sources, no ``base``."""
        sources = await self._sources_from_directory(cwd, names)
        return TemplatePlan(repos={name: TemplateRepo(source=s) for name, s in sources.items()})

    async def _plan_from_directory(self, cwd: Path, names: Sequence[str]) -> SetPlan:
        """Resolve ``--repos`` names against ``cwd``, pinning absolute sources and bases."""
        sources = await self._sources_from_directory(cwd, names)
        return await self._pin_bases(
            {name: (source, None) for name, source in sources.items()}, self._config.agent
        )

    async def _plan_from_template(self, template: TemplatePlan) -> SetPlan:
        """Pin a template into a set plan; a repo without ``base`` gets its checked-out branch.

        Sources are the template's absolute paths, so the current directory plays no part.
        """
        errors: list[str] = []
        for name, repo in sorted(template.repos.items()):
            problem = await _source_problem(self._git, name, repo.source)
            if problem:
                errors.append(problem)
        if errors:
            raise SandboxError("\n".join(errors))
        # The template's agent override wins over the config's; either is pinned.
        return await self._pin_bases(
            {name: (repo.source, repo.base) for name, repo in template.repos.items()},
            template.agent if template.agent is not None else self._config.agent,
        )

    async def _sources_from_directory(self, cwd: Path, names: Sequence[str]) -> dict[str, Path]:
        errors: list[str] = []
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            errors.append(f"repo(s) named more than once: {', '.join(duplicates)}")
        sources: dict[str, Path] = {}
        for name in dict.fromkeys(names):
            try:
                validate_repo_name(name)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            source = (cwd / name).resolve()
            problem = await _source_problem(self._git, name, source)
            if problem:
                errors.append(problem)
                continue
            sources[name] = source
        if errors:
            raise SandboxError("\n".join(errors))
        return sources

    async def _pin_bases(
        self, repos: dict[str, tuple[Path, str | None]], agent: dict[str, Any] | None
    ) -> SetPlan:
        """Build a set plan, pinning each missing ``base`` to the source's checked-out branch
        and the agent override in effect."""
        errors: list[str] = []
        pinned: dict[str, SetRepo] = {}
        for name, (source, base) in repos.items():
            resolved = base or await self._git.current_branch(source)
            if resolved is None:
                errors.append(
                    f"{name}: {source} has a detached HEAD; check out the branch to base "
                    "the agent's clone on"
                )
                continue
            pinned[name] = SetRepo(source=source, base=resolved)
        if errors:
            raise SandboxError("\n".join(errors))
        return SetPlan(repos=pinned, agent=agent)


def _refuse_if_different(
    set_name: str, plan_file: Path, requested: SetPlan, on_disk: SetPlan
) -> None:
    if requested.sources() == on_disk.sources():
        return
    wanted = ", ".join(f"{n}={s}" for n, s in sorted(requested.sources().items()))
    have = ", ".join(f"{n}={s}" for n, s in sorted(on_disk.sources().items()))
    raise SandboxError(
        f"set {set_name!r} already has a different plan at {plan_file}\n"
        f"  requested: {wanted}\n"
        f"  on disk:   {have}\n"
        "the plan on disk wins: edit it to change the set, or run `up` without --repos or --from"
    )


async def _source_problem(git: Git, name: str, source: Path) -> str | None:
    if not source.is_dir():
        return f"{name}: no repo directory at {source}"
    if not (source / SANDBOX_DIRNAME).is_dir():
        return (
            f"{name}: {source} has no {SANDBOX_DIRNAME}/ directory; "
            "a repo opts in to sandbox by adding one at its root"
        )
    if not await git.is_repository(source):
        return f"{name}: {source} is not the root of a git repository"
    return None
