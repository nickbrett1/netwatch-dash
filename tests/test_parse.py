"""Parser tests against the real captured fixtures (schema/netwatch-data.md)."""

from pathlib import Path

from netwatch_dash.parse import (
    Drift,
    derive_status,
    media_is_gigabit,
    parse_config,
    parse_csv_line,
    parse_event,
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
    parsed = parse_config("NTFY_TOPIC=secretvalue\nRTT_WARN_MS=4.0\nNOTIFY_MACOS=true\n")
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
        "loss_pct": 0.0, "link_ok": True, "forwarded_rtt_ms": 5.0,
        "forwarded_warn_ms": 15.0, "probe_age_s": 10.0, "probe_stale_s": 600.0,
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
            loss_pct=None, link_ok=None, forwarded_rtt_ms=None,
            forwarded_warn_ms=15.0, probe_age_s=None, probe_stale_s=600.0,
        )
        == "unknown"
    )
