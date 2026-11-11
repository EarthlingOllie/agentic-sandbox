"""Global configuration: ``~/.config/sandbox/config.yaml``.

Every host-state location sandbox touches is a field here, so tests (and unusual setups)
can point the whole tool at a scratch directory by writing one config file and setting
``SANDBOX_CONFIG`` to its path.
"""

import os
from ipaddress import IPv4Network
from pathlib import Path
from typing import Annotated, Final

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from sandbox.compose.agent import AgentOverride
from sandbox.errors import SandboxError
from sandbox.yamlio import read_yaml, validate_file

CONFIG_ENV_VAR: Final = "SANDBOX_CONFIG"
LOOPBACK: Final = IPv4Network("127.0.0.0/8")
DEFAULT_IP_POOL: Final = IPv4Network("127.42.0.0/16")


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

    ip_pool: IPv4Network = Field(
        default=DEFAULT_IP_POOL,
        description="Loopback range sets lease /24 blocks from.",
    )
    templates_root: HostPath = Field(default_factory=_default_templates_root)
    sets_root: HostPath = Field(default_factory=_default_sets_root)
    state_dir: HostPath = Field(
        default_factory=_default_state_dir,
        description="Lock files, IP leases and other cross-process host state.",
    )
    hosts_file: HostPath | None = Field(
        default=None,
        description="Unset (the default): each set's hosts block goes into /etc/hosts through "
        "`sudo -n sandbox-hosts`. A path: the block is written to that file directly, without "
        "sudo (tests and unprivileged setups).",
    )
    agent: AgentOverride | None = Field(
        default=None,
        description="Default agent override: a Compose fragment (services, volumes, "
        "networks) merged over the default agent; pinned into each new set's plan.",
    )

    @field_validator("ip_pool")
    @classmethod
    def _pool_is_loopback(cls, pool: IPv4Network) -> IPv4Network:
        if not pool.subnet_of(LOOPBACK):
            raise ValueError(f"must be inside {LOOPBACK}")
        if pool.prefixlen > 24:
            raise ValueError("must be a /24 or larger (each set leases one /24)")
        return pool

    @property
    def lock_dir(self) -> Path:
        return self.state_dir / "locks"


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
    return validate_file(Config, {} if data is None else data, resolved)
