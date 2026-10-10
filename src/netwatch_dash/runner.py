"""Asking launchd to run a daily check now, on a human's say-so.

The dashboard is a read-only view (memo v3 D5): it computes no alerts, sends
none, and never runs a producer. This module is the one exception, and it is
deliberately the narrowest exception available — it does **not** run
``netwatch speed`` itself, it asks **launchd** to re-run the job that already
owns that work.

That choice is the whole design. A second implementation of "run a speed test"
would have to reproduce the job's environment — ``SPEEDTEST_SERVER_IDS``,
``LAN_PEER_MODE``, the interpreter the plist pins — and would then be a second
thing to keep in step with the job it is pretending to be. A run it produced
would not be the run the schedule produces, which is exactly the assurance the
button is supposed to give. ``launchctl kickstart`` re-runs the *same* job the
calendar entry runs, with the same program, the same environment and the same
logging, so what it starts is indistinguishable from the 04:00 run. It is also
what ``deploy/install-host.sh`` already does to adopt a replaced producer, so
there is no new mechanism here — only an already-existing one made reachable.

Because this is the only write on an otherwise read-only surface, it is fenced
three ways:

* **The job table is fixed, and never assembled from a request.** ``CHECKS``
  maps the dashboard's own names to the two labels the installer writes. An
  endpoint that could name any job would be a way to run anything on the host.
* **A request must carry a header a cross-origin form cannot set.**
  ``ACTION_HEADER`` forces a CORS preflight, which the app never answers, so a
  page the reader merely visited cannot start a bandwidth test behind their
  back — a real concern on a surface with no authentication to fall back on.
* **A per-check cooldown bounds the rate.** A check saturates the link it is
  measuring, so a retry loop would corrupt the very numbers the button exists to
  refresh; the cooldown is short enough to feel like a button and long enough
  that the run it can interfere with is over.

Every failure — no ``launchctl``, a job that is not loaded, a timeout — is a
*reported absence* with a reason, never a raised exception, for the same reason
``/healthz`` never raises: a service that cannot do a thing should say so, not
500.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Callable

# The two daily bandwidth checks, and the launchd jobs that own them. Keys are
# the dashboard's own names (the section headings' language); values are the
# labels ``deploy/install-host.sh`` installs. Fixed here, never derived from a
# request.
CHECKS: dict[str, str] = {
    "speed": "com.nickbrett.netwatch.speed",
    "lan-speed": "com.nickbrett.netwatch.lan-speed",
}

# A cross-origin page can only set this header by first asking with a CORS
# preflight, which this app never answers — so requiring it is what keeps a
# third-party tab from starting runs. See the module docstring.
ACTION_HEADER = "X-Netwatch-Rerun"
ACTION_HEADER_VALUE = "1"

# launchctl either accepts the kickstart in milliseconds or fails; ten seconds is
# already far past that and exists only so a wedged launchd cannot hang a request.
KICKSTART_TIMEOUT_S = 10.0
# A speed test saturates the link for tens of seconds. Two minutes is long enough
# that the previous run is done before the next may start, and short enough that
# a reader who genuinely wants a second look is not meaningfully blocked.
DEFAULT_COOLDOWN_S = 120.0


def _kickstart(
    label: str, *, uid: int | None = None, timeout_s: float = KICKSTART_TIMEOUT_S
) -> tuple[bool, str]:
    """Ask launchd to (re)start one job. Never raises; returns ``(ok, detail)``.

    ``-k`` is "kill if running, then start": the daily job is not running at the
    moment someone clicks, so this is a start, but using ``-k`` keeps it correct
    if a click lands on top of a scheduled run rather than queueing a second.
    """
    exe = shutil.which("launchctl")
    if exe is None:
        return False, "launchctl is not on this host (this is not macOS)"
    target = f"gui/{os.getuid() if uid is None else uid}/{label}"
    try:
        proc = subprocess.run(
            [exe, "kickstart", "-k", target],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"launchctl kickstart {target} timed out after {timeout_s:.0f}s"
    except OSError as exc:  # e.g. not executable, or a bad gui/ domain
        return False, f"could not run launchctl: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return False, f"launchctl kickstart {target} failed: {detail or proc.returncode}"
    return True, f"launchctl kickstart {target}"


class Runner:
    """Re-runs the daily checks through launchd, rate-limited per check.

    ``run`` and ``clock`` are injectable so the behaviour is testable without a
    Mac: the tests drive a fake command and a fake clock, and never shell out.
    """

    def __init__(
        self,
        *,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
        run: Callable[[str], tuple[bool, str]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cooldown_s = cooldown_s
        self._run = run or _kickstart
        self._clock = clock
        self._last: dict[str, float] = {}

    def cooldown_remaining(self, check: str) -> float:
        """Seconds before ``check`` may be started again (0 when it may now)."""
        last = self._last.get(check)
        if last is None:
            return 0.0
        return max(0.0, self._cooldown_s - (self._clock() - last))

    def rerun(self, check: str) -> dict:
        """Start ``check`` now, or say why not. Never raises.

        A result is ``{check, label, started, detail, cooldown_s}``; a refusal
        also carries ``reason`` (and, when the reason is the cooldown,
        ``retry_after_s``) so the page has a sentence to show rather than a code
        to guess at.
        """
        label = CHECKS.get(check)
        if label is None:
            return {
                "check": check,
                "started": False,
                "reason": f"no such check: {check}; try one of {', '.join(sorted(CHECKS))}",
            }

        remaining = self.cooldown_remaining(check)
        if remaining > 0:
            return {
                "check": check,
                "label": label,
                "started": False,
                "reason": f"a run was started under {self._cooldown_s:.0f}s ago",
                "retry_after_s": round(remaining, 1),
                "cooldown_s": self._cooldown_s,
            }

        started, detail = self._run(label)
        # Record the attempt whether or not launchctl took it: the cooldown is
        # about bounding how often this host is asked, and a failure that repeats
        # on every click is a failure worth making the page show once, not a
        # prompt to hammer launchctl.
        self._last[check] = self._clock()
        result = {
            "check": check,
            "label": label,
            "started": started,
            "detail": detail,
            "cooldown_s": self._cooldown_s,
        }
        if not started:
            result["reason"] = detail
        return result
