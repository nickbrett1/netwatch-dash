"""The dashboard's HTTP surface.

Read-only, and deliberately narrow. It answers what the release is responsible
for (`/healthz`) and what the producers wrote (the `/api/…` endpoints), and it
computes no alerts and sends none (memo v3 D5) — alerting stays `netwatch`'s job.

Three rules hold across every endpoint:

* **Never raise over a file.** A missing, empty or malformed file is a reported
  absence with a reason, never a 500. `/healthz` is a `siteMonitor` target, so a
  crash there would show as the Mac being down.
* **The CSV is never parsed per request** (schema §3) — ~8.4 MB/day, ~3 GB/yr.
  `/healthz` and the Phase 2 endpoints read `events.jsonl`'s bounded tail, which
  is cheap and rotation-proof; the CSV's reader is a tailer plus a rollup, and
  until it exists the inputs that live in it are named as not-read.
* **Unknown is never a good value** (schema §6). An absent field is `null`, an
  ignorable one is called out, and the drift the parser counts is returned rather
  than swallowed.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Query

from . import buildinfo, csvrollup, snapshot
from .settings import PROJECT, Settings

# How many records a collection endpoint returns when nobody asked for a number.
PROBE_DEFAULT_LIMIT = 60  # a 5-hour window at the 300 s cadence
SPEED_DEFAULT_LIMIT = 20  # a few weeks of daily runs


def healthz_body(
    settings: Settings, now: datetime | None = None, csv: object | None = None
) -> dict:
    """The whole of ``/healthz``, as a plain function so it is testable alone.

    `csv` is an already-built snapshot when the caller has one (the app owns the
    rollup cache); otherwise the state is read here, without the csv — which is
    what the smoke gate and the unit tests want.
    """
    snap = csv if isinstance(csv, snapshot.Snapshot) else snapshot.build(settings, now)
    info = buildinfo.load(settings.build_info_path)
    producers = buildinfo.producer_report(info, settings.home)

    reasons = list(snap.status_reason())
    if not info.found:
        reasons.append(f"this payload cannot report its own build: {info.error}")
    if producers["skew"]:
        reasons.append(
            "installed producers differ from the ones this release ships: "
            + ", ".join(producers["skew"])
        )
    if producers["missing"]:
        reasons.append(
            "producers this release ships are not installed here: "
            + ", ".join(producers["missing"])
        )

    drift = snap.events.drift
    return {
        "status": snap.status,
        "status_reason": reasons,
        "version": info.get("version"),
        "commit": info.get("commit"),
        "build": info.as_dict(),
        "producers": producers,
        "config": snap.config,
        "config_error": snap.config_error,
        # Owed to schema §6: a producer/schema separation has to become a visible
        # number rather than a silently missing panel. Both files are parsed now,
        # so both drifts are counted — a clean run of either is the invariant.
        "data": {
            "drift_count": drift.count + snap.csv.drift.count,
            "drift": {
                "events.jsonl": drift.as_dict(),
                "gateway_rtt.csv": snap.csv.drift.as_dict(),
            },
        },
        "sources": snap.sources(),
        "not_implemented": dict(sorted(snapshot.NOT_IMPLEMENTED.items())),
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # The seed runs here, at start-up, and not on the first request: it reads
        # up to `csv_tail_bytes` of a file that grows ~8.4 MB/day (schema §3).
        app.state.csv.prewarm(app.state.settings)
        yield

    app = FastAPI(
        title=PROJECT,
        summary="read-only view of the netwatch data files",
        lifespan=lifespan,
    )
    app.state.settings = settings or Settings.from_env()
    # One tailer for the process, not one per request: the cursor is the whole
    # reason the csv is affordable (schema §3).
    app.state.csv = csvrollup.RollupCache(app.state.settings.csv_tail_bytes)

    def snap(now: datetime | None = None) -> snapshot.Snapshot:
        return snapshot.build_with_csv(app.state.settings, app.state.csv, now)

    @app.get("/healthz")
    def healthz() -> dict:
        return healthz_body(app.state.settings, csv=snap())

    @app.get("/api/summary")
    def api_summary() -> dict:
        """Everything the tile needs, from one read."""
        return snap().summary()

    @app.get("/api/probe")
    def api_probe(limit: int = Query(PROBE_DEFAULT_LIMIT, ge=1, le=1000)) -> dict:
        return snap().collection("probe", limit)

    @app.get("/api/speed")
    def api_speed(limit: int = Query(SPEED_DEFAULT_LIMIT, ge=1, le=1000)) -> dict:
        return snap().collection("speed", limit)

    @app.get("/api/link")
    def api_link() -> dict:
        """The link rate now, and every renegotiation in the window."""
        return snap().link()

    @app.get("/api/localise")
    def api_localise() -> dict:
        """Which hop the numbers implicate: forwarded path, or router CPU."""
        return snap().localise()

    @app.get("/api/incidents")
    def api_incidents() -> dict:
        """Loss, bursts and the last alert the producer raised."""
        return snap().incidents()

    return app
