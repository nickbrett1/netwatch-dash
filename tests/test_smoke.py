"""Smoke test: the src-layout package installs and imports cleanly."""


def test_package_imports():
    import netwatch_dash

    assert netwatch_dash.__version__
