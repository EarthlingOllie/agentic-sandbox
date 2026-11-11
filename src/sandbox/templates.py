"""Templates: reusable plans stored as ``<templates_root>/<NAME>.yaml``.

A template shares the plan schema with set plans (:class:`sandbox.plan.TemplatePlan`) but
``base`` is optional, so one template can seed many sets. There is no template inheritance:
a template is a complete plan on its own.
"""

import re
from pathlib import Path
from typing import Final

from sandbox.errors import SandboxError
from sandbox.plan import TemplatePlan, write_new_plan
from sandbox.yamlio import read_yaml, validate_file

TEMPLATE_SUFFIX: Final = ".yaml"
TEMPLATE_NAME_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


def validate_template_name(name: str) -> str:
    if not TEMPLATE_NAME_PATTERN.fullmatch(name) or name.endswith(TEMPLATE_SUFFIX):
        raise SandboxError(
            f"invalid template name {name!r}: use letters, digits, '.', '_' and '-' "
            f"(1-63 characters, starting with a letter or digit, without {TEMPLATE_SUFFIX})"
        )
    return name


class TemplateStore:
    """The templates root: list, load and save templates."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, name: str) -> Path:
        return self.root / f"{validate_template_name(name)}{TEMPLATE_SUFFIX}"

    def names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            path.stem
            for path in self.root.iterdir()
            if path.suffix == TEMPLATE_SUFFIX
            and path.is_file()
            and TEMPLATE_NAME_PATTERN.fullmatch(path.stem)
        )

    def load(self, name: str) -> TemplatePlan:
        path = self.path(name)
        if not path.is_file():
            available = ", ".join(self.names()) or "none"
            raise SandboxError(
                f"no template {name!r} at {path} (available: {available}); "
                "list them with: sandbox templates"
            )
        data = read_yaml(path)
        return validate_file(TemplatePlan, {} if data is None else data, path)

    def save(self, name: str, plan: TemplatePlan) -> Path:
        """Write ``plan`` as template ``name``; an existing template is never overwritten."""
        path = self.path(name)
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            write_new_plan(path, plan)
        except SandboxError as exc:
            raise SandboxError(
                f"template {name!r} already exists at {path}; delete it or pick another name"
            ) from exc
        return path
