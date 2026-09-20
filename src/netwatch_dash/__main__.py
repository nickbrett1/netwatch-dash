"""Default module entry point (python -m netwatch_dash).

This is what the release payload's ``bin/netwatch-dash`` execs, and what the
Buildkite smoke gate probes, so it answers ``--version`` and ``--help`` before
the real application work is wired up. Override the container command via the
genproj docker-container "command" configuration option (or "entrypoint") when
your application needs a custom entry point.
"""

import sys
from importlib.metadata import PackageNotFoundError, version

DISTRIBUTION = "netwatch-dash"

USAGE = """\
usage: netwatch-dash [--version] [--help]

The netwatch-dash application is not implemented yet; this entry point exists so
the release payload can be assembled, run and smoke-tested from the first
release onwards.
"""


def _version() -> str:
    """The installed distribution's version.

    Read from the metadata of the distribution that is actually installed rather
    than from a constant, so the answer belongs to the payload it is printed
    from. A source checkout that was never installed falls back to the module's
    own ``__version__``, which is the same value pyproject declares.
    """
    try:
        return version(DISTRIBUTION)
    except PackageNotFoundError:
        from netwatch_dash import __version__

        return __version__


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    if "--version" in args:
        print(f"{DISTRIBUTION} {_version()}")
        return 0
    if "--help" in args or "-h" in args:
        print(USAGE, end="")
        return 0

    print(f"{__package__} is installed and importable.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
