"""`runner.Runner` — re-running a daily check through launchd, and how it fails.

The action is the only write on the read-only surface, so these tests are about
its fences: the label table is fixed, the cooldown bounds the rate, and every
failure to reach launchd is a *reason* in the result rather than a raised
exception. No test shells out — `_kickstart` is driven with a patched `shutil`
and `subprocess`, and the `Runner` with an injected command and clock.
"""

from netwatch_dash import runner
from netwatch_dash.runner import CHECKS, Runner


def test_the_check_table_is_the_two_daily_bandwidth_jobs():
    assert CHECKS == {
        "speed": "com.nickbrett.netwatch.speed",
        "lan-speed": "com.nickbrett.netwatch.lan-speed",
    }


def test_an_unknown_check_is_refused_without_asking_launchctl():
    calls = []
    r = Runner(run=lambda label: (calls.append(label) or True, "ok"))

    result = r.rerun("probe")

    assert result["started"] is False
    assert "no such check" in result["reason"]
    assert calls == []


def test_a_started_run_records_the_label_and_starts_the_cooldown():
    calls = []
    r = Runner(run=lambda label: (calls.append(label) or True, "ran"))

    result = r.rerun("speed")

    assert result["started"] is True
    assert result["label"] == CHECKS["speed"]
    assert calls == [CHECKS["speed"]]
    assert r.cooldown_remaining("speed") > 0


def test_a_second_run_within_the_cooldown_is_refused_with_a_retry_hint():
    now = [0.0]
    calls = []
    r = Runner(
        cooldown_s=120.0,
        run=lambda label: (calls.append(label) or True, "ran"),
        clock=lambda: now[0],
    )
    r.rerun("speed")

    now[0] = 30.0
    result = r.rerun("speed")

    assert result["started"] is False
    assert 0 < result["retry_after_s"] <= 90.0
    assert calls == [CHECKS["speed"]]


def test_the_cooldown_expires_and_the_next_run_is_allowed():
    now = [0.0]
    calls = []
    r = Runner(
        cooldown_s=120.0,
        run=lambda label: (calls.append(label) or True, "ran"),
        clock=lambda: now[0],
    )
    r.rerun("speed")

    now[0] = 121.0

    assert r.rerun("speed")["started"] is True
    assert calls == [CHECKS["speed"], CHECKS["speed"]]


def test_a_refused_kickstart_keeps_its_reason_and_detail():
    r = Runner(run=lambda label: (False, "Could not find service"))

    result = r.rerun("lan-speed")

    assert result["started"] is False
    assert result["reason"] == "Could not find service"
    assert result["detail"] == "Could not find service"


def test_kickstart_says_so_when_launchctl_is_absent(monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda _: None)

    started, detail = runner._kickstart("com.nickbrett.netwatch.speed")

    assert started is False
    assert "launchctl is not on this host" in detail


def test_a_nonzero_kickstart_reports_its_stderr(monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda _: "/bin/launchctl")

    class Proc:
        returncode = 3
        stdout = ""
        stderr = 'Could not find service "com.nickbrett.netwatch.speed"\n'

    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: Proc())

    started, detail = runner._kickstart("com.nickbrett.netwatch.speed")

    assert started is False
    assert "failed" in detail
    assert "Could not find service" in detail


def test_a_timeout_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda _: "/bin/launchctl")

    def boom(*args, **kwargs):
        raise runner.subprocess.TimeoutExpired(cmd="launchctl", timeout=10.0)

    monkeypatch.setattr(runner.subprocess, "run", boom)

    started, detail = runner._kickstart("com.nickbrett.netwatch.speed")

    assert started is False
    assert "timed out" in detail
