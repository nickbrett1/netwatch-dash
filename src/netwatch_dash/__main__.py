"""Default module entry point (python -m netwatch_dash).

This is what the release payload's ``bin/netwatch-dash`` execs, so it answers
``--version`` and ``--help`` *before* it imports the ASGI stack — the Buildkite
smoke gate runs it on the assembled payload, and a gate that fails because an
optional import is missing off a machine would be a gate that fails for a reason
that is not the payload's. With no flags it serves the dashboard.
"""

import sys
from importlib.metadata import PackageNotFoundError, version

from .settings import Settings

DISTRIBUTION = "netwatch-dash"

USAGE = """\
usage: netwatch-dash [--version] [--help]

Serves the netwatch dashboard. Where it listens and what it reads come from the
environment (deploy/install-host.sh writes $HOME/.config/netwatch-dash/env):

  NETWATCH_DASH_BIND        host:port to bind          (default 127.0.0.1:8791)
  NETWATCH_DASH_STATE       ~/.local/state/netwatch
  NETWATCH_DASH_GWCSV       ~/netwatch/gateway_rtt.csv
  NETWATCH_DASH_CONFIG      ~/.config/netwatch/config
  NETWATCH_DASH_TZ          America/New_York
  NETWATCH_DASH_BUILD_INFO  this payload's build-info.json (default: discovered)
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


def serve(settings: Settings | None = None) -> int:
    """Run the dashboard until stopped.

    ``uvicorn`` is imported here rather than at module scope so that ``--version``
    and ``--help`` do not depend on it being importable.
    """
    import uvicorn

    from .app import create_app

    settings = settings or Settings.from_env()
    host, port = settings.host_port
    uvicorn.run(
        create_app(settings),
        host=host,
        port=port,
        log_level="info",
        # The payload installs plain `uvicorn`, not `uvicorn[standard]`, so
        # uvloop/httptools are absent and uvicorn's `auto` would silently fall
        # back to asyncio/h11 anyway. Naming them removes a fallback whose
        # outcome depends on which optional extras a given environment happens
        # to have — the same payload should take the same path everywhere.
        loop="asyncio",
        http="h11",
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    if "--version" in args:
        print(f"{DISTRIBUTION} {_version()}")
        return 0
    if "--help" in args or "-h" in args:
        print(USAGE, end="")
        return 0

    return serve()


if __name__ == "__main__":
    sys.exit(main())
