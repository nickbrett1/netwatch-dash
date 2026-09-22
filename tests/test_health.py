"""``/healthz`` — the payload's self-report, over HTTP.

The endpoint is a `siteMonitor` target, so the tests are about the two things a
monitor and a human both depend on: it always answers (200, JSON, no exception,
whatever the filesystem looks like), and what it says is traceable — a build it
cannot read is *said*, not smoothed over, and the config's capability token can
never appear.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from netwatch_dash.__main__ import main
from netwatch_dash.app import create_app, healthz_body
from netwatch_dash.buildinfo import sha256
from netwatch_dash.settings import Settings


def make_settings(tmp_path: Path, **overrides) -> Settings:
    home = tmp_path / "home"
    values = {
        "bind": "127.0.0.1:8791",
        "state_dir": home / ".local/state/netwatch",
        "csv_path": home / "netwatch/gateway_rtt.csv",
        "config_path": home / ".config/netwatch/config",
        "tz": "America/New_York",
        "home": home,
        "build_info_path": tmp_path / "build-info.json",
    }
    values.update(overrides)
    return Settings(**values)


def write_build_info(tmp_path: Path, **overrides) -> Path:
    info = {
        "name": "netwatch-dash",
        "version": "1.2.3",
        "tag": "v1.2.3",
        "commit": "a" * 40,
        "target": "aarch64-apple-darwin",
        "built_at": "2026-09-20T00:00:00Z",
        "python": {"version": "3.13.15", "release": "20260901"},
        "wheel": {"file": "netwatch_dash-1.2.3-py3-none-any.whl"},
        "dependencies": {"fastapi": "0.141.1", "uvicorn": "0.53.0"},
        "producers": {},
    }
    info.update(overrides)
    path = tmp_path / "build-info.json"
    path.write_text(json.dumps(info), encoding="utf-8")
    return path


def test_healthz_reports_what_this_payload_is(tmp_path):
    write_build_info(tmp_path)
    body = healthz_body(make_settings(tmp_path))

    assert body["version"] == "1.2.3"
    assert body["commit"] == "a" * 40
    assert body["build"]["found"] is True
    assert body["build"]["tag"] == "v1.2.3"
    assert body["build"]["target"] == "aarch64-apple-darwin"
    assert body["build"]["dependencies"] == {"fastapi": "0.141.1", "uvicorn": "0.53.0"}


def test_healthz_says_which_launcher_started_it(tmp_path, monkeypatch):
    """The launcher exports its own identity so *this* endpoint can report it."""
    write_build_info(tmp_path)
    monkeypatch.setenv("FETCH_LAUNCH_PATH", "/host/.local/share/netwatch-dash/fetch-launch.sh")
    monkeypatch.setenv("FETCH_LAUNCH_VERSION", "1.2.3")
    monkeypatch.setenv("FETCH_LAUNCH_SHA256", "b" * 64)

    body = healthz_body(make_settings(tmp_path))

    assert body["launcher"] == {
        "found": True,
        "path": "/host/.local/share/netwatch-dash/fetch-launch.sh",
        "version": "1.2.3",
        "sha256": "b" * 64,
    }


def test_healthz_with_no_launcher_does_not_invent_one(tmp_path, monkeypatch):
    """A payload started by hand is the common case, and it is not a fault."""
    write_build_info(tmp_path)
    for name in ("FETCH_LAUNCH_PATH", "FETCH_LAUNCH_VERSION", "FETCH_LAUNCH_SHA256"):
        monkeypatch.delenv(name, raising=False)

    body = healthz_body(make_settings(tmp_path))

    assert body["launcher"]["found"] is False
    assert body["launcher"]["path"] is None


def test_healthz_over_http_is_json_and_200(tmp_path):
    """A monitor reads the status line, not the body; it must never 500."""
    write_build_info(tmp_path)
    client = TestClient(create_app(make_settings(tmp_path)))
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["version"] == "1.2.3"


def test_status_is_unknown_not_ok_while_there_is_no_data(tmp_path):
    """`unknown` is the honest answer, and the reason says which inputs are missing.

    A payload that has never probed has no loss, no link and no age, so an "ok"
    here would be a claim about a network nothing has looked at (schema §6).
    """
    write_build_info(tmp_path)
    body = healthz_body(make_settings(tmp_path))
    assert body["status"] == "unknown"
    assert any("events.jsonl" in reason for reason in body["status_reason"])
    assert any("not read yet" in reason for reason in body["status_reason"])
    # Zero because the read happened and found nothing; the read is what the
    # count reports on, not the presence of a file.
    assert body["data"]["drift_count"] == 0


def test_a_payload_that_cannot_read_its_own_build_says_so(tmp_path):
    body = healthz_body(make_settings(tmp_path))  # no build-info.json written

    assert body["build"]["found"] is False
    assert body["build"]["error"]
    assert body["version"] is None
    assert any("cannot report its own build" in r for r in body["status_reason"])


def test_unparseable_build_info_is_reported_not_raised(tmp_path):
    (tmp_path / "build-info.json").write_text("{", encoding="utf-8")
    body = healthz_body(make_settings(tmp_path))
    assert body["build"]["found"] is False
    assert "not valid JSON" in body["build"]["error"]


def test_producer_skew_and_missing_are_counted_and_explained(tmp_path):
    home = tmp_path / "home"
    (home / ".local/bin").mkdir(parents=True)
    (home / "netwatch").mkdir(parents=True)
    (home / ".local/bin/netwatch").write_text("same", encoding="utf-8")
    (home / "netwatch/gwping.py").write_text("host copy", encoding="utf-8")
    shipped = {
        "netwatch": sha256(home / ".local/bin/netwatch"),
        "gwping.py": "0" * 64,
        "flapwatch": "1" * 64,
    }
    write_build_info(tmp_path, producers=shipped)

    body = healthz_body(make_settings(tmp_path))

    assert body["producers"]["skew"] == ["gwping.py"]
    assert body["producers"]["missing"] == ["flapwatch"]
    assert any("differ from the ones this release ships" in r for r in body["status_reason"])


def test_a_release_whose_producers_all_match_reports_no_skew(tmp_path):
    home = tmp_path / "home"
    (home / "netwatch").mkdir(parents=True)
    (home / "netwatch/gwping.py").write_text("verbatim", encoding="utf-8")
    write_build_info(
        tmp_path, producers={"gwping.py": sha256(home / "netwatch/gwping.py")}
    )

    body = healthz_body(make_settings(tmp_path))

    assert body["producers"]["skew"] == []
    assert not any("differ from" in r for r in body["status_reason"])


def test_config_whitelist_and_never_the_token(tmp_path):
    cfg = tmp_path / "home/.config/netwatch/config"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        "RTT_ALERT=off\nRTT_WARN_MS=4.0\nNTFY_TOPIC=sekrit-topic\nUNKNOWN_KEY=1\n",
        encoding="utf-8",
    )

    body = healthz_body(make_settings(tmp_path))

    assert body["config"]["RTT_ALERT"] == "off"
    assert body["config"]["RTT_WARN_MS"] == "4.0"
    assert "UNKNOWN_KEY" not in body["config"]
    assert "sekrit-topic" not in json.dumps(body)


def test_absent_config_is_reported_not_assumed(tmp_path):
    body = healthz_body(make_settings(tmp_path))
    assert body["config"] == {}
    assert body["config_error"] == "not present"


def test_no_endpoint_is_named_as_missing_any_more(tmp_path):
    """Every endpoint the contract named now exists; the list is empty, not gone."""
    write_build_info(tmp_path)
    body = healthz_body(make_settings(tmp_path))
    assert body["not_implemented"] == {}


def test_version_flag_answers_without_the_asgi_stack(tmp_path, monkeypatch, capsys):
    """The smoke gate probes this on the assembled payload, before any reader."""
    monkeypatch.setenv("NETWATCH_DASH_BUILD_INFO", str(tmp_path / "nope.json"))
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.startswith("netwatch-dash ")


def test_help_lists_the_environment_overrides(capsys):
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "NETWATCH_DASH_BIND" in out
    assert "--version" in out


@pytest.mark.parametrize(
    "value,expected",
    [
        ("127.0.0.1:8791", ("127.0.0.1", 8791)),
        ("100.77.144.14:8791", ("100.77.144.14", 8791)),
    ],
)
def test_bind_is_split_into_host_and_port(value, expected):
    assert Settings.from_env({"NETWATCH_DASH_BIND": value}).host_port == expected


def test_a_bind_that_is_not_host_port_fails_loudly():
    """Better a start-up failure than a 500 on every request."""
    settings = Settings.from_env({"NETWATCH_DASH_BIND": "8791"})
    with pytest.raises(ValueError):
        _ = settings.host_port


def test_defaults_are_the_host_paths_and_loopback():
    settings = Settings.from_env({"HOME": "/Users/nick"})
    assert settings.bind == "127.0.0.1:8791"
    assert settings.state_dir == Path("/Users/nick/.local/state/netwatch")
    assert settings.csv_path == Path("/Users/nick/netwatch/gateway_rtt.csv")
    assert settings.config_path == Path("/Users/nick/.config/netwatch/config")
    assert settings.tz == "America/New_York"
    assert settings.build_info_path is None


def test_the_landing_page_is_the_drill_in(tmp_path):
    """Clicking the tile must land on something real, not a 404."""
    write_build_info(tmp_path)
    client = TestClient(create_app(make_settings(tmp_path)))

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "netwatch-dash" in response.text
    assert "/api/localise" in response.text  # the history it draws
    # Bandwidth is a single number on the tile; the daily runs behind it are the
    # other half of "drill in", and live behind a different endpoint.
    assert "/api/speed" in response.text


def test_the_landing_loss_chart_does_not_count_loss_twice(tmp_path):
    """Regression, in the page itself: `n - measured` *is* `loss`, so a bar that
    added the two drew at twice its own tooltip percentage. The chart has no JS
    test harness, so this pins the one arithmetic that went wrong rather than the
    layout around it.
    """
    write_build_info(tmp_path)
    client = TestClient(create_app(make_settings(tmp_path)))

    html = client.get("/").text

    assert "(p.n || 0) - (p.measured || 0)" not in html
    assert "const lost = p.loss || 0;" in html
