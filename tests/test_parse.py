"""Parser tests against the real captured fixtures (schema/netwatch-data.md)."""

from pathlib import Path

from netwatch_dash.parse import (
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


def test_state_files():
    assert parse_streak((FIX / "rtt_streak.txt").read_text()) == 2
    alerts = parse_last_alert((FIX / "last_alert.txt").read_text())
    assert set(alerts) == {"loss", "rtt", "dl", "ul"}
    assert alerts["rtt"] > 0


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
