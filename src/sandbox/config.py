"""Global configuration: ``~/.config/sandbox/config.yaml``.

Every host-state location sandbox touches is a field here, so tests (and unusual setups)
can point the whole tool at a scratch directory by writing one config file and setting
``SANDBOX_CONFIG`` to its path.
"""

import os
from pathlib import Path
from typing import Annotated, Final

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from sandbox.compose.agent import AgentOverride
from sandbox.errors import SandboxError
from sandbox.names import DEFAULT_DOMAIN, validate_domain
from sandbox.yamlio import read_yaml, validate_file

CONFIG_ENV_VAR: Final = "SANDBOX_CONFIG"


def config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) if xdg else Path.home() / ".config"


def default_config_path() -> Path:
    return config_home() / "sandbox" / "config.yaml"


def _expand_absolute(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise ValueError("must be an absolute path (or start with ~)")
    return expanded


HostPath = Annotated[Path, AfterValidator(_expand_absolute)]


def _default_templates_root() -> Path:
    return config_home() / "sandbox" / "plans"


def _default_sets_root() -> Path:
    return Path.home() / "sandbox" / "sets"


def _default_state_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "state") / "sandbox"


class Config(BaseModel):
    """Validated global configuration. Unknown keys are rejected so typos fail fast."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    templates_root: HostPath = Field(default_factory=_default_templates_root)
    sets_root: HostPath = Field(default_factory=_default_sets_root)
    state_dir: HostPath = Field(
        default_factory=_default_state_dir,
        description="Lock files and other cross-process host state.",
    )
    domain: Annotated[str, AfterValidator(validate_domain)] = Field(
        default=DEFAULT_DOMAIN,
        strict=True,
        description="The DNS suffix every set hostname ends in: `<name>.<set>.<domain>`. "
        "One or more labels; `local` (mDNS) is refused.",
    )
    agent: AgentOverride | None = Field(
        default=None,
        description="Default agent override: a Compose fragment (services, volumes, "
        "networks) merged over the default agent; pinned into each new set's plan.",
    )

    @property
    def lock_dir(self) -> Path:
        return self.state_dir / "locks"


# Keys an earlier version read. Named one by one, so an upgraded config says what to delete
# instead of failing as a typo would.
REMOVED_KEYS: Final = {
    "ip_pool": "sets no longer lease loopback addresses",
    "hosts_file": "set hostnames are answered by the resolver, not written to a hosts file",
}


def _refuse_removed_keys(data: object, source: Path) -> None:
    found = [key for key in REMOVED_KEYS if isinstance(data, dict) and key in data]
    if found:
        lines = [f"{source}: {len(found)} validation error(s)"]
        lines += [
            f"  {key}: no longer used ({REMOVED_KEYS[key]}); remove `{key}` from this file"
            for key in found
        ]
        raise SandboxError("\n".join(lines))


def load_config(path: Path | None = None) -> Config:
    """Load the config from ``path``, ``$SANDBOX_CONFIG`` or the default location.

    A missing file at the default location means "all defaults"; a missing file that was
    named explicitly is an error.
    """
    explicit = path is not None or bool(os.environ.get(CONFIG_ENV_VAR))
    resolved = path or Path(os.environ.get(CONFIG_ENV_VAR) or default_config_path())
    if not resolved.exists():
        if explicit:
            raise SandboxError(f"config file {resolved} does not exist")
        return Config()
    data = read_yaml(resolved)
    _refuse_removed_keys(data, resolved)
    return validate_file(Config, {} if data is None else data, resolved)
