"""Guard: integration tests never hard-code a set name that reaches the shared Docker host.

Two worktrees running the integration suite at once share one Docker daemon; a fixed set
name means a fixed Compose project, so one run's teardown removes the other's containers.
Docker-facing set names come from ``tests.integration.conftest.unique_set``.
"""

import re
from pathlib import Path

INTEGRATION = Path(__file__).parent.parent / "integration"
# A string literal naming an `it-...` set, or a `sandbox-...` Compose project, by hand.
HARD_CODED = re.compile(r"""f?["'](?:it-|sandbox-(?!agent))""")


def test_integration_tests_derive_set_names_from_the_run_id() -> None:
    offenders = [
        f"{path.name}:{number}: {line.strip()}"
        for path in sorted(INTEGRATION.glob("test_*.py"))
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if HARD_CODED.search(line)
    ]
    assert offenders == [], "use unique_set()/project_name() instead:\n" + "\n".join(offenders)
