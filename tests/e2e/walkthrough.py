"""The walkthrough's bookkeeping: checks, phases, the ordered report, and a teardown that
always runs.

A *check* is one promise of one command: the command line, what was checked, and whether it
held. A failed check is recorded, with the reason and the command's output, and the
walkthrough carries on. A *phase* is a group of checks that stand or fall together as a
prerequisite: a phase that ``needs`` a phase with a failed check is not run (its checks
would only repeat the first failure) and is reported as not run. Whatever happens, an
interrupt included, the teardown runs, and its checks are reported like any other.

Nothing here knows about sandbox or a machine; ``tests/unit/test_walkthrough_report.py``
checks it without either.
"""

import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

Outcome = Literal["PASS", "FAIL", "SKIP"]
TEARDOWN: Final = "teardown"
_INDENT: Final = " " * 10


class Stop(Exception):
    """Raised inside a phase to end it: what it would check next depends on what failed."""


@dataclass(frozen=True)
class Entry:
    number: int
    outcome: Outcome
    command: str
    checked: str
    # Why it failed, then the command's output; empty unless it failed.
    detail: str = ""


@dataclass(frozen=True)
class Phase:
    name: str
    body: Callable[[], object]
    # Phases that must have passed for this one to mean anything.
    needs: tuple[str, ...] = ()


@dataclass
class Report:
    entries: list[Entry] = field(default_factory=list[Entry])

    def count(self, outcome: Outcome) -> int:
        return sum(1 for entry in self.entries if entry.outcome == outcome)

    @property
    def ok(self) -> bool:
        """Something was checked and all of it held."""
        return self.count("PASS") == len(self.entries) > 0

    def summary(self) -> str:
        return (
            f"{len(self.entries)} checks: {self.count('PASS')} passed, "
            f"{self.count('FAIL')} failed, {self.count('SKIP')} not run"
        )

    def render(self) -> str:
        """The report: one block per check, in the order they ran."""
        lines = [f"sandbox walkthrough: {self.summary()}", ""]
        for entry in self.entries:
            lines.append(f"{entry.number:>3} {entry.outcome}  {entry.command}")
            lines.append(f"{_INDENT}{entry.checked}")
            lines.extend(f"{_INDENT}| {line}" for line in entry.detail.splitlines())
        return "\n".join(lines) + "\n"


class Walkthrough:
    def __init__(self) -> None:
        self.report = Report()

    def record(self, outcome: Outcome, command: str, checked: str, detail: str = "") -> None:
        number = len(self.report.entries) + 1
        self.report.entries.append(Entry(number, outcome, command, checked, detail))

    def check(
        self, command: str, checked: str, holds: Callable[[], object], output: str = ""
    ) -> bool:
        """Record whether ``holds`` returns without raising; ``output`` is the command's,
        shown only when it does not."""
        try:
            holds()
        except Exception as exc:  # any failure of a check is a failed check
            reason = f"{type(exc).__name__}: {exc}".rstrip()
            shown = f"--- output of `{command}` ---\n{output.rstrip()}" if output.strip() else ""
            self.record("FAIL", command, checked, "\n".join(filter(None, [reason, shown])))
            return False
        self.record("PASS", command, checked)
        return True

    def run(
        self,
        phases: Sequence[Phase],
        teardown: Callable[[], object],
        given: Sequence[str] = (),
    ) -> Report:
        """Run ``phases`` in order, then ``teardown``. ``given`` names phases that are not
        run because what they establish is already there (an existing setup)."""
        passed = set(given)
        try:
            for phase in phases:
                if phase.name in given:
                    continue
                missing = [need for need in phase.needs if need not in passed]
                if missing:
                    self.record(
                        "SKIP",
                        f"phase: {phase.name}",
                        f"not run: it needs {', '.join(missing)}, which did not pass",
                    )
                elif self._run(f"phase: {phase.name}", phase.body):
                    passed.add(phase.name)
        finally:
            self._run(TEARDOWN, teardown)
        return self.report

    def _run(self, label: str, body: Callable[[], object]) -> bool:
        """Whether ``body`` ran with no failed check and no crash."""
        failures = self.report.count("FAIL")
        try:
            body()
        except Stop:
            pass
        except Exception:  # a crash is reported, and the walkthrough goes on
            self.record("FAIL", label, "runs to its end", traceback.format_exc())
        return self.report.count("FAIL") == failures
