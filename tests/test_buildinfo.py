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


def test_installable_producers_names_a_destination_for_each(tmp_path):
    """What `deploy/install-host.sh` copies, and where it puts it."""
    src = tmp_path / "producers"
    src.mkdir()
    (src / "gwping.py").write_text("a", encoding="utf-8")
    (src / "netwatch").write_text("b", encoding="utf-8")
    home = tmp_path / "home"

    got = buildinfo.installable_producers(src, home)

    assert got == [
        ("gwping.py", home / "netwatch/gwping.py"),
        ("netwatch", home / ".local/bin/netwatch"),
    ]


def test_installable_producers_leaves_the_readme_behind(tmp_path):
    """producers/README.md is provenance, not a producer.

    `scripts/build-payload.sh` keeps it out of the shipped digest set, so the
    installer has to keep it out of the copy set or the host gains a file that
    the drift report never mentions.
    """
    src = tmp_path / "producers"
    src.mkdir()
    (src / "README.md").write_text("prose", encoding="utf-8")

    assert buildinfo.installable_producers(src, tmp_path) == []


def test_installable_producers_of_a_missing_dir_is_empty_not_an_error(tmp_path):
    """A checkout without producers/ installs nothing; it does not raise."""
    assert buildinfo.installable_producers(tmp_path / "nope", tmp_path) == []


def test_install_and_drift_agree_on_where_a_producer_lives(tmp_path):
    """The installer's destination must be the path the drift report compares.

    These are two halves of one question — "where does this producer go?" and
    "where is the host's copy?" — answered by the same table on purpose. If they
    diverge, install-host.sh populates one path while `/healthz` compares another,
    and the host reports skew for the file it just installed.
    """
    src = tmp_path / "producers"
    src.mkdir()
    for name in ("gwping.py", "netwatch", "surprise"):
        (src / name).write_text(name, encoding="utf-8")
    home = tmp_path / "home"

    installed = dict(buildinfo.installable_producers(src, home))

    assert installed == {
        name: buildinfo.installed_path(name, home) for name in installed
    }
    # An unknown name is installed to the fallback, exactly where it is looked for.
    assert installed["surprise"] == home / "netwatch/surprise"


def test_every_producer_this_repo_ships_has_an_install_path():
    """A new producer must be given a home on purpose, not by omission.

    `installed_path` falls back to `~/netwatch/`, so a producer whose real home
    is `~/.local/bin/` (a command the plists exec by name) would be installed
    somewhere nothing runs it — and the drift report would agree it is "present",
    because it looks in the same wrong place. Failing here makes that a choice.
    """
    producers = Path(buildinfo.__file__).resolve().parents[2] / "producers"
    shipped = sorted(
        f.name
        for f in producers.iterdir()
        if f.is_file() and f.name not in buildinfo.IGNORED_PRODUCER_FILES
    )

    assert shipped, "producers/ is empty, so this test would prove nothing"
    assert set(shipped) <= set(buildinfo.INSTALL_PATHS)


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


def test_a_payload_started_by_hand_has_no_launcher_to_describe():
    """Most runs are this: a test, `python -m netwatch_dash`. Say so, do not guess."""
    report = buildinfo.launcher_report({})

    assert report["found"] is False
    assert report["path"] is None
    assert report["version"] is None
    assert report["sha256"] is None


def test_the_launcher_that_started_the_payload_is_read_back():
    env = {
        "FETCH_LAUNCH_PATH": "/host/.local/share/netwatch-dash/fetch-launch.sh",
        "FETCH_LAUNCH_VERSION": "0.1.18",
        "FETCH_LAUNCH_SHA256": "a" * 64,
        # A stray empty value is not a launcher either.
        "FETCH_LAUNCH_EXTRA": "",
    }
    report = buildinfo.launcher_report(env)

    assert report["found"] is True
    assert report["path"] == "/host/.local/share/netwatch-dash/fetch-launch.sh"
    assert report["version"] == "0.1.18"
    assert report["sha256"] == "a" * 64
    assert "FETCH_LAUNCH_EXTRA" not in report  # only the three agreed names


def test_an_empty_launcher_path_is_still_not_found():
    """A var that is set but empty is not an answer."""
    report = buildinfo.launcher_report({"FETCH_LAUNCH_PATH": ""})
    assert report["found"] is False
