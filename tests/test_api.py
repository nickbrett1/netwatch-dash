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
from netwatch_dash.runner import ACTION_HEADER, ACTION_HEADER_VALUE, CHECKS, Runner
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


def test_a_2_5g_link_up_is_healthy_not_a_fault(tmp_path):
    """en0 renegotiating UP to 2500Base-T must not read as a down/crit link."""
    settings = make_settings(
        tmp_path,
        events_text=probe(
            "2026-09-20T12:49:00Z",
            media="2500Base-T full-duplex flow-control",
        ),
    )
    snap = snapshot.build(settings, now=at(AFTER_NEWEST))

    assert snap.status_inputs()["link_ok"] is True
    assert snap.status == "ok"


def test_a_100base_tx_link_down_is_crit(tmp_path):
    settings = make_settings(
        tmp_path, events_text=probe("2026-09-20T12:49:00Z", media="100Base-TX")
    )
    snap = snapshot.build(settings, now=at(AFTER_NEWEST))

    assert snap.status_inputs()["link_ok"] is False
    assert snap.status == "crit"
    assert any("below 1000Base-T" in r for r in snap.status_reason())


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
    assert any("below 1000Base-T" in r for r in snap.status_reason())


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


def test_lan_speed_collection_reads_the_lan_events(tmp_path):
    lan = json.dumps(
        {
            "ts": "2026-09-20T04:10:00Z",
            "kind": "lan_speed",
            "peer": "192.168.1.2",
            "dl_mbps": 941.2,
            "ul_mbps": 938.7,
        }
    )
    settings = make_settings(tmp_path, events_text=lan)
    snap = snapshot.build(settings, now=at(AFTER_NEWEST))

    body = snap.collection("lan_speed", 20)

    assert body["returned"] == 1
    assert body["in_window"] == 1
    assert body["lan_speeds"][0]["peer"] == "192.168.1.2"
    assert body["lan_speeds"][0]["dl_mbps"] == 941.2
    assert body["lan_speeds"][0]["ul_mbps"] == 938.7
    # The view is the schema's field set and nothing else, plus the age.
    assert set(body["lan_speeds"][0]) == set(snapshot.LAN_SPEED_FIELDS) | {"age_s"}


def test_a_lan_speed_run_is_reported_but_never_moves_the_status(tmp_path):
    """The WAN `speed` run is the status input (§7); the LAN run is a measurement.

    A slow Mac<->NAS run is worth seeing, but it is not evidence about the
    internet, so it must not paint the tile — it colours only its own (uncoloured)
    LAN tiles.
    """
    lan = json.dumps(
        {
            "ts": "2026-09-20T04:10:00Z",
            "kind": "lan_speed",
            "peer": "192.168.1.2",
            "dl_mbps": 12.0,
            "ul_mbps": 9.0,
        }
    )
    settings = make_settings(
        tmp_path,
        events_text=probe("2026-09-20T12:49:00Z") + "\n" + lan,
        config="DL_WARN_MBPS=100\nUL_WARN_MBPS=100\n",
    )
    snap = snapshot.build(settings, now=at(AFTER_NEWEST))

    assert snap.lan_speed["dl_mbps"] == 12.0
    assert snap.status == "ok"
    # And the reason list says the LAN run is deliberately not an input.
    assert any("lan_speed" in r for r in snap.status_reason())


def test_lan_runs_group_by_peer(tmp_path):
    """Two peers, two groups: the NAS and a faster ryzen never share a chart.

    The event carries one run against one peer; the page wants one section per
    peer. Grouping is the consumer's job, keyed by `peer_name`.
    """
    lan = "\n".join(
        json.dumps(
            {"ts": ts, "kind": "lan_speed", "peer": peer, "peer_name": name,
             "dl_mbps": dl, "ul_mbps": ul}
        )
        for ts, peer, name, dl, ul in (
            ("2026-10-15T04:10:00Z", "192.168.1.2", "nas", 941.2, 938.7),
            ("2026-10-15T04:11:00Z", "192.168.1.20", "ryzen", 2340.0, 2351.0),
            ("2026-10-16T04:10:00Z", "192.168.1.2", "nas", 930.0, 925.0),
            ("2026-10-16T04:11:00Z", "192.168.1.20", "ryzen", 2360.0, 2355.0),
        )
    )
    settings = make_settings(tmp_path, events_text=lan)
    snap = snapshot.build(settings, now=at("2026-10-16T12:00:00Z"))

    body = snap.collection("lan_speed", 20)
    groups = {g["name"]: g for g in body["by_peer"]}

    assert set(groups) == {"nas", "ryzen"}
    # Each group holds only its own peer's runs, oldest first.
    assert [r["peer"] for r in groups["nas"]["runs"]] == ["192.168.1.2"] * 2
    assert groups["nas"]["runs"][0]["dl_mbps"] == 941.2
    assert groups["nas"]["newest"]["dl_mbps"] == 930.0
    assert groups["ryzen"]["newest"]["dl_mbps"] == 2360.0
    # Newest activity first: both ran in the 2026-10-16 round, ryzen's run later.
    assert [g["name"] for g in body["by_peer"]] == ["ryzen", "nas"]


def test_lan_peer_falls_back_to_the_address_when_unnamed(tmp_path):
    """A legacy single-peer run carries no `peer_name`; the address is the name."""
    lan = json.dumps(
        {"ts": "2026-09-29T04:10:00Z", "kind": "lan_speed",
         "peer": "192.168.1.2", "dl_mbps": 941.2, "ul_mbps": 938.7}
    )
    settings = make_settings(tmp_path, events_text=lan)
    snap = snapshot.build(settings, now=at(AFTER_NEWEST))

    peers = snap.summary()["lan_peers"]

    assert len(peers) == 1
    assert peers[0]["name"] == "192.168.1.2"
    assert peers[0]["peer"] == "192.168.1.2"
    assert peers[0]["newest"]["dl_mbps"] == 941.2
    assert peers[0]["newest"]["peer_name"] is None


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

    for url in (
        "/healthz",
        "/api/summary",
        "/api/probe?limit=2",
        "/api/speed",
        "/api/lan-speed",
    ):
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


# --------------------------------------------------------- re-running a check
#
# The one action on the surface. These tests never shell out: the runner is
# injected with a fake command, so they assert the *contract* (who may start a
# run, which job, how often) without a Mac in the loop.

def rerun_client(tmp_path, calls=None):
    """A client whose runner records the labels it was asked to start."""
    calls = calls if calls is not None else []
    app = create_app(make_settings(tmp_path, events_text=fixture_events()))
    app.state.runner = Runner(run=lambda label: (calls.append(label) or True, f"ran {label}"))
    return TestClient(app), calls


def test_rerun_needs_the_header_a_cross_origin_page_cannot_set(tmp_path):
    client, calls = rerun_client(tmp_path)

    response = client.post("/api/rerun/speed")

    assert response.status_code == 403
    assert ACTION_HEADER in response.json()["detail"]
    assert calls == []  # refused before launchctl was ever consulted


def test_rerun_refuses_an_unknown_check_even_with_the_header(tmp_path):
    client, calls = rerun_client(tmp_path)

    response = client.post(
        "/api/rerun/com.nickbrett.netwatch.evil",
        headers={ACTION_HEADER: ACTION_HEADER_VALUE},
    )

    assert response.status_code == 404
    assert calls == []  # the label table is fixed, never taken from the request


def test_rerun_starts_the_named_job_through_the_injected_runner(tmp_path):
    client, calls = rerun_client(tmp_path)

    response = client.post(
        "/api/rerun/lan-speed", headers={ACTION_HEADER: ACTION_HEADER_VALUE}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["started"] is True
    assert body["label"] == CHECKS["lan-speed"]
    assert calls == [CHECKS["lan-speed"]]


def test_rerun_is_rate_limited_per_check(tmp_path):
    client, calls = rerun_client(tmp_path)
    headers = {ACTION_HEADER: ACTION_HEADER_VALUE}

    assert client.post("/api/rerun/speed", headers=headers).json()["started"] is True
    second = client.post("/api/rerun/speed", headers=headers).json()

    assert second["started"] is False
    assert second["retry_after_s"] > 0
    assert calls == [CHECKS["speed"]]  # the cooldown stopped the second launchctl
    # A different check has its own cooldown, so it is not blocked by the first.
    assert client.post("/api/rerun/lan-speed", headers=headers).json()["started"] is True


def test_rerun_is_a_post_so_a_stray_get_cannot_start_a_run(tmp_path):
    client, calls = rerun_client(tmp_path)

    assert client.get("/api/rerun/speed").status_code == 405
    assert calls == []