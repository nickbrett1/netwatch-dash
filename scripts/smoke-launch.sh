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
PROBE_ARGS=(--version --help)
# Cold start includes importing the package for the first time and writing its
# bytecode with the BUNDLED interpreter (the payload ships no .pyc — see
# build-payload.sh), so this is generous on purpose.
TIMEOUT_SECS="${SMOKE_TIMEOUT:-60}"
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
cleanup() {
  local d
  for d in "${workdirs[@]:-}"; do
    [ -n "$d" ] && rm -rf "$d"
  done
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
  echo "smoke-launch: $payload OK"
done

if [ "$failed" -ne 0 ]; then
  echo "smoke-launch: one or more payloads failed - not releasing" >&2
  exit 1
fi
echo "smoke-launch: all payloads ran"
