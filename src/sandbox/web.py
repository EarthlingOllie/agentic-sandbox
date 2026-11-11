"""Web apps: the ``x-sandbox: {web: true}`` service of a repo, served on HTTPS.

A repo marks at most one service as its web app, and that service is named after the repo,
so its URL is predictable: ``https://<repo>.<set>.test``. Validation (``check_web_apps``)
reads each repo's own compose file, refuses anything else, refuses a hostname used twice in
the set, and checks the machine's mkcert CA is there, all before any container starts.
Provisioning (``issue_certificate``) then issues or reuses the set's wildcard certificate
(``sandbox.certs``); ``sandbox.compose.web`` renders the Compose side.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, Final, cast

from sandbox.certs import ensure_certificate, root_ca
from sandbox.compose.dedup import ServiceGraph
from sandbox.compose.network import hostname
from sandbox.compose.services import RepoCompose
from sandbox.compose.web import WebApp
from sandbox.errors import SandboxError
from sandbox.pipeline import UpContext
from sandbox.yamlio import read_yaml

X_SANDBOX: Final = "x-sandbox"


def find_web_apps(ctx: UpContext) -> list[WebApp]:
    """Each repo's web app (repo-name order), or fail with every problem found."""
    errors: list[str] = []
    found: list[WebApp] = []
    for repo in sorted(ctx.repo_composes, key=lambda rc: rc.repo):
        try:
            service = _web_service(repo)
        except SandboxError as exc:
            errors.append(str(exc))
            continue
        if service is not None:
            found.append(WebApp(repo.repo, service, ctx.set_name, ctx.set_dir / repo.repo))
    if errors:
        raise SandboxError("\n".join(errors))
    return found


def _web_service(repo: RepoCompose) -> str | None:
    document = read_yaml(repo.file)
    services = _mapping(_mapping(document).get("services"))
    marked: list[str] = []
    for name, spec in services.items():
        extension = _mapping(spec).get(X_SANDBOX)
        if extension is None:
            continue
        if not isinstance(extension, Mapping):
            raise SandboxError(
                f"{repo.repo}: {repo.file}: services.{name}.{X_SANDBOX}: expected a mapping"
            )
        web = cast(Mapping[str, Any], extension).get("web", False)
        if not isinstance(web, bool):
            raise SandboxError(
                f"{repo.repo}: {repo.file}: services.{name}.{X_SANDBOX}.web: expected true or false"
            )
        if web:
            marked.append(name)
    if not marked:
        return None
    if len(marked) > 1:
        raise SandboxError(
            f"{repo.repo}: {repo.file}: more than one web service ({', '.join(sorted(marked))}); "
            "a repo has at most one, named after the repo"
        )
    (service,) = marked
    if service != repo.repo:
        raise SandboxError(
            f"{repo.repo}: {repo.file}: web service {service!r} must be named after the repo "
            f"({repo.repo!r}), so it is served at https://{repo.repo}.<set>.test"
        )
    if "network_mode" in _mapping(services[service]):
        raise SandboxError(
            f"{repo.repo}: {repo.file}: web service {service!r} cannot set network_mode: "
            "it needs its own IP on the set network"
        )
    return service


def hostname_claims(
    set_name: str, graph: ServiceGraph, web_apps: Sequence[WebApp]
) -> dict[str, list[str]]:
    """Every hostname in the set -> what claims it (a web app, or a service and its repos)."""
    claims: dict[str, list[str]] = defaultdict(list)
    apps = {app.service: app for app in web_apps}
    for app in web_apps:
        claims[app.hostname].append(f"web app of repo {app.repo}")
    for name, service in graph.services.items():
        app = apps.get(name)
        others = [repo for repo in service.repos if app is None or repo != app.repo]
        if others:
            claims[hostname(name, set_name)].append(f"service {name!r} of repo {', '.join(others)}")
    return dict(claims)


def check_hostnames(set_name: str, claims: Mapping[str, Sequence[str]]) -> None:
    clashes = {name: owners for name, owners in sorted(claims.items()) if len(owners) > 1}
    if clashes:
        raise SandboxError(
            "\n".join(
                f"hostname {name} is used more than once in set {set_name!r}: " + "; ".join(owners)
                for name, owners in clashes.items()
            )
        )


# --- up steps --------------------------------------------------------------------------


async def check_web_apps(ctx: UpContext) -> None:
    """Validation: web apps well-formed, hostnames unique, and the mkcert CA present."""
    ctx.web_apps = find_web_apps(ctx)
    check_hostnames(ctx.set_name, hostname_claims(ctx.set_name, ctx.graph, ctx.web_apps))
    if ctx.web_apps:
        ctx.root_ca = await root_ca(ctx.mkcert)


async def issue_certificate(ctx: UpContext) -> None:
    """Provision: the set's ``*.<set>.test`` leaf, reused while valid. Kept on rollback."""
    if not ctx.web_apps or ctx.root_ca is None:
        return
    ctx.certificate = await ensure_certificate(
        ctx.mkcert,
        ctx.root_ca,
        ctx.set_dir,
        ctx.set_name,
        [app.hostname for app in ctx.web_apps],
    )


def _mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}
