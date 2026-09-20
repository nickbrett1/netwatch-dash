"""Parser tests against the real captured fixtures (schema/netwatch-data.md)."""

from pathlib import Path

import pytest

from netwatch_dash.parse import (
    BEYOND_ROUTER,
    PATH_CEILING,
    Drift,
    derive_status,
    iface_error_deltas,
    media_is_gigabit,
    parse_config,
    parse_csv_line,
    parse_event,
    parse_iferrs,
    parse_last_alert,
    parse_streak,
    path_excess_ms,
    path_fault,
    speed_is_stale,
)

FIX = Path(__file__).parent / "fixtures"


def _lines(name):
    return [ln for ln in (FIX / name).read_text().splitlines() if ln.strip()]


def test_probe_lines_parse_and_are_recognised():
    drift = Drift()
    recs = [parse_event(ln, drift) for ln in _lines("events_sample.jsonl")]
    assert all(r is not None for r in recs)
    # the 2026-09-20 line carries the six fields added after memo v2
    latest = next(r for r in recs if "peer_ms" in r)
    for f in ("link", "rx_mbps", "saturated", "peer", "peer_ms", "peer_loss_pct"):
        assert f in latest
    # known fields are not drift
    assert drift.unknown_fields == []
    assert drift.bad_lines == 0


def test_unknown_field_is_counted_not_dropped():
    drift = Drift()
    rec = parse_event('{"ts":"x","kind":"probe","rtt_ms":1.0,"brand_new":7}', drift)
    assert rec is not None
    assert "brand_new" in drift.unknown_fields
    assert drift.count == 1


def test_unparseable_line_is_counted():
    drift = Drift()
    assert parse_event("not json", drift) is None
    assert drift.bad_lines == 1


def test_csv_markers_targets_and_loss():
    drift = Drift()
    rows = [parse_csv_line(ln, drift) for ln in _lines("gateway_rtt_sample.csv")]
    kinds = [k for k, _ in rows]
    assert kinds[0] == "header"
    assert "marker" in kinds and "sample" in kinds
    targets = {r["target"] for k, r in rows if k == "sample"}
    assert targets <= {"gw", "wire", "wl", "net"}
    assert drift.unknown_markers == [] and drift.unknown_targets == []
    # empty rtt_ms means loss
    assert parse_csv_line("t,1,gw,", Drift())[1]["loss"] is True


def test_unknown_marker_and_target_are_counted():
    drift = Drift()
    parse_csv_line("# weird 2026-09-20T00:00:00 x", drift)
    parse_csv_line("t,1,mars,1.0", drift)
    assert drift.unknown_markers == ["weird"]
    assert drift.unknown_targets == ["mars"]


def test_media_unknown_is_not_gigabit():
    assert media_is_gigabit("1000baseT full-duplex flow-control energ") is True
    assert media_is_gigabit("autoselect") is False
    assert media_is_gigabit(None) is False


def test_config_whitelist_never_exposes_ntfy():
    keys = set(_lines("config_keys.txt"))
    assert "NTFY_TOPIC" in keys  # it really is in the host's file
    parsed = parse_config(
        "NTFY_TOPIC=secretvalue\nRTT_WARN_MS=4.0\nNOTIFY_MACOS=true\n"
    )
    assert "NTFY_TOPIC" not in parsed
    assert "NOTIFY_MACOS" not in parsed
    assert parsed["RTT_WARN_MS"] == "4.0"


def test_config_reads_rtt_alert_but_never_the_topic():
    """`RTT_ALERT` is read so /healthz can say RTT alerting is off (§5, §7)."""
    keys = set(_lines("config_keys.txt"))
    assert "RTT_ALERT" in keys  # arrived 2026-09-20; the fixture tracks the host
    parsed = parse_config(
        'RTT_ALERT=off\nNTFY_TOPIC="nick-netwatch-verysecret"\n'
        "SAT_MBPS=100\nNOTIFY_MACOS=true\n"
    )
    assert parsed == {"RTT_ALERT": "off"}
    assert "verysecret" not in repr(parsed)  # the token value cannot leak


def test_state_files():
    assert parse_streak((FIX / "rtt_streak.txt").read_text()) == 2
    alerts = parse_last_alert((FIX / "last_alert.txt").read_text())
    assert set(alerts) == {"loss", "rtt", "dl", "ul"}
    assert alerts["rtt"] > 0


def test_the_residual_cancels_the_router_out():
    """The point of the residual: the router's own ICMP cannot inflate it.

    Measured on mac-studio 2026-09-20, the same path read 5.8 ms beside a 2.5 ms
    gateway and 11.3 ms beside a 33 ms one — the router moved 30 ms and the WAN
    leg did not move at all. An absolute threshold sees that as signal; the
    residual sees it as nothing.
    """
    assert path_excess_ms(5.8, 2.5) == pytest.approx(3.3)
    assert path_excess_ms(11.3, 33.0) == pytest.approx(-21.7)  # the router is slow
    # Both ends or no answer: a residual is a subtraction.
    assert path_excess_ms(5.8, None) is None
    assert path_excess_ms(None, 2.5) is None


def test_path_fault_names_the_two_faults():
    """Two conditions, because they implicate different hops."""
    # Beyond the router: the WAN leg carries the delay.
    assert (
        path_fault(forwarded_rtt_ms=30.0, gateway_rtt_ms=3.0, ceiling_ms=25.0,
                   excess_ms=10.0)
        == BEYOND_ROUTER
    )
    # The whole path is slow including the router, but not *beyond* it — the
    # ceiling catches what the residual deliberately forgives.
    assert (
        path_fault(forwarded_rtt_ms=38.0, gateway_rtt_ms=50.0, ceiling_ms=25.0,
                   excess_ms=10.0)
        == PATH_CEILING
    )
    # A quiet path at the host's real numbers is not a fault at all.
    assert (
        path_fault(forwarded_rtt_ms=6.6, gateway_rtt_ms=3.9, ceiling_ms=25.0,
                   excess_ms=10.0)
        is None
    )


def test_a_missing_threshold_is_no_comparison_never_a_comparison_with_zero():
    """§6: an absent threshold is reported as absent, not read as 0 ms."""
    assert path_fault(forwarded_rtt_ms=6.0, gateway_rtt_ms=3.0,
                      ceiling_ms=None, excess_ms=None) is None
    # Each threshold works on its own: the ceiling needs no gateway.
    assert path_fault(forwarded_rtt_ms=30.0, gateway_rtt_ms=None,
                      ceiling_ms=25.0, excess_ms=None) == PATH_CEILING
    # And with no gateway there is no residual, so the excess cannot fire —
    # which must not be mistaken for the residual being small.
    assert path_fault(forwarded_rtt_ms=30.0, gateway_rtt_ms=None,
                      ceiling_ms=None, excess_ms=10.0) is None
    assert path_fault(forwarded_rtt_ms=None, gateway_rtt_ms=3.0,
                      ceiling_ms=25.0, excess_ms=10.0) is None


def test_a_router_spike_makes_the_status_quieter_not_louder():
    """§7's invariant, now with a mechanism rather than by ignoring the input."""
    base = {
        "loss_pct": 0.0,
        "link_ok": True,
        "forwarded_rtt_ms": 20.0,
        "forwarded_warn_ms": 25.0,
        "forwarded_excess_ms": 10.0,
        "forwarded_gw_rtt_ms": 3.0,
        "probe_age_s": 10.0,
        "probe_stale_s": 600.0,
    }
    assert derive_status(**base) == "warn"  # 17 ms beyond the router
    # The router's reply inflating to 20 ms removes the excess entirely: same
    # forwarded path, quieter status. A signal that can only ever be raised by a
    # gateway spike is the bug this replaces.
    assert derive_status(**{**base, "forwarded_gw_rtt_ms": 20.0}) == "ok"


def test_the_display_gateway_reading_is_still_never_an_input():
    """The old invariant, kept literally: `gw_rtt_ms` moves nothing at all."""
    base = {
        "loss_pct": 0.0,
        "link_ok": True,
        "forwarded_rtt_ms": 6.0,
        "forwarded_warn_ms": 25.0,
        "forwarded_excess_ms": 10.0,
        "forwarded_gw_rtt_ms": 3.0,
        "probe_age_s": 10.0,
        "probe_stale_s": 600.0,
    }
    assert derive_status(**base) == "ok"
    assert derive_status(**base, gw_rtt_ms=500.0) == "ok"


def test_status_uses_loss_and_link_not_gateway_rtt():
    base = {
        "loss_pct": 0.0,
        "link_ok": True,
        "forwarded_rtt_ms": 5.0,
        "forwarded_warn_ms": 15.0,
        "probe_age_s": 10.0,
        "probe_stale_s": 600.0,
    }
    assert derive_status(**base) == "ok"
    # a terrible gateway RTT must not change the status (schema §7)
    assert derive_status(**base, gw_rtt_ms=500.0) == "ok"
    # loss and a downgraded link are what move it
    assert derive_status(**{**base, "loss_pct": 1.0}) == "crit"
    assert derive_status(**{**base, "link_ok": False}) == "crit"
    # stale probe is crit, not ok — a dead recorder must not look healthy
    assert derive_status(**{**base, "probe_age_s": 601.0}) == "crit"
    # unknown is never coerced to ok
    assert (
        derive_status(
            loss_pct=None,
            link_ok=None,
            forwarded_rtt_ms=None,
            forwarded_warn_ms=15.0,
            probe_age_s=None,
            probe_stale_s=600.0,
        )
        == "unknown"
    )


def test_iferrs_marker_parses_and_is_recognised():
    """§8: the producer's new marker must not show up as drift."""
    drift = Drift()
    markers = []
    for ln in _lines("iferrs_sample.csv"):
        res = parse_csv_line(ln, drift)
        if res and res[0] == "marker":
            markers.append(res[1])
    assert len(markers) == 2
    assert drift.unknown_markers == []
    assert drift.unknown_fields == []
    assert drift.count == 0
    first = parse_iferrs(markers[0], drift)
    assert first == {
        "iface": "en0",
        "ierrs": 0,
        "oerrs": 0,
        "coll": 0,
        "rx_bytes": 93364822283,
        "tx_bytes": 21108729333,
    }


def test_iferrs_deltas_are_reset_aware():
    """Counter decrease is a reset -> 0, never a negative rate (§8)."""
    a = {"ierrs": 3, "oerrs": 1, "coll": 0, "rx_bytes": 1000, "tx_bytes": 500}
    b = {"ierrs": 5, "oerrs": 4, "coll": 2, "rx_bytes": 1600, "tx_bytes": 900}
    assert iface_error_deltas(a, b) == {
        "ierrs": 2,
        "oerrs": 3,
        "coll": 2,
        "rx_bytes": 600,
        "tx_bytes": 400,
    }
    # interface bounced: counters restarted near zero
    c = {"ierrs": 1, "oerrs": 0, "coll": 0, "rx_bytes": 20, "tx_bytes": 10}
    assert iface_error_deltas(b, c) == {
        "ierrs": 0,
        "oerrs": 0,
        "coll": 0,
        "rx_bytes": 0,
        "tx_bytes": 0,
    }
    # no baseline yet -> no invented errors
    assert iface_error_deltas(None, b) == dict.fromkeys(
        ("ierrs", "oerrs", "coll", "rx_bytes", "tx_bytes"), 0
    )


def test_iferrs_payload_unknown_key_is_counted_not_dropped():
    drift = Drift()
    m = {
        "type": "iferrs",
        "raw": '# iferrs 2026-09-20T11:44:34 {"iface":"en0","rx_bytes":1,"mtu":1500}',
    }
    payload = parse_iferrs(m, drift)
    assert payload is not None and payload["iface"] == "en0"
    assert drift.unknown_fields == ["iferrs.mtu"]


def test_iferrs_malformed_payload_is_a_bad_line_not_a_crash():
    drift = Drift()
    assert (
        parse_iferrs({"type": "iferrs", "raw": "# iferrs 2026-09-20T11:44:34"}, drift)
        is None
    )
    assert parse_iferrs({"type": "iferrs", "raw": "# iferrs ts {oops"}, drift) is None
    assert drift.bad_lines == 2


def test_every_marker_the_producer_writes_is_recognised():
    """The live log's vocabulary, pinned.

    `# stop` was found only by running the parser over the whole live log
    (510,698 lines, drift == 2, both `stop`). Markers are cheap to list and
    expensive to miss: an unlisted one shows up as drift forever.
    """
    produced = {
        "start",
        "stop",
        "loss",
        "burst_start",
        "burst_end",
        "link_change",
        "orbi",
        "iferrs",
    }
    drift = Drift()
    for marker in produced:
        parse_csv_line(f"# {marker} 2026-09-20T11:44:34 extra", drift)
    assert drift.unknown_markers == []
    assert drift.count == 0
    # a genuinely new marker still counts
    drift = Drift()
    parse_csv_line("# dhcp_renew 2026-09-20T11:44:34", drift)
    assert drift.unknown_markers == ["dhcp_renew"]


def test_speed_staleness_is_three_valued():
    """A missing age or threshold is unknown, not "not stale"."""
    assert speed_is_stale(10.0, 100.0) is False
    assert speed_is_stale(200.0, 100.0) is True
    assert speed_is_stale(200.0, 100.0) is True
    assert speed_is_stale(None, 100.0) is None
    assert speed_is_stale(10.0, None) is None


def test_throughput_only_counts_a_current_measurement():
    """Throughput is a health signal (§7), but a stale reading is not a fact."""
    base = {
        "loss_pct": 0.0,
        "link_ok": True,
        "forwarded_rtt_ms": None,
        "forwarded_warn_ms": 15.0,
        "probe_age_s": 10.0,
        "probe_stale_s": 600.0,
        "dl_mbps": 10.0,
        "ul_mbps": 500.0,
        "dl_warn_mbps": 100.0,
        "ul_warn_mbps": 50.0,
        "speed_stale_s": 1000.0,
        "speed_age_s": 10.0,
    }
    assert derive_status(**base) == "warn"  # download is below its threshold
    assert derive_status(**{**base, "speed_age_s": 99999.0}) == "ok"  # too old to say
    assert derive_status(**{**base, "dl_warn_mbps": None}) == "ok"  # no threshold stated
    assert derive_status(**{**base, "dl_mbps": None}) == "ok"  # no measurement either
