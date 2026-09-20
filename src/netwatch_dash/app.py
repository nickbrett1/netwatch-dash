"""The dashboard's HTTP surface.

**Phase 1: ``/healthz``.** It answers three questions the release is responsible
for (memo v3 §4): *what is this payload* (build-info.json, written at assembly
time), *is the netwatch installed here the one this release ships* (producer
digests), and *what does the config say about the health signals* — specifically
that gateway-ICMP alerting is off (schema §5, §7).

It reads no bulk data. ``/healthz`` is polled by a ``siteMonitor`` and must
answer cheaply and never 500, so every file it touches is small and every failure
is a *reported absence* rather than an exception: `event` files and the RTT CSV
are not opened here at all (schema §3 — the CSV is ~8.4 MB/day and must never be
parsed per request; its reader is a tailer + rollup, Phase 3).

The ``/api/…`` endpoints that read those files are not here yet. They arrive
with the readers, and the status below is what they will move.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from . import buildinfo
from .parse import derive_status, parse_config
from .settings import PROJECT, Settings

# The /api surface the readers will fill. Declared here so the gap is a list in
# one place rather than a thing to remember.
NOT_IMPLEMENTED = (
    "/api/summary",
    "/api/probe",
    "/api/link",
    "/api/speed",
    "/api/localise",
    "/api/incidents",
)

# The signals /healthz's status is derived from, and which of them nothing reads
# yet. Status is never guessed at: an input that is not read is absent, and
# `derive_status` answers "unknown" for a set of inputs it does not have.
UNREAD = (
    "loss_pct, media/link and probe age: read from events.jsonl (Phase 2)",
    (
        "forwarded-path RTT and en0 error counters: read from the "
        "gateway_rtt.csv rollup (Phase 3)"
    ),
)


def _read_config(path: Path) -> tuple[dict[str, str], str | None]:
    """Whitelisted config keys, or an empty mapping and the reason why.

    `parse_config` drops `NTFY_TOPIC` by not listing it (schema §5), so a
    capability token in the config file cannot reach a response.
    """
    if not path.is_file():
        return {}, "not present"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    return parse_config(text), None


def healthz_body(settings: Settings) -> dict:
    """The whole of ``/healthz``, as a plain function so it is testable alone."""
    info = buildinfo.load(settings.build_info_path)
    producers = buildinfo.producer_report(info, settings.home)
    config, config_error = _read_config(settings.config_path)

    reasons: list[str] = []
    if not info.found:
        reasons.append(f"this payload cannot report its own build: {info.error}")
    if producers["skew"]:
        reasons.append(
            "installed producers differ from the ones this release ships: "
            + ", ".join(producers["skew"])
        )
    reasons.extend(f"not read yet — {item}" for item in UNREAD)

    return {
        # The health signal itself (schema §7). Every input is absent until the
        # readers land, so this is "unknown" rather than a reassuring "ok" —
        # which is the whole point of never coercing unknown.
        "status": derive_status(
            loss_pct=None,
            link_ok=None,
            forwarded_rtt_ms=None,
            forwarded_warn_ms=0.0,
            probe_age_s=None,
            probe_stale_s=0.0,
        ),
        "status_reason": reasons,
        "version": info.get("version"),
        "commit": info.get("commit"),
        "build": info.as_dict(),
        "producers": producers,
        "config": config,
        "config_error": config_error,
        # Owed to schema §6: a producer/schema separation must become a visible
        # number. Nothing parses bulk data yet, so it is null with a reason
        # rather than 0, which would claim a clean run that never happened.
        "data": {"drift_count": None, "reason": "no reader implemented yet"},
        "not_implemented": list(NOT_IMPLEMENTED),
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(
        title=PROJECT,
        summary="read-only view of the netwatch data files",
    )
    app.state.settings = settings or Settings.from_env()

    @app.get("/healthz")
    def healthz() -> dict:
        return healthz_body(app.state.settings)

    return app
