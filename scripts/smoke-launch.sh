#!/usr/bin/env bash
#
# Smoke-test the darwin payload before the release publishes it.
#
# genproj seeds this file once; after that it is yours. `scripts/` is app-owned,
# so regeneration never overwrites it — unlike .buildkite/pipeline.yml, which is
# genproj's and is rewritten on every regeneration.
#
# Why this exists
# ---------------
# A darwin target is built on a Mac host (see .buildkite/README.md): the one
# host in the fleet that can execute a macOS payload. Building there proves the
# payload links; it does not prove it runs. Nothing between the build step and
# the release used to execute the artifact, so a payload that cannot start
# shipped, and the failure surfaced only when a host installed it. The generated
# pipeline runs this script on the macOS host and gates the release on it.
#
# What this project changed, and why
# ----------------------------------
# The seeded version of this file takes a *payload root* — the directory the
# build step uploaded — on the assumption that the build's output IS the payload
# (`bash scripts/smoke-launch.sh dist`). For this project that is false in one
# specific way: the payload carries its own CPython and has to be ASSEMBLED, and
# the build step is genproj-owned so its commands cannot be extended to do it.
#
# So the argument here is a WHEEL DIRECTORY, and the payload root is assembled
# from it by `scripts/build-payload.sh` — the same script, with the same gates,
# that `scripts/release-artifacts.sh` packs from. The gate therefore runs the
# real payload, not a thinner stand-in: same interpreter, same resolved
# dependencies, same entry-point shim that the tarball will contain.
#
# The alternative — running the wheel in place — would be a gate that passes
# whether or not the assembly works, which is decoration.
#
# What "starts" means, and why it is two checks
# ---------------------------------------------
# The payload is checked twice, because it can fail in two unrelated ways:
#
#   * `--version` / `--help` — answered BEFORE the ASGI stack is imported, so
#     this proves the interpreter execs and the package imports.
#   * it is started for real, with a temporary HOME and on a loopback port the OS
#     reports free, and `/healthz` is read back over HTTP with the payload's own
#     interpreter. This is what proves the app binds and answers.
#
# The second check is the one a host's experience actually is. Adding it is how
# this project found that the first check was passing payloads whose app could
# not start: `--version` never touches FastAPI, so it cannot fail for that
# reason. Both are kept — an import error and a bind error should not be told
# apart by the same message.
#
# Contract
# --------
# Called with the wheel directory (directories) the build step uploaded:
#
#   bash scripts/smoke-launch.sh dist
#
# Each is assembled into a temporary payload root, which must contain
# `bin/netwatch-dash` and must start and answer. Fail-closed: a payload that
# cannot run fails the step, and the release depends on this step.
set -euo pipefail

ENTRY_NAME="netwatch-dash"
PAYLOAD_SCRIPT="scripts/build-payload.sh"
# Must match build-payload.sh's PROJECT: it names the share/<name>/ directory the
# payload writes build-info.json into, which the serve check asserts a path in.
PROJECT="netwatch-dash"
PROBE_ARGS=(--version --help)
# Cold start includes importing the package for the first time and writing its
# bytecode with the BUNDLED interpreter (the payload ships no .pyc — see
# build-payload.sh), so this is generous on purpose. It is also the deadline for
# the serve check below, which pays that same cold start and then one HTTP round
# trip.
TIMEOUT_SECS="${SMOKE_TIMEOUT:-60}"
# The port the serve check binds. Left unset, a free one is asked of the OS (see
# below); set it only to pin a specific port for a debugging run.
SMOKE_PORT="${SMOKE_PORT:-}"

# What "the payload works" means over HTTP, run with the payload's OWN
# interpreter so the gate depends on nothing the payload does not ship (no curl,
# no jq). One request, one JSON body, and two things demanded:
#
#   * a `status` field — a payload that answers 200 with a stack-trace page, an
#     empty body, or someone else's JSON is not serving the dashboard, and the
#     gate must not pass on the status code alone.
#
#   * that the answer came from THIS payload. The expected build-info.json path
#     is passed in and must come back exactly; /healthz derives that path from
#     the running interpreter's prefix (buildinfo.find), so it identifies the
#     server rather than describing it. Without this the check is not a check:
#     measured on mac-studio 2026-09-20, a leftover devcontainer server was still
#     being port-forwarded onto the agent, and the gate reported
#     "http://127.0.0.1:8792/healthz -> status unknown … OK" while the payload it
#     had just built was not listening at all. "Something answered" is not
#     "the thing I started answered", and the difference is the whole gate.
#     `found` is demanded too: the payload is assembled WITH a build-info.json,
#     so a payload that cannot find its own is a broken assembly, and /healthz
#     says so itself.
HEALTHZ_PROBE='
import json, os, sys, urllib.request

url = "http://127.0.0.1:%s/healthz" % sys.argv[1]
expected = sys.argv[2]
try:
    with urllib.request.urlopen(url, timeout=5) as response:
        status, body = response.status, response.read().decode("utf-8", "replace")
except Exception as exc:
    print("no answer from %s: %s" % (url, exc), file=sys.stderr)
    sys.exit(1)
if status != 200:
    print("%s answered %s, not 200" % (url, status), file=sys.stderr)
    sys.exit(1)
try:
    document = json.loads(body)
except ValueError:
    print("%s did not answer with JSON: %r" % (url, body[:200]), file=sys.stderr)
    sys.exit(1)
if not isinstance(document, dict) or "status" not in document:
    print("%s answered JSON with no status: %r" % (url, body[:200]), file=sys.stderr)
    sys.exit(1)
build = document.get("build") or {}
if not build.get("found"):
    print("%s cannot report its own build: %r" % (url, build.get("error")), file=sys.stderr)
    sys.exit(1)
# Compared as real paths: /healthz reports what its own discovery produced, and
# on macOS /tmp is a symlink to /private/tmp, so the string the caller passed in
# and the string the server reports legitimately differ. Resolving both sides
# compares the location, which is the thing being asked about.
if os.path.realpath(build.get("path") or "") != os.path.realpath(expected):
    print(
        "%s was answered by a different process: it reports its build as %r, not %r"
        % (url, build.get("path"), expected),
        file=sys.stderr,
    )
    # Exit 2, not 1: this cannot become true by waiting, so the caller stops
    # polling instead of spending the whole deadline re-asking a settled question.
    sys.exit(2)
print("%s -> status %s" % (url, document["status"]))
'

# Is this port ours to take? A connection that is ACCEPTED means something else
# is already listening, and the gate would then be reading that server's answers
# rather than the payload's. Checked before starting, so the failure is a clear
# refusal instead of a confusing one after the fact. (The identity check above is
# the real control; this one only buys a better message.)
PORT_IS_FREE='
import socket, sys

probe = socket.socket()
probe.settimeout(2)
try:
    probe.connect(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(0)
sys.exit(1)
'
# build-info.json records a version; nothing in the gate reads it, and there is
# no tag yet at smoke time (the release step creates it). Say so rather than
# inventing a version number that looks real.
SMOKE_VERSION="${RELEASE_VERSION:-0.0.0-smoke}"

if [ "$#" -eq 0 ]; then
  echo "usage: smoke-launch.sh <wheel-dir> [<wheel-dir>...]" >&2
  exit 2
fi

payloads=()
workdirs=()
tempfiles=()
cleanup() {
  local d f
  for d in "${workdirs[@]:-}"; do
    # `if`, not `[ -n "$d" ] && rm -rf "$d"`: with an empty array the expansion is
    # one empty string, and a bare `[ -n "" ] && …` is the loop's last command,
    # so it returns 1 and — because this runs as an EXIT trap — becomes the
    # SCRIPT's exit status. That is not hypothetical: it is how the gate reported
    # "all payloads ran" and then exited 1, once the pipeline started handing it
    # an already-assembled root (so nothing ever appended to `workdirs`). A
    # cleanup hook must not be able to fail the thing it is cleaning up after.
    if [ -n "$d" ]; then
      rm -rf "$d"
    fi
  done
  # Temp FILES are tracked rather than removed inline, for the same reason the
  # workdirs are: an iteration that `continue`s on failure would otherwise leak
  # its logs, and those logs are exactly what a human needs when the gate is red.
  for f in "${tempfiles[@]:-}"; do
    if [ -n "$f" ]; then
      rm -f "$f"
    fi
  done
  return 0
}
trap cleanup EXIT

# A portable `timeout`: coreutils' timeout is not on macOS by default, and this
# runs on the Mac host. Returns the command's exit status, or the kill status
# when the command had to be stopped.
run_with_timeout() {
  local secs="$1"
  shift
  "$@" &
  local pid=$!
  (
    sleep "$secs"
    if kill -0 "$pid" 2>/dev/null; then
      echo "smoke-launch: payload did not answer within ${secs}s" >&2
      kill -TERM "$pid" 2>/dev/null || true
    fi
  ) &
  local watcher=$!
  local rc=0
  wait "$pid" || rc=$?
  kill "$watcher" 2>/dev/null || true
  wait "$watcher" 2>/dev/null || true
  return "$rc"
}

failed=0
for root in "$@"; do
  if [ ! -d "$root" ]; then
    echo "smoke-launch: no such directory: $root — the build step uploads dist/**; check artifact_paths" >&2
    failed=1
    continue
  fi

  # A root that already holds the entry point is a payload root and is run as it
  # stands. Otherwise it is treated as a wheel directory and assembled first.
  # Both paths end at the same checks, so neither can be the lenient one.
  if [ -e "$root/bin/$ENTRY_NAME" ]; then
    payload="$root"
    echo "smoke-launch: $root already holds bin/$ENTRY_NAME — running it as a payload root"
  else
    if [ ! -f "$PAYLOAD_SCRIPT" ]; then
      echo "smoke-launch: $root has no bin/$ENTRY_NAME and $PAYLOAD_SCRIPT is missing — nothing to assemble with" >&2
      failed=1
      continue
    fi
    tmp="$(mktemp -d)"
    workdirs+=("$tmp")
    payload="$tmp/payload"
    echo "smoke-launch: assembling a payload from the wheels in $root"
    if ! bash "$PAYLOAD_SCRIPT" "$SMOKE_VERSION" "$payload" "$root"; then
      echo "smoke-launch: $PAYLOAD_SCRIPT failed — the payload cannot be assembled, so it cannot be released" >&2
      failed=1
      continue
    fi
  fi

  entry="$payload/bin/$ENTRY_NAME"
  echo "smoke-launch: checking $payload"
  if [ ! -e "$entry" ]; then
    echo "smoke-launch: no entry point at $entry — a payload root must hold bin/$ENTRY_NAME (see LAUNCHING.md)" >&2
    failed=1
    continue
  fi
  chmod +x "$entry" 2>/dev/null || true
  if [ ! -x "$entry" ]; then
    echo "smoke-launch: $entry is not executable" >&2
    failed=1
    continue
  fi
  # The entry point is a #!/bin/sh shim, not a Mach-O, so this checks the
  # interpreter the shim execs — the file that actually has to be arm64. A
  # wrapper script's own architecture is nothing; the real check is the run
  # below, where a wrong-arch interpreter dies with "Bad CPU type".
  interpreter="$payload/python/bin/python3.13"
  if [ -f "$interpreter" ]; then
    if command -v file >/dev/null 2>&1; then
      desc="$(file -b "$interpreter" 2>/dev/null || true)"
      case "$desc" in
        *Mach-O*)
          case "$desc" in
            *arm64* | *aarch64*) : ;;
            *)
              echo "smoke-launch: $interpreter is not an arm64 binary (file: $desc)" >&2
              failed=1
              continue
              ;;
          esac
          ;;
        *)
          echo "smoke-launch: $interpreter is not a Mach-O binary (file: $desc)" >&2
          failed=1
          continue
          ;;
      esac
    fi
  else
    echo "smoke-launch: the payload has no bundled interpreter at $interpreter" >&2
    failed=1
    continue
  fi
  # Starts and answers: try a probe flag under a timeout. A payload that exits 0
  # for neither probe does not start the way LAUNCHING.md promises, so it fails
  # closed rather than releasing.
  #
  # The payload's own output is kept, not discarded: this step runs on an agent
  # nobody logs into interactively, and the difference between "the interpreter
  # cannot exec" and "the module raised on import" is the whole diagnosis. Only
  # the first probe's output is shown, because the second probe's output would
  # repeat it.
  answered=0
  probe_log="$(mktemp)"
  probe_output_seen=0
  for probe in "${PROBE_ARGS[@]}"; do
    if run_with_timeout "$TIMEOUT_SECS" "$entry" "$probe" >"$probe_log" 2>&1; then
      answered=1
      break
    fi
    if [ "$probe_output_seen" = 0 ] && [ -s "$probe_log" ]; then
      echo "smoke-launch: $entry $probe failed, and said:" >&2
      sed -e 's/^/    | /' "$probe_log" >&2
      probe_output_seen=1
    fi
  done
  rm -f "$probe_log"
  if [ "$answered" -ne 1 ]; then
    echo "smoke-launch: $entry did not answer to --version or --help within ${TIMEOUT_SECS}s" >&2
    echo "smoke-launch: run it by hand to see why (the payload must start on this host before a host installs it)" >&2
    failed=1
    continue
  fi
  # Serves, and answers over HTTP.
  #
  # --version and --help are answered BEFORE the ASGI stack is imported (that is
  # deliberate — see __main__.py), so they prove the interpreter execs and the
  # package imports. They do not prove the app starts, binds, or answers a
  # request, which is the only thing a host installing this payload does with it.
  # Until this block existed, a payload whose FastAPI app could not start still
  # passed the gate and failed on the host.
  #
  # Three things are deliberately not taken from the host:
  #   * a temporary HOME, so the gate reads no real state. With no events.jsonl
  #     the status is "unknown", which is the honest answer for an empty state
  #     directory and is stable from run to run — the gate asserts the shape of
  #     the response, not a status that depends on the host's data.
  #   * a port the OS reports free. The default is 8791, and this gate runs on
  #     mac-studio, where the real dashboard may already be bound to it; a clash
  #     would be indistinguishable from a payload that cannot start.
  #   * the payload's own interpreter for the HTTP request, so the gate is not
  #     silently skipping the check on an agent that has no curl.
  home="$(mktemp -d)"
  workdirs+=("$home")
  port="$SMOKE_PORT"
  if [ -z "$port" ]; then
    port="$("$interpreter" -c 'import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()' 2>/dev/null || true)"
  fi
  case "$port" in
    '' | *[!0-9]*) port=18791 ;;
  esac

  # Refuse a port somebody else already holds, before starting anything. On
  # mac-studio this is not theoretical: developer port forwards have squatted
  # 8792/8793, and a gate that runs against them is reading a different process.
  if ! "$interpreter" -c "$PORT_IS_FREE" "$port"; then
    echo "smoke-launch: 127.0.0.1:$port already accepts connections - something else is listening there" >&2
    echo "smoke-launch: refusing, because it would answer in place of the payload. Retry, or set SMOKE_PORT" >&2
    failed=1
    continue
  fi

  serve_log="$(mktemp)"
  healthz_log="$(mktemp)"
  tempfiles+=("$serve_log" "$healthz_log")
  # The path /healthz must report back: the build-info.json this assembly wrote,
  # which buildinfo.find resolves from the running interpreter's prefix.
  expected_build_info="$payload/share/$PROJECT/build-info.json"
  # NETWATCH_DASH_HOME is the variable the app reads (settings.py); setting HOME
  # too keeps any tool that looks at it inside the temporary tree.
  HOME="$home" NETWATCH_DASH_HOME="$home" NETWATCH_DASH_BIND="127.0.0.1:$port" \
    "$entry" >"$serve_log" 2>&1 &
  serve_pid=$!

  # A deadline, not a fixed sleep: a cold start imports FastAPI and uvicorn with
  # the bundled interpreter, which is not instant, and a fixed sleep would either
  # be too short on a loaded agent or waste time on an idle one.
  served=0
  probe_rc=0
  deadline=$((SECONDS + TIMEOUT_SECS))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if "$interpreter" -c "$HEALTHZ_PROBE" "$port" "$expected_build_info" >"$healthz_log" 2>&1; then
      served=1
      break
    else
      probe_rc=$?
    fi
    # A definite "somebody else answered" will not change by asking again.
    if [ "$probe_rc" -eq 2 ]; then
      break
    fi
    # A server that has already exited will not start answering; stop waiting and
    # report its output instead of burning the whole deadline.
    if ! kill -0 "$serve_pid" 2>/dev/null; then
      break
    fi
    sleep 1
  done

  kill "$serve_pid" 2>/dev/null || true
  wait "$serve_pid" 2>/dev/null || true

  if [ "$served" -ne 1 ]; then
    echo "smoke-launch: $entry did not answer /healthz as this payload on 127.0.0.1:$port (within ${TIMEOUT_SECS}s)" >&2
    if [ -s "$healthz_log" ]; then
      echo "smoke-launch: the last request said:" >&2
      sed -e 's/^/    | /' "$healthz_log" >&2
    fi
    if [ -s "$serve_log" ]; then
      echo "smoke-launch: while serving, the payload said:" >&2
      sed -e 's/^/    | /' "$serve_log" >&2
    fi
    failed=1
    continue
  fi
  echo "smoke-launch: $(cat "$healthz_log")"
  echo "smoke-launch: $payload OK"
done

if [ "$failed" -ne 0 ]; then
  echo "smoke-launch: one or more payloads failed - not releasing" >&2
  exit 1
fi
echo "smoke-launch: all payloads ran"
