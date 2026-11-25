"""The walkthrough harness's own promises: an ordered report, checks that carry on after a
failure, and a teardown that always runs. The harness itself (``tests/e2e``) needs a real
machine; what it does with passes and failures is checked here, with no machine at all."""

import pytest

from tests.e2e.walkthrough import Phase, Stop, Walkthrough


def passing() -> None:
    pass


def failing() -> None:
    raise AssertionError("expected 0, got 1")


def test_the_report_lists_every_check_in_order_with_its_command_and_what_was_checked() -> None:
    walk = Walkthrough()

    def body() -> None:
        walk.check("sandbox a up --repos api", "exits 0", passing)
        walk.check("sandbox a up --repos api", "the clone of api is on agent/a", passing)
        walk.check("sandbox a status --json", "the set is running", passing)

    report = walk.run([Phase("up", body)], teardown=passing)

    assert report.ok
    assert [(e.number, e.outcome, e.command, e.checked) for e in report.entries] == [
        (1, "PASS", "sandbox a up --repos api", "exits 0"),
        (2, "PASS", "sandbox a up --repos api", "the clone of api is on agent/a"),
        (3, "PASS", "sandbox a status --json", "the set is running"),
    ]
    rendered = report.render()
    assert "3 checks: 3 passed, 0 failed, 0 not run" in rendered
    lines = rendered.splitlines()
    first = next(i for i, line in enumerate(lines) if "sandbox a up --repos api" in line)
    assert "PASS" in lines[first]
    assert "exits 0" in lines[first + 1]
    assert rendered.index("the clone of api") < rendered.index("the set is running")


def test_a_failed_check_is_reported_with_its_reason_and_the_commands_output() -> None:
    walk = Walkthrough()

    def body() -> None:
        walk.check("sandbox a down", "exits 0", failing, output="error: docker said no\n")
        walk.check("sandbox a status", "exits 0", passing, output="set a: stopped\n")

    report = walk.run([Phase("down", body)], teardown=passing)

    assert not report.ok
    assert [e.outcome for e in report.entries] == ["FAIL", "PASS"]
    rendered = report.render()
    assert "2 checks: 1 passed, 1 failed, 0 not run" in rendered
    assert "expected 0, got 1" in rendered
    assert "error: docker said no" in rendered
    # Only what failed brings its output along.
    assert "set a: stopped" not in rendered


def test_a_failed_check_stops_neither_the_rest_of_its_phase_nor_an_unrelated_phase() -> None:
    walk = Walkthrough()
    ran: list[str] = []

    def first() -> None:
        walk.check("sandbox a urls", "lists the web app", failing)
        walk.check("sandbox a urls --json", "is JSON", lambda: ran.append("same phase"))

    def second() -> None:
        walk.check("sandbox templates", "exits 0", lambda: ran.append("next phase"))

    report = walk.run([Phase("urls", first), Phase("templates", second)], teardown=passing)

    assert ran == ["same phase", "next phase"]
    assert [e.outcome for e in report.entries] == ["FAIL", "PASS", "PASS"]


def test_a_phase_that_needs_a_failed_phase_is_not_run_and_says_why() -> None:
    walk = Walkthrough()
    ran: list[str] = []

    def up() -> None:
        walk.check("sandbox a up --repos api", "exits 0", failing)

    phases = [
        Phase("up", up),
        Phase("sh", lambda: ran.append("sh"), needs=("up",)),
        Phase("destroy", lambda: ran.append("destroy"), needs=("sh",)),
        Phase("templates", lambda: ran.append("templates")),
    ]
    report = walk.run(phases, teardown=passing)

    assert ran == ["templates"]
    assert not report.ok
    skipped = [e for e in report.entries if e.outcome == "SKIP"]
    assert [e.command for e in skipped] == ["phase: sh", "phase: destroy"]
    assert "up" in skipped[0].checked
    assert "sh" in skipped[1].checked
    assert "1 failed, 2 not run" in report.render()


def test_a_phase_can_stop_itself_without_stopping_the_walkthrough() -> None:
    walk = Walkthrough()
    ran: list[str] = []

    def up() -> None:
        if not walk.check("sandbox a up", "exits 0", failing):
            raise Stop
        ran.append("rest of up")

    report = walk.run(
        [Phase("up", up), Phase("templates", lambda: ran.append("templates"))], teardown=passing
    )

    assert ran == ["templates"]
    assert [e.outcome for e in report.entries] == ["FAIL"]


def test_a_phase_that_crashes_is_a_failure_and_the_walkthrough_carries_on() -> None:
    walk = Walkthrough()
    ran: list[str] = []

    def crash() -> None:
        raise RuntimeError("docker vanished")

    phases = [
        Phase("status", crash),
        Phase("after", lambda: ran.append("after"), needs=("status",)),
        Phase("templates", lambda: ran.append("templates")),
    ]
    report = walk.run(phases, teardown=passing)

    assert ran == ["templates"]
    assert [e.outcome for e in report.entries] == ["FAIL", "SKIP"]
    assert report.entries[0].command == "phase: status"
    assert "docker vanished" in report.render()


def test_a_phase_given_as_already_done_is_not_run_and_satisfies_those_that_need_it() -> None:
    walk = Walkthrough()
    ran: list[str] = []

    phases = [
        Phase("setup", lambda: ran.append("setup")),
        Phase("up", lambda: walk.check("sandbox a up", "exits 0", passing), needs=("setup",)),
    ]
    report = walk.run(phases, teardown=passing, given=("setup",))

    assert ran == []
    assert report.ok
    assert [e.command for e in report.entries] == ["sandbox a up"]


@pytest.mark.parametrize("problem", [failing, lambda: (_ for _ in ()).throw(RuntimeError("x"))])
def test_teardown_runs_after_failures_and_its_own_checks_are_reported(problem: object) -> None:
    walk = Walkthrough()
    torn_down: list[str] = []

    def teardown() -> None:
        torn_down.append("yes")
        walk.check("sandbox a destroy --force", "exits 0", failing)

    def body() -> None:
        assert callable(problem)
        problem()

    report = walk.run([Phase("up", body)], teardown=teardown)

    assert torn_down == ["yes"]
    assert report.entries[-1].command == "sandbox a destroy --force"
    assert report.entries[-1].outcome == "FAIL"


def test_teardown_runs_when_the_walkthrough_is_interrupted() -> None:
    walk = Walkthrough()
    torn_down: list[str] = []

    def interrupted() -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        walk.run([Phase("up", interrupted)], teardown=lambda: torn_down.append("yes"))

    assert torn_down == ["yes"]


def test_a_teardown_that_crashes_is_reported_not_raised() -> None:
    walk = Walkthrough()

    def teardown() -> None:
        raise RuntimeError("could not remove the set")

    report = walk.run([Phase("up", passing)], teardown=teardown)

    assert not report.ok
    assert report.entries[-1].command == "teardown"
    assert "could not remove the set" in report.render()


def test_a_walkthrough_that_checked_nothing_did_not_pass() -> None:
    assert not Walkthrough().run([], teardown=passing).ok
