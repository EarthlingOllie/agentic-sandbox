"""The docs and the CI workflows describe the addressing model that exists.

Read as a contributor reads them: the files in the repository, nothing imported from them.
Set hostnames are answered by the resolver at container addresses (ADR 0001); the loopback
model before it (leased loopback IPs, a hosts-file helper, a macOS alias pool) is gone, and
nothing a reader or a workflow follows may still present it as how sandbox works.
"""

import re
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from sandbox.compose.resolver import RESOLVER_ADDRESS, RESOLVER_PORT
from sandbox.config import Config
from sandbox.resolver_config import RESOLVED_DROP_IN, macos_resolver_file

ROOT = Path(__file__).parent.parent.parent
README = ROOT / "README.md"
CHECKLIST = ROOT / "docs" / "release-checklist-macos.md"
CI = ROOT / ".github" / "workflows" / "ci.yml"
E2E = ROOT / ".github" / "workflows" / "e2e.yml"
MVP_SPEC = ROOT / ".scratch" / "sandbox-mvp" / "spec.md"
RESOLVER_SPEC = ROOT / ".scratch" / "resolver-addressing" / "spec.md"
ADR = ROOT / "docs" / "adr" / "0001-set-hostnames-resolve-to-container-addresses.md"
GLOSSARY = ROOT / "CONTEXT.md"

# What only the loopback model had: its address pool, its leases, its passwordless helper.
LOOPBACK_MODEL = re.compile(r"127\.42\.|\bleas(e|es|ed|ing)\b|NOPASSWD|hosts block", re.IGNORECASE)
# Its config keys, its setup flag and what it installed on a machine. A doc may name these
# only to say they are removed.
LOOPBACK_INSTALLS = re.compile(
    r"ip_pool|hosts_file|--sets\b|sandbox-hosts|sudoers|lo0|launchd|loopback-aliases|/etc/hosts"
)
REMOVAL = re.compile(r"remov|no longer|earlier version|left over|leftover|gone", re.IGNORECASE)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _paragraphs(text: str) -> list[str]:
    return [block for block in re.split(r"\n\s*\n", text) if block.strip()]


def _section(text: str, heading: str) -> str:
    """The body of the ``## heading`` section, up to the next heading of that level."""
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    assert match is not None, f"no `## {heading}` section"
    return match.group(1)


def _anchors(text: str) -> set[str]:
    """The anchors GitHub gives the headings of a Markdown file."""
    found: set[str] = set()
    for title in re.findall(r"^#{1,6} (.+)$", text, re.M):
        kept = re.sub(r"[^\w\- ]", "", title.strip().lower())
        found.add(kept.replace(" ", "-"))
    return found


def _workflow(path: Path) -> dict[Any, Any]:
    return cast(dict[Any, Any], yaml.safe_load(_read(path)))


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # YAML 1.1 reads the bare key `on` as the boolean true.
    found: object = workflow.get("on", workflow.get(True))
    assert isinstance(found, dict), "the workflow has no `on:` mapping"
    return cast(dict[str, Any], found)


def _commands(job: dict[str, Any]) -> list[str]:
    return [str(step["run"]) for step in cast(list[dict[str, Any]], job["steps"]) if "run" in step]


@pytest.mark.parametrize("path", [README, CHECKLIST, CI, E2E], ids=lambda p: p.name)
def test_nothing_a_reader_or_a_workflow_follows_describes_the_loopback_model(path: Path) -> None:
    for paragraph in _paragraphs(_read(path)):
        assert not LOOPBACK_MODEL.search(paragraph), paragraph
        if LOOPBACK_INSTALLS.search(paragraph):
            assert REMOVAL.search(paragraph), (
                f"names something the loopback model installed without saying it is removed:\n"
                f"{paragraph}"
            )


def test_the_readme_has_no_hosts_file_section_and_every_link_in_it_leads_somewhere() -> None:
    text = _read(README)
    anchors = _anchors(text)

    assert "hosts-file" not in anchors
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if target.startswith(("http://", "https://")):
            continue
        path, _, anchor = target.partition("#")
        if path:
            assert (ROOT / path).exists(), f"README links to {path}, which does not exist"
        elif anchor:
            assert anchor in anchors, f"README links to #{anchor}, which is no heading of it"


def test_the_readme_configuration_example_is_a_config_sandbox_accepts_with_every_key() -> None:
    blocks = re.findall(r"```yaml\n(.*?)```", _section(_read(README), "Configuration"), re.S)
    assert blocks, "the Configuration section has no YAML example"
    example = cast(dict[str, Any], yaml.safe_load(blocks[0]))

    Config.model_validate(example)
    assert set(example) == set(Config.model_fields)


def test_the_readme_says_where_setup_points_the_machine() -> None:
    setup = _section(_read(README), "Setup")

    assert str(RESOLVED_DROP_IN) in setup
    assert str(macos_resolver_file("<domain>")) in setup
    assert f"{RESOLVER_ADDRESS}" in setup
    assert str(RESOLVER_PORT) in setup
    assert "--domain" in setup
    assert "--uninstall" in setup


def test_the_readme_says_what_the_machine_can_reach_and_which_hosts_are_supported() -> None:
    addressing = " ".join(_section(_read(README), "Addressing").split())

    assert "every port a container listens on" in addressing
    assert "agent" in addressing
    assert "OrbStack" in addressing
    assert "Docker Desktop for Mac is not supported" in addressing
    assert "systemd-resolved" in addressing


@pytest.mark.parametrize("section", ["Status", "URLs", "Prune", "Development"])
def test_the_readme_sections_on_inspecting_and_clearing_sets_speak_of_the_resolver_model(
    section: str,
) -> None:
    body = _section(_read(README), section)

    assert not re.search(r"127\.\d+\.\d+\.\d+", body.replace(RESOLVER_ADDRESS, ""))
    assert not re.search(r"\bloopback IP\b|hosts block", body)


def test_the_mvp_spec_carries_a_revision_note_pointing_at_the_resolver_spec_and_adr() -> None:
    text = _read(MVP_SPEC)
    head = text[: text.index("## Problem Statement")]

    assert "Revision" in head
    for target in (RESOLVER_SPEC, ADR):
        assert target.exists()
        assert target.name in head, f"the revision note does not name {target.name}"
    # The stories and decisions the resolver spec says it supersedes, named where a reader
    # of the MVP spec starts.
    superseded = re.search(
        r"Supersedes, in the MVP spec: stories ([\d, -]+),", _read(RESOLVER_SPEC)
    )
    assert superseded is not None
    assert superseded.group(1).strip() in head


def test_the_macos_checklist_is_built_around_running_the_harness_with_orbstack() -> None:
    text = _read(CHECKLIST)

    assert "OrbStack" in text
    assert "SANDBOX_E2E=1" in text
    assert "pytest -m e2e tests/e2e" in text
    assert "Docker Desktop is running" not in text


def test_the_nightly_workflow_runs_the_harness_on_the_linux_runner_and_prints_its_report() -> None:
    workflow = _workflow(E2E)
    triggers = _triggers(workflow)
    (job,) = cast(dict[str, dict[str, Any]], workflow["jobs"]).values()
    commands = _commands(job)

    assert triggers["schedule"], "not nightly"
    assert "workflow_dispatch" in triggers, "not on demand"
    assert triggers["push"] == {"tags": ["v*"]}, "not for release tags only"
    assert str(job["runs-on"]).startswith("ubuntu-")
    harness = [c for c in commands if "pytest -m e2e tests/e2e" in c]
    assert len(harness) == 1
    assert "SANDBOX_E2E=1" in harness[0]
    assert "SANDBOX_E2E_KEEP_SETUP" not in harness[0], "the runner is a throwaway machine"
    # The report reaches the run's summary page whether the harness passed or failed.
    report = [
        step
        for step in cast(list[dict[str, Any]], job["steps"])
        if "GITHUB_STEP_SUMMARY" in str(step.get("run", ""))
    ]
    assert report, "no step puts the report in the run summary"
    assert all(step.get("if") == "always()" for step in report)


def test_the_per_push_workflow_still_runs_unit_and_integration_tests() -> None:
    workflow = _workflow(CI)
    jobs = cast(dict[str, dict[str, Any]], workflow["jobs"])
    commands = [command for job in jobs.values() for command in _commands(job)]

    assert "push" in _triggers(workflow)
    assert 'uv run pytest -m "not integration"' in commands
    assert "uv run pytest -m integration" in commands
    assert set(jobs["unit"]["strategy"]["matrix"]["os"]) == {"ubuntu-latest", "macos-latest"}
    assert not any("SANDBOX_E2E" in command for command in commands)


def test_the_glossary_and_the_adr_name_the_terms_the_readme_uses_for_addressing() -> None:
    glossary, adr = _read(GLOSSARY), _read(ADR)

    for term in ("Domain", "Set hostname", "Conventional port", "Resolver", "Supported host"):
        assert f"**{term}**" in glossary
    assert "status: accepted" in adr
    assert "OrbStack" in adr
    assert "Docker Desktop for Mac is unsupported" in adr
