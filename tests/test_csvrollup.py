"""The csv tailer and rollup, against the captured bytes in `tests/fixtures/`."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from netwatch_dash import csvrollup, snapshot
from netwatch_dash.app import healthz_body
from netwatch_dash.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures"
# 2026-09-17T13:09:07 local (EDT) is 17:09:07Z = 1789664947, which is the last
# moment the captured window describes.
END_OF_WINDOW = datetime(2026, 9, 17, 17, 12, 0, tzinfo=UTC)


def make_settings(
    tmp_path: Path, csv_text: str | None = None, *, retention_s: str | None = None
) -> Settings:
    home = tmp_path
    state = home / ".local/state/netwatch"
    state.mkdir(parents=True, exist_ok=True)
    (home / "netwatch").mkdir(parents=True, exist_ok=True)
    if csv_text is not None:
        (home / "netwatch/gateway_rtt.csv").write_text(csv_text, encoding="utf-8")
    (home / ".config/netwatch").mkdir(parents=True, exist_ok=True)
    (home / ".config/netwatch/config").write_text(
        "RTT_WARN_MS=4.0\nDL_WARN_MBPS=250\n", encoding="utf-8"
    )
    env = {"NETWATCH_DASH_HOME": str(home)}
    if retention_s is not None:
        env["NETWATCH_DASH_CSV_RETENTION_S"] = retention_s
    return Settings.from_env(env)


def fixture_csv() -> str:
    return (FIXTURES / "gateway_rtt_sample.csv").read_text(encoding="utf-8")


def read_once(tmp_path: Path, text: str) -> csvrollup.Rollup:
    path = tmp_path / "gw.csv"
    path.write_text(text, encoding="utf-8")
    # Pinned to the captured window: markers older than the retention window are
    # dropped, and a test that read the file "now" would be testing the prune.
    return csvrollup.read(
        path,
        csvrollup.Cursor(),
        tz="America/New_York",
        now_epoch=END_OF_WINDOW.timestamp(),
    )


def test_marker_local_time_becomes_the_canonical_epoch(tmp_path):
    """§1: markers are local, the epoch is canonical, and joining is by the epoch."""
    rollup = read_once(tmp_path, fixture_csv())
    change = rollup.markers_of("link_change")[0]

    assert change.ts_iso == "2026-09-17T13:09:07"
    assert change.unixtime == 1789664947.0  # 17:09:07Z
    assert rollup.first_unixtime == 1789664386.175
    assert rollup.last_unixtime == 1789664987.0 or rollup.last_unixtime == 1789664898.025


def test_a_clean_parse_of_the_captured_bytes_is_the_invariant(tmp_path):
    """§6: any non-zero drift against real bytes means producer and schema separated."""
    rollup = read_once(tmp_path, fixture_csv())

    assert rollup.drift.as_dict() == {
        "count": 0,
        "unknown_fields": [],
        "unknown_markers": [],
        "unknown_targets": [],
        "unknown_kinds": [],
        "bad_lines": 0,
    }
    assert rollup.samples == 4
    assert len(rollup.markers) == 6


def test_samples_land_in_per_minute_buckets_by_target(tmp_path):
    rollup = read_once(tmp_path, fixture_csv())

    assert rollup.minute_series("gw", 10)[0]["rtt_ms_avg"] == 2.084
    assert rollup.minute_series("net", 10)[0]["rtt_ms_avg"] == 6.593
    assert rollup.minute_series("wl", 10)[0]["rtt_ms_avg"] == 3.314
    assert rollup.minute_series("nonexistent", 10) == []


def test_loss_is_not_averaged_in_as_zero_latency(tmp_path):
    """An empty `rtt_ms` is loss (§3). Counting it as 0 ms would flatter the minute."""
    text = "ts_iso,unixtime,target,rtt_ms\n" + "".join(
        f"2026-09-17T12:59:4{i}.000,{1789664386 + i}.0,gw,{'' if i < 3 else '4.0'}\n"
        for i in range(5)
    )
    rollup = read_once(tmp_path, text)
    minute = rollup.minute_series("gw", 1)[0]

    assert minute["n"] == 5
    assert minute["loss"] == 3
    assert minute["loss_pct"] == 60.0
    assert minute["rtt_ms_avg"] == 4.0  # over the two that were measured


def test_an_appended_partial_line_is_held_until_it_is_complete(tmp_path):
    """The producer appends to a file this reads; half a line is normal."""
    path = tmp_path / "gw.csv"
    row = "2026-09-17T12:59:46.174,1789664386.175,gw,2.084\n"
    path.write_text("ts_iso,unixtime,target,rtt_ms\n" + row, encoding="utf-8")
    cursor = csvrollup.Cursor()
    rollup = csvrollup.read(path, cursor, tz="UTC")

    with path.open("a", encoding="utf-8") as handle:
        handle.write(row[:20])
    rollup = csvrollup.read(path, cursor, rollup, tz="UTC")
    assert rollup.samples == 1  # nothing parsed yet
    assert cursor.partial == row[:20]

    with path.open("a", encoding="utf-8") as handle:
        handle.write(row[20:])
    rollup = csvrollup.read(path, cursor, rollup, tz="UTC")
    assert rollup.samples == 2
    assert cursor.partial == ""


def test_the_cursor_is_dropped_when_the_file_is_replaced(tmp_path):
    """§3's gap: the offset is only valid while the file grows, so it is validated."""
    path = tmp_path / "gw.csv"
    row = "2026-09-17T12:59:46.174,1789664386.175,gw,2.084\n"
    path.write_text("ts_iso,unixtime,target,rtt_ms\n" + row, encoding="utf-8")
    cursor = csvrollup.Cursor()
    rollup = csvrollup.read(path, cursor, tz="UTC")
    assert rollup.samples == 1

    other = tmp_path / "gw.csv.new"
    other.write_text(
        "ts_iso,unixtime,target,rtt_ms\n"
        "2026-09-17T13:59:46.174,1789667986.175,net,9.5\n",
        encoding="utf-8",
    )
    os.replace(other, path)
    rollup = csvrollup.read(path, cursor, rollup, tz="UTC")

    assert "inode changed" in (rollup.restarted or "")
    # A re-seed is a new window: the old minutes are gone, not spliced on.
    assert rollup.samples == 1
    assert rollup.latest_of("gw") is None
    assert rollup.latest_of("net")["rtt_ms"] == 9.5


def test_a_shorter_file_is_a_truncation_not_a_negative_offset(tmp_path):
    path = tmp_path / "gw.csv"
    row = "2026-09-17T12:59:46.174,1789664386.175,gw,2.084\n"
    path.write_text("ts_iso,unixtime,target,rtt_ms\n" + row * 3, encoding="utf-8")
    cursor = csvrollup.Cursor()
    rollup = csvrollup.read(path, cursor, tz="UTC")
    assert rollup.samples == 3

    path.write_text("ts_iso,unixtime,target,rtt_ms\n" + row, encoding="utf-8")
    rollup = csvrollup.read(path, cursor, rollup, tz="UTC")

    assert "shorter than the offset" in (rollup.restarted or "")
    assert rollup.samples == 1


def test_a_missing_file_is_a_reported_absence_not_an_exception(tmp_path):
    rollup = csvrollup.read(tmp_path / "nope.csv", csvrollup.Cursor(), tz="UTC")

    assert rollup.error.startswith("FileNotFoundError")
    assert rollup.window()["read"] is False
    assert rollup.window()["size"] is None


def test_interface_errors_are_deltas_and_a_decrease_reads_as_zero(tmp_path):
    """§8: the producer writes raw counters; the dashboard owns the deltas."""
    text = (FIXTURES / "iferrs_sample.csv").read_text(encoding="utf-8")
    rollup = read_once(tmp_path, text)

    assert rollup.iface() == "en0"
    assert rollup.error_totals["rx_bytes"] == 93365847841 - 93364822283
    # The first marker has no predecessor: a baseline, not invented history.
    assert rollup.error_intervals[-1]["delta"]["rx_bytes"] == 0

    reset = text + (
        '# iferrs 2026-09-20T11:44:56 {"iface": "en0", "ierrs": 0, "oerrs": 0, '
        '"coll": 0, "rx_bytes": 100, "tx_bytes": 100}\n'
    )
    rollup = read_once(tmp_path, reset)
    assert rollup.error_totals["rx_bytes"] > 0  # the reset contributed 0, not less


def test_the_cache_answers_before_the_seed_has_finished(tmp_path):
    """The seed is off the request path, so "not read yet" is its own answer."""
    settings = make_settings(tmp_path, fixture_csv())
    cache = csvrollup.RollupCache()

    answer = cache.get(settings, now_epoch=END_OF_WINDOW.timestamp())

    assert answer.reading is True
    assert answer.seeded is False
    assert answer.window()["read"] is False


def test_the_cache_seeds_once_and_then_reads_only_what_is_new(tmp_path):
    settings = make_settings(tmp_path, fixture_csv())
    cache = csvrollup.RollupCache()
    moment = END_OF_WINDOW.timestamp()

    first = cache.refresh(settings, now_epoch=moment)
    assert first.samples == 4
    assert first.window()["read"] is True

    path = settings.csv_path
    appended = "2026-09-17T13:10:00.000,1789665000.000,net,5.0\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(appended)
    # Past the refresh debounce, so `get` actually reads rather than handing back
    # the rollup it already has.
    later = moment + settings.csv_refresh_s + 1
    second = cache.get(settings, now_epoch=later)
    assert second.samples == 5
    assert second.bytes_read == len(appended)


def test_the_csv_takes_over_the_status_inputs_it_owns(tmp_path):
    """Forwarded RTT, csv loss and en0 errors are inputs now, and are named."""
    settings = make_settings(tmp_path, fixture_csv())
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "events.jsonl").write_text(
        '{"ts":"2026-09-17T17:11:00Z","kind":"probe","rtt_ms":2.0,"loss_pct":0.0,'
        '"media":"1000baseT full-duplex"}\n',
        encoding="utf-8",
    )
    cache = csvrollup.RollupCache()
    cache.refresh(settings, now_epoch=END_OF_WINDOW.timestamp())
    snap = snapshot.build_with_csv(settings, cache, END_OF_WINDOW)

    inputs = snap.status_inputs()
    # The captured window ends four minutes before `now`, so it speaks for the
    # link: the forwarded path (net) is an input, judged against the host's own
    # RTT_WARN_MS — and 6.593 ms is above 4.0, hence the warning.
    assert snap.csv_current is True
    assert inputs["forwarded_rtt_ms"] == 6.593
    assert inputs["forwarded_warn_ms"] == 4.0  # the host's RTT_WARN_MS
    assert snap.status == "warn"
    assert any("forwarded-path RTT" in r for r in snap.status_reason())


def test_a_current_reading_above_the_host_threshold_is_a_warning(tmp_path):
    settings = make_settings(
        tmp_path,
        "ts_iso,unixtime,target,rtt_ms\n"
        f"2026-09-17T17:11:00.000,{int(END_OF_WINDOW.timestamp())}.0,net,9.5\n",
    )
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "events.jsonl").write_text(
        '{"ts":"2026-09-17T17:11:00Z","kind":"probe","rtt_ms":2.0,"loss_pct":0.0,'
        '"media":"1000baseT full-duplex"}\n',
        encoding="utf-8",
    )
    cache = csvrollup.RollupCache()
    cache.refresh(settings, now_epoch=END_OF_WINDOW.timestamp())
    snap = snapshot.build_with_csv(settings, cache, END_OF_WINDOW)

    assert snap.status_inputs()["forwarded_rtt_ms"] == 9.5
    assert snap.status == "warn"
    assert any("forwarded-path RTT" in r for r in snap.status_reason())


def test_a_gateway_spike_with_a_flat_path_is_localised_to_the_router(tmp_path):
    """§7's inversion: the panel's verdict, not just its thresholds, has to flip."""
    moment = int(END_OF_WINDOW.timestamp())
    settings = make_settings(
        tmp_path,
        "ts_iso,unixtime,target,rtt_ms\n"
        f"2026-09-17T17:11:00.000,{moment}.0,gw,76.4\n"
        f"2026-09-17T17:11:00.000,{moment}.0,net,3.1\n",
    )
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "events.jsonl").write_text(
        '{"ts":"2026-09-17T17:11:00Z","kind":"probe","rtt_ms":2.0,"loss_pct":0.0,'
        '"media":"1000baseT full-duplex"}\n',
        encoding="utf-8",
    )
    cache = csvrollup.RollupCache()
    cache.refresh(settings, now_epoch=END_OF_WINDOW.timestamp())
    snap = snapshot.build_with_csv(settings, cache, END_OF_WINDOW)

    body = snap.localise()

    assert body["reading"]["verdict"] == "router control plane"
    assert body["reading"]["numbers"]["gw"] == 76.4
    assert body["reading"]["numbers"]["forwarded"] == 3.1
    # and the gateway did not move the status
    assert snap.status_inputs()["forwarded_rtt_ms"] == 3.1
    assert snap.status == "ok"


def test_the_panels_answer_over_http(tmp_path):
    from starlette.testclient import TestClient

    from netwatch_dash.app import create_app

    # The captured window is three days old and the request path reads at
    # real-now, so the window is widened to keep the fixture: this test is about
    # the panels answering, not about the prune (which its own tests cover).
    settings = make_settings(tmp_path, fixture_csv(), retention_s="2592000")
    app = create_app(settings)
    # Seed on the test's own thread so the first request does not race the
    # start-up prewarm and read an empty rollup.
    app.state.csv.refresh(settings)
    client = TestClient(app)

    for url in ("/api/link", "/api/localise", "/api/incidents"):
        response = client.get(url)
        assert response.status_code == 200, url
        assert response.json(), url

    link = client.get("/api/link").json()
    assert "link_change" in [c["kind"] for c in link["changes"]]
    incidents = client.get("/api/incidents").json()
    assert incidents["counts"]["bursts"] == 1
    assert incidents["bursts"][0]["open"] is False


def test_a_burst_that_never_ended_is_reported_as_open(tmp_path):
    settings = make_settings(
        tmp_path,
        "ts_iso,unixtime,target,rtt_ms\n"
        "# burst_start 2026-09-17T13:08:38 163.9ms\n",
    )
    cache = csvrollup.RollupCache()
    cache.refresh(settings, now_epoch=END_OF_WINDOW.timestamp())
    snap = snapshot.build_with_csv(settings, cache, END_OF_WINDOW)

    bursts = snap.incidents()["bursts"]

    assert len(bursts) == 1
    assert bursts[0]["open"] is True
    assert bursts[0]["end"] is None


def test_healthz_counts_the_drift_of_both_files(tmp_path):
    settings = make_settings(tmp_path, "ts_iso,unixtime,target,rtt_ms\n")
    body = healthz_body(settings)

    assert body["data"]["drift"]["gateway_rtt.csv"]["count"] == 0
    assert body["not_implemented"] == {}
    assert os.path.isdir(settings.home)
