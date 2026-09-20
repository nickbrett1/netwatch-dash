"""The csv tailer and rollup, against the captured bytes in `tests/fixtures/`."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from netwatch_dash import csvrollup, snapshot
from netwatch_dash.app import healthz_body
from netwatch_dash.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures"
# 2026-09-17T13:09:07 local (EDT) is 17:09:07Z = 1789664947, which is the last
# moment the captured window describes.
END_OF_WINDOW = datetime(2026, 9, 17, 17, 12, 0, tzinfo=UTC)


def make_settings(
    tmp_path: Path,
    csv_text: str | None = None,
    *,
    retention_s: str | None = None,
    config_text: str | None = None,
) -> Settings:
    home = tmp_path
    state = home / ".local/state/netwatch"
    state.mkdir(parents=True, exist_ok=True)
    (home / "netwatch").mkdir(parents=True, exist_ok=True)
    if csv_text is not None:
        (home / "netwatch/gateway_rtt.csv").write_text(csv_text, encoding="utf-8")
    (home / ".config/netwatch").mkdir(parents=True, exist_ok=True)
    # The host's own numbers, as deployed: the forwarded-path verdict is the
    # residual's, with RTT_WARN_MS only as a backstop, so the fixtures have to
    # carry a ceiling that clears the path's real 5-7 ms floor. A fixture at
    # RTT_WARN_MS=4.0 would pass every test here while painting the live tile
    # amber on 120 of 120 minutes.
    (home / ".config/netwatch/config").write_text(
        config_text
        if config_text is not None
        else "RTT_WARN_MS=25.0\nRTT_EXCESS_MS=10.0\nDL_WARN_MBPS=250\n",
        encoding="utf-8",
    )
    env = {"NETWATCH_DASH_HOME": str(home)}
    if retention_s is not None:
        env["NETWATCH_DASH_CSV_RETENTION_S"] = retention_s
    return Settings.from_env(env)


def fixture_csv() -> str:
    return (FIXTURES / "gateway_rtt_sample.csv").read_text(encoding="utf-8")


def _minute_rows(target: str, start_minute: int, count: int, rtt_for) -> str:
    """One sample per minute for `count` minutes, `rtt_for(i)` deciding each.

    `rtt_for` returns the raw `rtt_ms` field, so an empty string is a lost probe
    — the producer's own spelling of loss (§3) — rather than a zero.
    """
    return "".join(
        f"2026-09-17T00:00:00.000,{(start_minute + i) * 60}.0,{target},{rtt_for(i)}\n"
        for i in range(count)
    )


def _hour_aligned(minute: int) -> int:
    """A minute index on an hour boundary, so a folded hour is exactly 60 minutes.

    `series` buckets by `minute // 60`, so a stream that starts mid-hour gives its
    first bucket a partial hour. Tests that count minutes inside an hour want the
    stream to start on the boundary.
    """
    return (minute // 60) * 60


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
    """Forwarded RTT, csv loss and en0 errors are inputs now, and are named.

    The captured window ends four minutes before `now`, so it speaks for the
    link: `net` is 6.593 ms. That is *not* a fault. At this host's thresholds the
    path's own floor (5-7 ms measured) sits under a 25 ms ceiling and under
    10 ms of excess over the gateway — the regression this pins is the old rule
    `net > RTT_WARN_MS=4.0`, which called 120 of 120 live minutes a warning and
    so said nothing at all.
    """
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
    assert snap.csv_current is True
    assert inputs["forwarded_rtt_ms"] == 6.593
    assert inputs["forwarded_warn_ms"] == 25.0  # the host's RTT_WARN_MS
    assert inputs["forwarded_excess_ms"] == 10.0  # the host's RTT_EXCESS_MS
    # The residual is measured against the csv's gateway, same file and minute —
    # not the probe's 300 s reading, which is five minutes of router jitter away.
    assert inputs["forwarded_gw_rtt_ms"] == snap.csv_gw_rtt_ms
    assert snap.status == "ok"
    assert not any("forwarded-path RTT" in r for r in snap.status_reason())


def test_a_healthy_path_is_quiet_under_a_realistic_ceiling(tmp_path):
    """The bug this exists to prevent: a threshold below the path's own floor.

    mac-studio 2026-09-20: `net` never went under 5.4 ms in 120 minutes, so
    RTT_WARN_MS=4.0 fired on all of them. A verdict that never changes carries no
    information, so the test asserts the quiet case at a ceiling that clears the
    floor — and asserts the loud case, so "quiet" is a decision and not a typo.
    """
    moment = int(END_OF_WINDOW.timestamp())
    row = "2026-09-17T17:11:00.000,{}.0,net,6.6\n2026-09-17T17:11:00.000,{}.0,gw,3.9\n"
    settings = make_settings(tmp_path, row.format(moment, moment))
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "events.jsonl").write_text(
        '{"ts":"2026-09-17T17:11:00Z","kind":"probe","rtt_ms":2.0,"loss_pct":0.0,'
        '"media":"1000baseT full-duplex"}\n',
        encoding="utf-8",
    )
    cache = csvrollup.RollupCache()
    cache.refresh(settings, now_epoch=END_OF_WINDOW.timestamp())
    snap = snapshot.build_with_csv(settings, cache, END_OF_WINDOW)

    assert snap.csv_gw_rtt_ms == 3.9
    assert snap.status == "ok"
    reading = snap.localise()["reading"]
    assert reading["verdict"] == "no hop implicated"
    assert reading["fault"] is None
    assert reading["numbers"]["excess"] == pytest.approx(6.6 - 3.9)


def test_a_forwarded_path_above_the_ceiling_is_a_warning(tmp_path):
    """One of the two conditions: the whole path, router included, is slow."""
    moment = int(END_OF_WINDOW.timestamp())
    settings = make_settings(
        tmp_path,
        "ts_iso,unixtime,target,rtt_ms\n"
        f"2026-09-17T17:11:00.000,{moment}.0,net,30.0\n",
        config_text="RTT_WARN_MS=25.0\nRTT_EXCESS_MS=10.0\n",
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

    assert snap.status == "warn"
    # No gateway sample in the csv, so only the ceiling could have fired — and
    # the reason says so rather than guessing which hop is at fault.
    assert any(
        "above the host's RTT_WARN_MS=25" in r for r in snap.status_reason()
    ), snap.status_reason()


def test_the_excess_beyond_the_router_is_a_warning_on_its_own(tmp_path):
    """The other condition, and the one §7's prose actually describes.

    The ceiling is set high enough that it cannot fire, so only the residual can
    be responsible: 30 ms to 1.1.1.1 against a 3 ms gateway is 27 ms of WAN.
    """
    moment = int(END_OF_WINDOW.timestamp())
    settings = make_settings(
        tmp_path,
        "ts_iso,unixtime,target,rtt_ms\n"
        f"2026-09-17T17:11:00.000,{moment}.0,net,30.0\n"
        f"2026-09-17T17:11:00.000,{moment}.0,gw,3.0\n",
        config_text="RTT_WARN_MS=100.0\nRTT_EXCESS_MS=10.0\n",
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

    assert snap.status == "warn"
    reasons = snap.status_reason()
    assert any("27.0ms beyond the router" in r for r in reasons), reasons
    reading = snap.localise()["reading"]
    assert reading["verdict"] == "forwarded path"
    assert reading["fault"] == "beyond-router"
    assert reading["numbers"]["excess"] == 27.0


def test_a_router_spike_is_judged_on_the_csv_gateway_not_the_probe(tmp_path):
    """The residual needs two samples from the same minute.

    A 500 ms *probe* reading is five minutes and a whole world of router jitter
    away from a 1 s csv sample; subtracting it would invent a 43 ms WAN delay
    that no two adjacent measurements support. §7's invariant is preserved and
    made computable: the gateway still cannot raise the status — here it lowers
    the residual to nothing.
    """
    moment = int(END_OF_WINDOW.timestamp())
    settings = make_settings(
        tmp_path,
        "ts_iso,unixtime,target,rtt_ms\n"
        f"2026-09-17T17:11:00.000,{moment}.0,net,6.0\n"
        f"2026-09-17T17:11:00.000,{moment}.0,gw,3.0\n",
        config_text="RTT_WARN_MS=25.0\nRTT_EXCESS_MS=10.0\n",
    )
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "events.jsonl").write_text(
        '{"ts":"2026-09-17T17:11:00Z","kind":"probe","rtt_ms":500.0,"loss_pct":0.0,'
        '"media":"1000baseT full-duplex"}\n',
        encoding="utf-8",
    )
    cache = csvrollup.RollupCache()
    cache.refresh(settings, now_epoch=END_OF_WINDOW.timestamp())
    snap = snapshot.build_with_csv(settings, cache, END_OF_WINDOW)

    assert snap.status_inputs()["gw_rtt_ms"] == 500.0  # still carried, for display
    assert snap.status == "ok"
    assert snap.localise()["reading"]["numbers"]["excess"] == 3.0


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

    # The drill-in reads the fault and both thresholds from this one payload
    # rather than re-deciding the rule in JavaScript.
    localise = client.get("/api/localise").json()
    assert localise["thresholds"] == {"rtt_warn_ms": 25.0, "rtt_excess_ms": 10.0}
    assert "fault" in localise["reading"]
    assert "excess" in localise["reading"]["numbers"]
    # The history key is `series`, not `minutes`: past the recent window the rows
    # are hourly, and every row says how wide it is so the chart can draw both.
    net = localise["targets"]["net"]
    assert "minutes" not in net
    assert net["series"]
    assert {"bucket_s", "minute", "n", "loss_pct"} <= set(net["series"][0])
    summary = client.get("/api/summary").json()
    assert summary["status_inputs"]["forwarded_excess_ms"] == 10.0


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


def test_the_seed_reads_the_whole_file_unless_a_bound_is_given(tmp_path):
    """The 4 MiB tail quietly capped how far back the drill-in could see.

    The seed used to read the last 4 MiB (~12 h). That bound was right while the
    retention window was a day, but it also meant the panel stopped at a point it
    never named. The default is now the whole file — and a positive bound is
    still honoured, so "whole file" did not quietly turn every seed into a full
    read of a file that grows to gigabytes.
    """
    settings = make_settings(tmp_path, fixture_csv())
    assert settings.csv_tail_bytes == csvrollup.WHOLE_FILE
    assert settings.csv_retention_s == 259200.0

    base = _hour_aligned(int(END_OF_WINDOW.timestamp()) // 60 - 480)
    path = tmp_path / "long.csv"
    path.write_text(
        "ts_iso,unixtime,target,rtt_ms\n"
        + _minute_rows("net", base, 4000, lambda i: "5.0"),
        encoding="utf-8",
    )
    whole = csvrollup.read(
        path,
        csvrollup.Cursor(),
        tz="UTC",
        now_epoch=END_OF_WINDOW.timestamp(),
        retention_s=259200.0,
        initial_tail_bytes=csvrollup.WHOLE_FILE,
    )
    assert whole.window()["truncated"] is False
    assert whole.window()["window_bytes"] == path.stat().st_size
    assert whole.latest_of("net")["minute"] == base + 3999

    bounded = csvrollup.read(
        path,
        csvrollup.Cursor(),
        tz="UTC",
        now_epoch=END_OF_WINDOW.timestamp(),
        retention_s=259200.0,
        initial_tail_bytes=4096,
    )
    assert bounded.window()["truncated"] is True
    assert bounded.window()["window_bytes"] == 4096
    assert bounded.samples < whole.samples


def test_history_older_than_the_recent_window_is_folded_into_hours(tmp_path):
    """The drill-in reaches back days; past the recent hours it reads in hours.

    Eight hours of one-per-minute samples: the newest six stay per-minute and the
    first two fold into two hourly rows. The fold has to be exact — an hour's mean
    is the mean of its minutes — and each row carries `bucket_s`, so a reader can
    tell a quiet minute from a quiet hour. The x axis stays linear in time: the
    second hour opens 60 minutes after the first, and the newest minute row 60
    minutes after that, which is what lets one polyline draw both resolutions.
    """
    base = _hour_aligned(int(END_OF_WINDOW.timestamp()) // 60 - 480)
    path = tmp_path / "gw.csv"
    path.write_text(
        "ts_iso,unixtime,target,rtt_ms\n"
        + _minute_rows(
            "net", base, 480, lambda i: "10.0" if i < 60 else "20.0" if i < 120 else "30.0"
        ),
        encoding="utf-8",
    )
    rollup = csvrollup.read(
        path,
        csvrollup.Cursor(),
        tz="UTC",
        now_epoch=END_OF_WINDOW.timestamp(),
        retention_s=259200.0,
    )

    series = rollup.series("net")
    hours, minutes = series[:2], series[2:]
    assert len(hours) == 2 and len(minutes) == 360
    assert [row["bucket_s"] for row in hours] == [3600, 3600]
    assert [row["rtt_ms_avg"] for row in hours] == [10.0, 20.0]
    assert all(row["n"] == 60 for row in hours)
    assert {row["bucket_s"] for row in minutes} == {60}
    assert {row["rtt_ms_avg"] for row in minutes} == {30.0}
    assert hours[1]["minute"] - hours[0]["minute"] == 60
    assert minutes[0]["minute"] - hours[1]["minute"] == 60


def test_a_folded_hour_is_averaged_over_the_minutes_that_replied(tmp_path):
    """A lost minute must not be folded in as a quiet one.

    Half of the hour's minutes are loss. Averaging those in as zero latency would
    read the hour as 5 ms; over the minutes that replied it is 10 ms, which is
    `Bucket.as_dict`'s rule applied across minutes instead of across samples.
    """
    base = _hour_aligned(int(END_OF_WINDOW.timestamp()) // 60 - 480)
    path = tmp_path / "gw.csv"
    path.write_text(
        "ts_iso,unixtime,target,rtt_ms\n"
        + _minute_rows("net", base, 480, lambda i: "" if i % 2 else "10.0"),
        encoding="utf-8",
    )
    rollup = csvrollup.read(
        path,
        csvrollup.Cursor(),
        tz="UTC",
        now_epoch=END_OF_WINDOW.timestamp(),
        retention_s=259200.0,
    )

    hour = rollup.series("net")[0]
    assert hour["bucket_s"] == 3600
    assert hour["n"] == 60
    assert hour["loss"] == 30
    assert hour["loss_pct"] == 50.0
    assert hour["rtt_ms_avg"] == 10.0  # over the 30 that replied, not over 60


def test_the_retention_window_now_reaches_three_days(tmp_path):
    """24 h of retention was the other, harder cap on the drill-in's reach.

    A sample from 2.9 days ago is kept and one from 3.5 days ago is dropped, so
    the window is the three days it says it is rather than the day it used to be.
    """
    now = END_OF_WINDOW.timestamp()
    now_minute = int(now // 60)
    inside = now_minute - 4200  # ~2.9 days back
    outside = now_minute - 5000  # ~3.5 days back
    path = tmp_path / "gw.csv"
    path.write_text(
        "ts_iso,unixtime,target,rtt_ms\n"
        f"2026-09-17T00:00:00.000,{outside * 60}.0,net,9.0\n"
        + _minute_rows("net", inside, 60, lambda i: "5.0")
        + _minute_rows("net", now_minute - 60, 60, lambda i: "6.0"),
        encoding="utf-8",
    )
    rollup = csvrollup.read(
        path, csvrollup.Cursor(), tz="UTC", now_epoch=now, retention_s=259200.0
    )

    series = rollup.series("net")
    assert min(row["minute"] for row in series) == inside
    assert 9.0 not in [row["rtt_ms_avg"] for row in series]


def test_a_three_day_window_is_folded_to_a_readable_number_of_points(tmp_path):
    """The payload guard the longer seed needs: three days must cost ~a day's worth.

    Per minute over three days is ~4,300 points per target; folded it is ~430.
    The series is the drill-in's whole history, so if this ever stops holding, one
    page is carrying a hundred thousand numbers nobody can read.
    """
    now = END_OF_WINDOW.timestamp()
    base = _hour_aligned(int(now // 60) - 4200)
    path = tmp_path / "gw.csv"
    path.write_text(
        "ts_iso,unixtime,target,rtt_ms\n"
        + _minute_rows("net", base, 4200, lambda i: "5.0"),
        encoding="utf-8",
    )
    rollup = csvrollup.read(
        path, csvrollup.Cursor(), tz="UTC", now_epoch=now, retention_s=259200.0
    )

    series = rollup.series("net")
    assert len(series) < 500
    assert series[0]["bucket_s"] == 3600
    assert series[-1]["bucket_s"] == 60
