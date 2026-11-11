"""The plan schema shared by set plans (``<set>/sandbox.yaml``) and templates.

A set plan pins everything needed to recreate the set: each repo's absolute ``source`` and
the ``base`` branch the agent's clone was cut from. Templates share the schema but leave ``base``
optional so they stay reusable. The set's name is its directory name and is never stored in
the plan (unknown keys such as ``name`` are rejected).
"""

from pathlib import Path
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from sandbox.compose.agent import AgentOverride
from sandbox.errors import SandboxError
from sandbox.names import PLAN_FILENAME, validate_repo_name
from sandbox.yamlio import dump_yaml, read_yaml, validate_file


def _require_absolute(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("must be an absolute path")
    return path


AbsolutePath = Annotated[Path, AfterValidator(_require_absolute)]
RepoName = Annotated[str, AfterValidator(validate_repo_name)]
BranchName = Annotated[str, Field(min_length=1)]


class RepoSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: AbsolutePath


class TemplateRepo(RepoSpec):
    base: BranchName | None = None


class SetRepo(RepoSpec):
    base: BranchName


class Plan[R: RepoSpec](BaseModel):
    """Repos keyed by name (the clone's directory in the set), plus an optional agent override."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repos: dict[RepoName, R] = Field(min_length=1)
    # A Compose fragment merged over the default agent (see sandbox.compose.agent). A set
    # plan pins the one it was created with: its own, else the template's, else the config's.
    agent: AgentOverride | None = None

    def sources(self) -> dict[str, Path]:
        return {name: repo.source for name, repo in self.repos.items()}

    def to_yaml(self) -> str:
        return dump_yaml(self.model_dump(mode="json", exclude_none=True))


class TemplatePlan(Plan[TemplateRepo]):
    pass


class SetPlan(Plan[SetRepo]):
    pass


def plan_path(set_dir: Path) -> Path:
    return set_dir / PLAN_FILENAME


def load_set_plan(set_dir: Path) -> SetPlan:
    path = plan_path(set_dir)
    data = read_yaml(path)
    return validate_file(SetPlan, {} if data is None else data, path)


def write_new_plan(path: Path, plan: Plan[Any]) -> None:
    """Write ``plan`` to ``path``; never overwrites an existing file."""
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(plan.to_yaml())
    except FileExistsError as exc:
        raise SandboxError(f"refusing to overwrite existing plan {path}") from exc
