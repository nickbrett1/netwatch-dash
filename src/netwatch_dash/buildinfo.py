"""What this payload is, read back out of the payload.

``scripts/build-payload.sh`` writes ``share/netwatch-dash/build-info.json`` at
assembly time: the release version, the commit, the bundled interpreter, the
resolved dependency versions, and the sha256 of every producer this release
ships. Nothing here is compiled into the code, because a constant is true in
exactly one of the two places this code runs — a wheel unpacked on a host, or a
checkout in CI — and a lie in the other (docs/release-payload.md).

Two rules from the data contract apply unchanged (schema §6):

* a file that cannot be read is reported as **absent, with a reason** — never as
  an empty mapping that reads like "no producers shipped";
* an answer this process cannot give is **never invented**. Which producers were
  looked for, and where, is part of the answer, not a detail.

The producer comparison is what makes "which netwatch is this host running?"
answerable: the payload's copy of a producer is the reference, the host's copy is
what is running, and the digest is the only honest way to compare two files that
have the same name and no version string.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .settings import PROJECT

BUILD_INFO_RELPATH = Path("share") / PROJECT / "build-info.json"

# Where a shipped producer is installed on a host, relative to $HOME. From
# producers/README.md ("origin on mac-studio"): the two directories the
# producers live in are `~/netwatch/` and `~/.local/bin/`, split by nothing more
# principled than which one the author happened to use.
INSTALL_PATHS = {
    "gwping.py": "netwatch/gwping.py",
    "netwatch": ".local/bin/netwatch",
    "flapwatch": ".local/bin/flapwatch",
    "check_link.sh": "netwatch/check_link.sh",
    "probe_icmp_vs_tcp.py": "netwatch/probe_icmp_vs_tcp.py",
    "probe_lan_tcp.py": "netwatch/probe_lan_tcp.py",
}


def installed_path(name: str, home: Path) -> Path:
    """The path a producer is installed at on this host.

    A name this table does not know is still looked for, under ``~/netwatch/``,
    rather than skipped: a new producer is exactly the case where "no comparison
    happened" must not look like "no drift".
    """
    return home / INSTALL_PATHS.get(name, f"netwatch/{name}")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def find(explicit: Path | None = None) -> Path | None:
    """Locate ``build-info.json``, or return None.

    ``explicit`` (from ``NETWATCH_DASH_BUILD_INFO``) wins, and is returned even
    if it does not exist: the point of naming a file is that the answer is about
    *that* file, and silently falling back to a discovered one would answer a
    question nobody asked.

    Otherwise the payload's own layout is searched. The bundled interpreter is
    installed at ``<payload-root>/python``, so ``sys.prefix``'s parent is the
    payload root; a source checkout is found relative to this file. Both are
    tried because the same code runs in both places, and neither requires the
    other to be absent first.
    """
    if explicit is not None:
        return explicit
    candidates = [
        Path(sys.prefix).parent / BUILD_INFO_RELPATH,
        Path(sys.prefix) / BUILD_INFO_RELPATH,
        Path(__file__).resolve().parents[2] / BUILD_INFO_RELPATH,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


@dataclass
class BuildInfo:
    """The payload's self-report, and whether it could be read."""

    path: Path | None
    data: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def found(self) -> bool:
        return self.error is None

    def get(self, key: str, default: object = None) -> object:
        return self.data.get(key, default)

    def as_dict(self) -> dict:
        return {
            "found": self.found,
            "path": str(self.path) if self.path else None,
            "error": self.error,
            "name": self.data.get("name"),
            "version": self.data.get("version"),
            "tag": self.data.get("tag"),
            "commit": self.data.get("commit"),
            "target": self.data.get("target"),
            "built_at": self.data.get("built_at"),
            "python": self.data.get("python"),
            "wheel": self.data.get("wheel"),
            "dependencies": self.data.get("dependencies") or {},
        }


def load(explicit: Path | None = None) -> BuildInfo:
    """Read the payload's build-info.json. Never raises."""
    path = find(explicit)
    if path is None:
        return BuildInfo(path=None, error="build-info.json not found")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return BuildInfo(path=path, error=f"{type(exc).__name__}: {exc}")
    try:
        data = json.loads(text)
    except ValueError as exc:
        return BuildInfo(path=path, error=f"not valid JSON: {exc}")
    if not isinstance(data, dict):
        return BuildInfo(path=path, error=f"not a JSON object: {type(data).__name__}")
    return BuildInfo(path=path, data=data)


def shipped_producers(info: BuildInfo) -> dict[str, str]:
    """The producer digests this release ships, ``{name: sha256}``.

    Only well-formed entries are kept, and a malformed value is dropped rather
    than compared: comparing a host file against a digest that is not a digest
    would report skew for every producer, which is a worse failure than reporting
    one fewer comparison.
    """
    raw = info.data.get("producers")
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): value
        for name, value in raw.items()
        if isinstance(value, str) and value
    }


def producer_report(info: BuildInfo, home: Path) -> dict:
    """Compare the shipped producers against the ones installed on this host.

    ``skew`` and ``missing`` are the two answers that matter, and they are kept
    apart: a producer that is present but different is a host running something
    else, while a producer that is absent is a host not running it at all. Both
    are normal on a machine that is not the one the producers run on (a CI agent,
    a laptop), so neither is an error — the count is the signal (schema §6).
    """
    shipped = shipped_producers(info)
    installed: dict[str, dict] = {}
    skew: list[str] = []
    missing: list[str] = []
    for name in sorted(shipped):
        path = installed_path(name, home)
        record: dict = {
            "path": str(path),
            "shipped_sha256": shipped[name],
            "present": False,
            "sha256": None,
        }
        if path.is_file():
            try:
                record["sha256"] = sha256(path)
                record["present"] = True
            except OSError as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"
        installed[name] = record
        if not record["present"]:
            missing.append(name)
        elif record["sha256"] != shipped[name]:
            skew.append(name)
    return {
        "shipped": shipped,
        "installed": installed,
        "skew": skew,
        "missing": missing,
    }
