"""build-info.json: reading it back, and comparing producers.

The payload's self-report is the only thing that can say which release is
running, so its failure modes matter as much as its contents: `load` never
raises, an unreadable file is *absent with a reason*, and a comparison is never
made against a digest that is not one.
"""

import json
from pathlib import Path

from netwatch_dash import buildinfo


def test_find_prefers_an_explicit_path_even_when_it_is_absent(tmp_path):
    """Naming a file means the answer is about *that* file.

    Falling back to a discovered one would answer a question nobody asked — and
    during a smoke gate, where a fixture is named deliberately, it would answer
    it with the real payload's identity.
    """
    target = tmp_path / "not-here.json"
    assert buildinfo.find(target) == target


def test_find_returns_none_when_nothing_is_discoverable(monkeypatch, tmp_path):
    monkeypatch.setattr(buildinfo.sys, "prefix", str(tmp_path / "no-prefix"))
    monkeypatch.setattr(
        buildinfo, "__file__", str(tmp_path / "src/netwatch_dash/buildinfo.py")
    )
    assert buildinfo.find() is None


def test_find_discovers_the_payload_layout_under_sys_prefix(monkeypatch, tmp_path):
    """The bundled interpreter is installed at `<root>/python`, so the payload
    root is `sys.prefix`'s parent. That is the assumption, pinned here.
    """
    root = tmp_path / "payload"
    (root / "share/netwatch-dash").mkdir(parents=True)
    info = root / "share/netwatch-dash/build-info.json"
    info.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(buildinfo.sys, "prefix", str(root / "python"))
    monkeypatch.setattr(
        buildinfo, "__file__", str(tmp_path / "elsewhere/buildinfo.py")
    )
    assert buildinfo.find() == info


def test_find_discovers_a_source_checkout(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    (root / "src/netwatch_dash").mkdir(parents=True)
    (root / "share/netwatch-dash").mkdir(parents=True)
    info = root / "share/netwatch-dash/build-info.json"
    info.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(buildinfo.sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(
        buildinfo, "__file__", str(root / "src/netwatch_dash/buildinfo.py")
    )
    assert buildinfo.find() == info


def test_load_reports_absence_with_a_reason(tmp_path):
    info = buildinfo.load(tmp_path / "missing.json")
    assert info.found is False
    assert info.path == tmp_path / "missing.json"
    assert "FileNotFoundError" in info.error
    assert info.as_dict()["found"] is False
    assert info.as_dict()["version"] is None


def test_load_reports_unparseable_json(tmp_path):
    p = tmp_path / "build-info.json"
    p.write_text("{not json", encoding="utf-8")
    info = buildinfo.load(p)
    assert info.found is False
    assert "not valid JSON" in info.error


def test_load_rejects_a_non_object(tmp_path):
    p = tmp_path / "build-info.json"
    p.write_text("[]", encoding="utf-8")
    info = buildinfo.load(p)
    assert info.found is False
    assert "not a JSON object" in info.error


def test_load_reads_a_well_formed_file(tmp_path):
    p = tmp_path / "build-info.json"
    p.write_text(json.dumps({"version": "1.2.3", "commit": "c" * 40}), encoding="utf-8")
    info = buildinfo.load(p)
    assert info.found is True
    assert info.error is None
    assert info.get("version") == "1.2.3"
    assert info.as_dict()["commit"] == "c" * 40


def test_shipped_producers_drops_malformed_entries():
    """A value that is not a digest is dropped, not compared.

    Comparing a host file against "" would report skew for every producer — a
    worse failure than reporting one fewer comparison, because it looks like a
    finding.
    """
    info = buildinfo.BuildInfo(
        path=Path("x"),
        data={"producers": {"netwatch": "a" * 64, "junk": "", "bad": 3, "none": None}},
    )
    assert buildinfo.shipped_producers(info) == {"netwatch": "a" * 64}


def test_shipped_producers_of_a_missing_file_is_empty_not_an_error():
    assert buildinfo.shipped_producers(buildinfo.BuildInfo(path=None, error="x")) == {}


def test_installed_path_falls_back_to_netwatch_for_an_unknown_name(tmp_path):
    """A producer this table has never seen is still looked for.

    A new producer is exactly the case where "no comparison happened" must not
    read like "no drift".
    """
    assert buildinfo.installed_path("netwatch", tmp_path) == tmp_path / ".local/bin/netwatch"
    assert buildinfo.installed_path("gwping.py", tmp_path) == tmp_path / "netwatch/gwping.py"
    assert buildinfo.installed_path("newthing", tmp_path) == tmp_path / "netwatch/newthing"


def test_producer_report_separates_skew_from_missing(tmp_path):
    home = tmp_path / "home"
    (home / ".local/bin").mkdir(parents=True)
    (home / "netwatch").mkdir(parents=True)
    # Same bytes the release ships.
    (home / ".local/bin/netwatch").write_text("same", encoding="utf-8")
    # Present, but a different file.
    (home / "netwatch/gwping.py").write_text("edited on the host", encoding="utf-8")
    # flapwatch is shipped but not installed here at all.

    same = buildinfo.sha256(home / ".local/bin/netwatch")
    info = buildinfo.BuildInfo(
        path=Path("x"),
        data={
            "producers": {
                "netwatch": same,
                "gwping.py": "0" * 64,
                "flapwatch": "1" * 64,
            }
        },
    )
    report = buildinfo.producer_report(info, home)

    assert report["skew"] == ["gwping.py"]
    assert report["missing"] == ["flapwatch"]
    assert report["installed"]["netwatch"]["present"] is True
    assert report["installed"]["netwatch"]["sha256"] == same
    assert report["installed"]["flapwatch"]["present"] is False
    assert report["installed"]["flapwatch"]["sha256"] is None
    # The path each file was looked for is part of the answer.
    assert report["installed"]["flapwatch"]["path"] == str(
        home / ".local/bin/flapwatch"
    )


def test_producer_report_with_no_shipped_producers_is_empty(tmp_path):
    report = buildinfo.producer_report(
        buildinfo.BuildInfo(path=None, error="x"), tmp_path
    )
    assert report["skew"] == []
    assert report["missing"] == []
    assert report["shipped"] == {}
