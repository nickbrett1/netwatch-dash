"""Where the dashboard reads from, and where it listens.

Every value is an environment variable with a working default, and the defaults
are the real host paths the producers write. ``deploy/install-host.sh`` writes
``$HOME/.config/netwatch-dash/env`` and the launchd job inherits it, so a host is
configured by the payload's own installer rather than by editing code — and a
checkout, with nothing set, still reads the same files the installed producers
write instead of an empty directory that reports healthy.

Nothing here is required. A setting that is absent falls back to the host
default, and a file that is absent is reported *as absent* by the endpoint that
needed it, never as a zero or an empty timeline (schema §6).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# The distribution name, and the directory name inside the payload
# (`share/netwatch-dash/`). Spelled here once so the two cannot drift.
PROJECT = "netwatch-dash"

# Loopback by default. The host binds the tailnet address explicitly
# (deploy/install-host.sh writes NETWATCH_DASH_BIND=100.77.144.14:8791). The
# service has no authentication, so the default has to be the one that cannot be
# reached from off-host — a default of 0.0.0.0 would publish the dashboard's
# contents to whatever network the machine happens to be on, including a café's.
DEFAULT_BIND = "127.0.0.1:8791"
DEFAULT_TZ = "America/New_York"


def _env_path(env: Mapping[str, str], key: str, default: Path) -> Path:
    value = env.get(key)
    return Path(value).expanduser() if value else default


def _float_env(env: Mapping[str, str], key: str, default: float) -> float:
    """A float setting, with an unparseable value falling back to the default.

    These are freshness windows, not credentials: a typo in a window should not
    stop the dashboard from starting, and it must not be silently read as 0
    either — 0 would make every measurement stale and paint the tile red.
    """
    value = env.get(key)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """The dashboard's environment, resolved once at start-up."""

    bind: str
    state_dir: Path
    csv_path: Path
    config_path: Path
    tz: str
    home: Path
    # An explicit build-info.json, or None to discover it (see buildinfo.find).
    build_info_path: Path | None = None
    # How old a measurement may be before it stops speaking for *now*. Defaults
    # are multiples of the producers' own cadences: a probe runs every 300 s, so
    # one missed run is jitter and three is 15 minutes of nothing.
    probe_stale_s: float = 900.0
    speed_stale_s: float = 172800.0  # two missed daily runs

    @property
    def events_path(self) -> Path:
        return self.state_dir / "events.jsonl"

    @property
    def streak_path(self) -> Path:
        return self.state_dir / ".rtt_streak"

    @property
    def last_alert_path(self) -> Path:
        return self.state_dir / ".last_alert"

    @property
    def host_port(self) -> tuple[str, int]:
        """Split ``bind`` into ``(host, port)``.

        A bind that is not ``host:port`` is a start-up failure, not a
        request-time one: it is better said loudly once at start than swallowed
        into a 500 on every request. ``rpartition`` (not ``split``) so an IPv6
        literal keeps its colons.
        """
        host, sep, port = self.bind.rpartition(":")
        if not sep or not host or not port.isdigit():
            raise ValueError(
                f"NETWATCH_DASH_BIND is not host:port: {self.bind!r}"
            )
        return host, int(port)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        home = Path(
            env.get("NETWATCH_DASH_HOME") or env.get("HOME") or "~"
        ).expanduser()
        build_info = env.get("NETWATCH_DASH_BUILD_INFO")
        return cls(
            bind=env.get("NETWATCH_DASH_BIND") or DEFAULT_BIND,
            state_dir=_env_path(
                env, "NETWATCH_DASH_STATE", home / ".local/state/netwatch"
            ),
            csv_path=_env_path(
                env, "NETWATCH_DASH_GWCSV", home / "netwatch/gateway_rtt.csv"
            ),
            config_path=_env_path(
                env, "NETWATCH_DASH_CONFIG", home / ".config/netwatch/config"
            ),
            tz=env.get("NETWATCH_DASH_TZ") or DEFAULT_TZ,
            home=home,
            build_info_path=Path(build_info).expanduser() if build_info else None,
            probe_stale_s=_float_env(env, "NETWATCH_DASH_PROBE_STALE_S", 900.0),
            speed_stale_s=_float_env(env, "NETWATCH_DASH_SPEED_STALE_S", 172800.0),
        )
