"""In-memory stand-in for the mkcert CLI (``sandbox.certs.Mkcert``), for unit tests.

Its CA is ``tests/fixtures/certs/caroot`` (copied, so a test can remove ``rootCA.pem``), and
issuing copies a fixture leaf for ``*.jira-123.test`` into place: real certificates, so
sandbox's own validity check runs unchanged.
"""

import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sandbox.errors import SandboxError

FIXTURES = Path(__file__).parent / "fixtures" / "certs"


@dataclass
class FakeMkcert:
    root: Path
    # Which fixture leaf `issue` hands out (see tests/fixtures/certs/README.md).
    leaf: str = "jira-123"
    issued: list[tuple[Path, Path, list[str]]] = field(
        default_factory=list[tuple[Path, Path, list[str]]]
    )

    def __post_init__(self) -> None:
        if not self.ca_dir.exists():
            shutil.copytree(FIXTURES / "caroot", self.ca_dir)

    @property
    def ca_dir(self) -> Path:
        return self.root / "caroot"

    async def caroot(self) -> Path:
        return self.ca_dir

    async def issue(self, cert_file: Path, key_file: Path, names: Sequence[str]) -> None:
        self.issued.append((cert_file, key_file, list(names)))
        if list(names) != ["*.jira-123.test"]:
            raise SandboxError(f"FakeMkcert has no fixture certificate for {names}")
        shutil.copyfile(FIXTURES / f"{self.leaf}.pem", cert_file)
        shutil.copyfile(FIXTURES / f"{self.leaf}-key.pem", key_file)
        key_file.chmod(0o600)  # as mkcert writes it
