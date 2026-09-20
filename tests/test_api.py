"""The Phase 2 surface: one read of the live state, and the status it produces.

The state is the captured fixtures — real bytes from mac-studio — so these tests
fail if the contract moves rather than if the code drifts away from a mock.
"""

import json
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from netwatch_dash import events, snapshot
from netwatch_dash.app import create_app, healthz_body
from netwatch_dash.settings import Settings

FIX = Path(__file__).parent / "fixtures"
# The newest probe in the fixture, and a moment 47 s after it. The fixture's own
# line order is NOT chronological (a 2026-09-13 line is last), which is the point:
# the newest record is the newest *by timestamp*.
NEWEST_PROBE_TS = "2026-09-20T12:49:13Z"
AFTER_NEWEST = "2026-09-20T12:50:00Z"


def at(iso: str) -> datetime:
    # 3.11+ parses the `Z` the producers write; requires-python is >=3.11.
    return datetime.fromisoformat(iso)


def make_settings(
    tmp_path: Path,
    *,
    events_text: str | None = None,
    streak: str | None = None,
    alerts: str | None = None,
    config: str | None = None,
    **overrides,
) -> Settings:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    if events_text is not None:
        (state / "events.jsonl").write_text(events_text, encoding="utf-8")
    if streak is not None:
        (state / ".rtt_streak").write_text(streak, encoding="utf-8")
    if alerts is not None:
        (state / ".last_alert").write_text(alerts, encoding="utf-8")
    config_path = tmp_path / "config"
    if config is not None:
        config_path.write_text(config, encoding="utf-8")

    values = {
        "bind": "127.0.0.1:8791",
        "state_dir": state,
        "csv_path": tmp_path / "gateway_rtt.csv",
        "config_path": config_path,
        "tz": "America/New_York",
        "home": tmp_path / "home",
        "build_info_path": tmp_path / "build-info.json",
    }
    values.update(overrides)
    return Settings(**values)


def fixture_events() -> str:
    return (FIX / "events_sample.jsonl").read_text(encoding="utf-8")


def probe(ts: str, **fields) -> str:
    base = {
        "ts": ts,
        "kind": "probe",
        "gw": "192.168.1.1",
        "iface": "en0",
        "rtt_ms": 2.4,
        "loss_pct": 0.0,
        "media": "1000baseT full-duplex flow-control energ",
    }
    base.update(fields)
    return json.dumps(base)


def test_the_fixture_is_not_in_chronological_order():
    """If this fails the ordering test below proves nothing."""
    stamps = [
        json.loads(line)["ts"]
        for line in fixture_events().splitlines()
        if line.strip()
    ]
    assert stamps != sorted(stamps)


def test_newest_probe_is_newest_by_timestamp_not_by_position(tmp_path):
    settings = make_settings(tmp_path, events_text=fixture_events())
    snap = snapshot.build(settings, now=at(AFTER_NEWEST))

    assert snap.probe["ts"] == NEWEST_PROBE_TS
    assert snap.probe_age_s == 47.0


def test_ok_when_there_is_no_loss_and_the_link_is_gigabit(tmp_path):
    settings = make_settings(tmp_path, events_text=fixture_events())
    snap = snapshot.build(settings, now=at(AFTER_NEWEST))

    assert snap.status == "ok"
    assert snap.status_inputs()["loss_pct"] == 0.0
    assert snap.status_inputs()["link_ok"] is True


def test_ok_is_still_qualified_by_what_is_not_read(tmp_path):
    """A green light that does not say what it did not look at is the failure mode."""
    settings = make_settings(tmp_path, events_text=fixture_events())
    reasons = snapshot.build(settings, now=at(AFTER_NEWEST)).status_reason()

    # The csv is not read here, so the status says so rather than implying it was.
    assert any("gateway_rtt.csv" in r for r in reasons)
    # And the signals §7 lists but the derivation leaves out are named.
    assert any("peer RTT" in r for r in reasons)


def test_a_stale_probe_is_crit(tmp_path):
    settings = make_settings(tmp_path, events_text=fixture_events())
    snap = snapshot.build(settings, now=at("2026-09-20T13:30:00Z"))

    assert snap.probe_age_s == 2447.0
    assert snap.status == "crit"
    assert any("old" in r for r in snap.status_reason())


def test_loss_is_crit(tmp_path):
    settings = make_settings(
        tmp_path, events_text=probe("2026-09-20T12:49:00Z", loss_pct=5.0)
    )
    snap = snapshot.build(settings, now=at(AFTER_NEWEST))

    assert snap.status == "crit"
    assert any("loss 5.0%" in r for r in snap.status_reason())


def test_media_that_is_not_gigabit_is_crit(tmp_path):
    settings = make_settings(
        tmp_path, events_text=probe("2026-09-20T12:49:00Z", media="autoselect")
    )
    snap = snapshot.build(settings, now=at(AFTER_NEWEST))

    assert snap.status_inputs()["link_ok"] is False
    assert snap.status == "crit"
    assert any("not 1000baseT" in r for r in snap.status_reason())


def test_absent_media_is_unknown_not_a_fault(tmp_path):
    """The mirror error to 'unknown is never good' is 'unknown is never bad'."""
    fields = json.loads(probe("2026-09-20T12:49:00Z"))
    del fields["media"]  # the producer omits a field it has nothing to say about
    settings = make_settings(tmp_path, events_text=json.dumps(fields))
    snap = snapshot.build(settings, now=at(AFTER_NEWEST))

    assert snap.status_inputs()["link_ok"] is None
    assert snap.status == "ok"
    assert any("link rate unknown" in r for r in snap.status_reason())


def test_gateway_rtt_never_moves_the_status(tmp_path):
    """Schema §7, at the level that actually renders: a 500 ms gw ping beside a
    clean probe is benign router control-plane, not a fault."""
    settings = make_settings(
        tmp_path, events_text=probe("2026-09-20T12:49:00Z", rtt_ms=500.0)
    )
    snap = snapshot.build(settings, now=at(AFTER_NEWEST))

    assert snap.status == "ok"
    # It is still carried, for display.
    assert snap.status_inputs()["gw_rtt_ms"] == 500.0


def test_the_rtt_streak_is_reported_but_is_never_a_status_input(tmp_path):
    """The streak counts gateway-RTT over-threshold probes.

    Gateway RTT is not a health input, so a nonzero streak must not paint the
    tile; it is a diagnostic with its provenance, and the producer's own RTT_ALERT
    switch is what says so.
    """
    settings = make_settings(
        tmp_path,
        events_text=fixture_events(),
        streak="9\n",
        config="RTT_ALERT=off\n",
    )
    snap = snapshot.build(settings, now=at(AFTER_NEWEST))

    assert snap.streak == 9
    assert snap.status_inputs()["rtt_streak"] is None
    assert snap.status == "ok"
    assert any("RTT_ALERT=off" in r for r in snap.status_reason())


def test_throughput_below_threshold_is_warn_when_the_measurement_is_current(tmp_path):
    speed = json.dumps(
        {
            "ts": "2026-09-20T12:49:00Z",
            "kind": "speed",
            "dl_mbps": 10.0,
            "ul_mbps": 500.0,
            "ping_ms": 4.9,
            "server": "Verizon New York, NY",
        }
    )
    settings = make_settings(
        tmp_path,
        events_text=probe("2026-09-20T12:49:00Z") + "\n" + speed,
        config="DL_WARN_MBPS=100\nUL_WARN_MBPS=50\n",
    )
    snap = snapshot.build(settings, now=at(AFTER_NEWEST))

    assert snap.status == "warn"
    # The light being amber for a reason the list never states is the same defect
    # as a verdict that never changes, one level down: the reason list is how a
    # reader interrogates the status, and `warn` with no matching line is a dead
    # end. Both figures are named, and the one that is fine stays unmentioned.
    reasons = snap.status_reason()
    assert any("download is 10 Mbps, below the host's DL_WARN_MBPS=100" in r for r in reasons), reasons
    assert not any("upload is" in r for r in reasons), reasons


def test_a_stale_speed_measurement_does_not_move_the_status(tmp_path):
    """A days-old speed test is not evidence about the link now, either way."""
    speed = json.dumps({"ts": "2026-09-10T04:00:00Z", "kind": "speed", "dl_mbps": 1.0})
    settings = make_settings(
        tmp_path,
        events_text=probe("2026-09-20T12:49:00Z") + "\n" + speed,
        config="DL_WARN_MBPS=100\n",
    )
    snap = snapshot.build(settings, now=at(AFTER_NEWEST))

    assert snap.status == "ok"
    assert any("does not speak for the link now" in r for r in snap.status_reason())


def test_an_unreadable_config_is_said_rather_than_silently_defaulted(tmp_path):
    settings = make_settings(tmp_path, events_text=fixture_events())
    snap = snapshot.build(settings, now=at(AFTER_NEWEST))

    assert snap.config == {}
    assert any("thresholds are the defaults" in r for r in snap.status_reason())


def test_an_unknown_field_is_counted_in_the_drift(tmp_path):
    settings = make_settings(
        tmp_path, events_text=probe("2026-09-20T12:49:00Z", brand_new=7)
    )
    snap = snapshot.build(settings, now=at(AFTER_NEWEST))

    assert snap.events.drift.count == 1
    assert snap.events.drift.as_dict()["unknown_fields"] == ["brand_new"]


def test_a_torn_tail_is_not_drift(tmp_path):
    """The writer appends to a file the reader may open mid-write."""
    text = probe("2026-09-20T12:49:00Z") + '\n{"ts":"2026-09-20T12:54:0'
    settings = make_settings(tmp_path, events_text=text)
    snap = snapshot.build(settings, now=at(AFTER_NEWEST))

    assert snap.events.drift.count == 0
    assert len(snap.events.probes) == 1


def test_a_record_with_no_usable_timestamp_is_counted_not_dropped(tmp_path):
    settings = make_settings(
        tmp_path,
        events_text='{"ts":"yesterday","kind":"probe","loss_pct":0.0}\n'
        + probe("2026-09-20T12:49:00Z"),
    )
    snap = snapshot.build(settings, now=at(AFTER_NEWEST))

    assert snap.events.drift.bad_lines == 1
    assert len(snap.events.probes) == 1
    assert snap.probe["ts"] == "2026-09-20T12:49:00Z"


def test_the_tail_is_bounded_and_says_so(tmp_path):
    first = probe("2026-09-20T12:44:00Z")
    second = probe("2026-09-20T12:49:00Z")
    path = tmp_path / "events.jsonl"
    path.write_text(first + "\n" + second + "\n", encoding="utf-8")

    # A window that starts inside the first line: the fragment is dropped, not
    # parsed and not counted as drift.
    log = events.read(path, tail_bytes=len(first) + len(second) - 10)

    assert log.truncated is True
    assert log.bytes_read < (path.stat().st_size)
    assert [r["ts"] for r in log.probes] == ["2026-09-20T12:49:00Z"]
    assert log.drift.count == 0


def test_probe_collection_is_oldest_first_and_respects_the_limit(tmp_path):
    settings = make_settings(tmp_path, events_text=fixture_events())
    snap = snapshot.build(settings, now=at(AFTER_NEWEST))

    body = snap.collection("probe", 2)

    assert body["returned"] == 2
    assert body["limit"] == 2
    assert body["in_window"] == 4  # the fixture holds four probes
    assert [p["ts"] for p in body["probes"]] == [
        "2026-09-20T12:44:10Z",
        NEWEST_PROBE_TS,
    ]
    assert body["probes"][-1]["age_s"] == 47.0


def test_a_probe_view_is_the_schema_field_set_only(tmp_path):
    settings = make_settings(tmp_path, events_text=fixture_events())
    snap = snapshot.build(settings, now=at(AFTER_NEWEST))

    view = snap.view(snap.probe, snapshot.PROBE_FIELDS)

    assert set(view) == set(snapshot.PROBE_FIELDS) | {"age_s"}
    assert view["peer"] == "192.168.1.2"
    assert view["saturated"] is False


def test_speed_collection_reads_the_speed_events(tmp_path):
    speed = json.dumps(
        {
            "ts": "2026-09-20T04:00:00Z",
            "kind": "speed",
            "dl_mbps": 523.4,
            "ul_mbps": 565.6,
            "ping_ms": 4.9,
            "server": "Verizon New York, NY",
        }
    )
    settings = make_settings(tmp_path, events_text=speed)
    snap = snapshot.build(settings, now=at(AFTER_NEWEST))

    body = snap.collection("speed", 20)

    assert body["returned"] == 1
    assert body["speeds"][0]["dl_mbps"] == 523.4
    assert body["speeds"][0]["server"] == "Verizon New York, NY"
    assert body["speeds"][0]["age_s"] == 31800.0  # 12:50 minus 04:00


def test_summary_carries_its_own_evidence(tmp_path):
    settings = make_settings(
        tmp_path,
        events_text=fixture_events(),
        streak="2\n",
        alerts="loss 1789843879\nrtt 1789870102\n",
    )
    snap = snapshot.build(settings, now=at(AFTER_NEWEST))

    body = snap.summary()

    assert body["now"] == AFTER_NEWEST
    assert body["status"] == "ok"
    assert body["streak"] == 2
    assert set(body["last_alert"]) == {"loss", "rtt"}
    assert body["tz"] == "America/New_York"
    assert body["sources"]["events"]["lines"] == 4
    assert body["sources"]["events"]["truncated"] is False
    # The CSV is named and explicitly not read, rather than absent from the
    # answer: a snapshot built without a rollup never touches the file (schema §3).
    assert body["sources"]["csv"]["read"] is False
    assert body["csv"]["reading"] is False  # not yet seeded, and it says so


def test_every_endpoint_answers_200(tmp_path):
    settings = make_settings(tmp_path, events_text=fixture_events())
    client = TestClient(create_app(settings))

    for url in ("/healthz", "/api/summary", "/api/probe?limit=2", "/api/speed"):
        response = client.get(url)
        assert response.status_code == 200, url
        assert response.json(), url

    assert len(client.get("/api/probe?limit=2").json()["probes"]) == 2


def test_a_limit_outside_the_range_is_rejected(tmp_path):
    client = TestClient(create_app(make_settings(tmp_path, events_text=fixture_events())))

    assert client.get("/api/probe?limit=0").status_code == 422
    assert client.get("/api/probe?limit=100000").status_code == 422


def test_healthz_reports_the_drift_count_and_the_status(tmp_path):
    settings = make_settings(
        tmp_path, events_text=probe("2026-09-20T12:49:00Z", brand_new=1)
    )
    body = healthz_body(settings, now=at(AFTER_NEWEST))

    assert body["status"] == "ok"
    assert body["data"]["drift_count"] == 1
    assert body["data"]["drift"]["events.jsonl"]["unknown_fields"] == ["brand_new"]
    assert body["data"]["drift"]["gateway_rtt.csv"]["count"] == 0
    assert body["sources"]["events"]["lines"] == 1
