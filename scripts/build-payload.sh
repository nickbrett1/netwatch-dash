#!/usr/bin/env bash
#
# Assembles the release payload root: the directory a launcher unpacks onto a
# host and execs `bin/netwatch-dash` from (LAUNCHING.md, "The payload contract").
#
# Why this is separate from release-artifacts.sh
# ----------------------------------------------
# The payload root has to exist before anyone can either PACK it or RUN it, and
# those two happen in different steps:
#
#   * scripts/release-artifacts.sh packs it into
#     netwatch-dash-aarch64-apple-darwin.tar.gz and writes the manifest.
#   * scripts/smoke-launch.sh runs it, on the macOS agent, to prove the payload
#     starts before the release is published.
#
# genproj's seeded smoke gate assumes the payload root is *already* the thing the
# build step uploaded (`bash scripts/smoke-launch.sh dist`). That is true for a
# plain wheel: `dist/` is the payload. It is not true here, because this payload
# carries its own CPython and that assembly has to happen somewhere. One copy of
# the assembly, two callers, keeps the thing the gate runs and the thing the
# tarball contains the same bytes by construction rather than by hope.
#
# Both callers are app-owned (`scripts/` is never overwritten by a regen), so
# this arrangement survives regeneration. The build step's own commands are
# genproj's and cannot be extended, which is why the assembly is here and not
# there.
#
# Called as: bash scripts/build-payload.sh <version> <payload-root> [<wheel-dir>]
#   <version>       the tag without its `v` prefix, e.g. "1.2.4" — recorded in
#                   build-info.json, which /healthz serves.
#   <payload-root>  created (and removed first if it exists): the assembled tree.
#   <wheel-dir>     where the app wheel lives; default dist/. The release step
#                   downloads what the build step uploaded, so this is the exact
#                   wheel pytest ran against.
set -euo pipefail

VERSION="${1:?usage: build-payload.sh <version> <payload-root> [<wheel-dir>]}"
PAYLOAD="${2:?usage: build-payload.sh <version> <payload-root> [<wheel-dir>]}"
WHEEL_DIR="${3:-dist}"

log() { echo "$*"; }
die() { echo "$*" >&2; exit 1; }

# The output directory is removed before it is filled, so it is checked before
# it is removed: a caller that passes "" (an unset variable that was meant to be
# quoted), "/" or the repo root would otherwise be asking for a tree to be
# deleted. The caller's mistake should be a refusal, not an `rm -rf`.
case "$PAYLOAD" in
  "" | / | . | .. | */../*) die "refusing to build into '$PAYLOAD' - pass the payload root explicitly" ;;
esac
[ "$PAYLOAD" != "$WHEEL_DIR" ] ||
  die "refusing to build into the wheel directory ('$PAYLOAD' == '$WHEEL_DIR')"

# --- the declared target -----------------------------------------------------
# This project declares `github-release.target: aarch64-apple-darwin` (which
# capability knobs were used is recorded in docs/genproj-invocation.md). That
# declaration is the *third* release shape genproj grew on 2026-09-20: one
# artifact, built once, that is genuinely platform-specific — as opposed to
# `targets` (a rust build matrix) and `any` (the universal key, whose contract is
# an artifact that is *not* architecture-specific). Read
# docs/genproj-target-gap.md before changing the value below: publishing this
# payload under `any` is not a smaller mistake than publishing it under the wrong
# triple, it is the same mistake in reverse.
#
# The key is a CONSTANT here, never derived from the asset filename. The seeded
# release-artifacts.sh parses `target` out of `basename "$file"`, which makes the
# filename *the* declaration and writes the manifest to agree with whatever was
# packed. With the triple declared upstream the filename becomes an assertion
# instead: release-artifacts.sh checks $ASSET against $TARGET, and the manifest
# key is written from $TARGET.
PROJECT="netwatch-dash"
TARGET="${RELEASE_TARGET:-aarch64-apple-darwin}"

# --- the bundled interpreter -------------------------------------------------
# The wheel in dist/ is `py3-none-any` — pure python, genuinely architecture
# independent. What it *runs on* is not: a macOS system python3 is the Xcode CLT
# shim (version tied to the installed toolchain, no pip, absent on a machine
# without the tools), so the payload carries its own CPython and the triple is
# honest because of it.
#
# The interpreter is pinned to an exact python-build-standalone release and an
# exact sha256, checked on every run. Pinning the *version* alone would let a
# re-published asset (or a mirror) change under us without the payload changing
# name; the digest is what makes "the payload at v1.2.4" mean one set of bytes
# for the interpreter the same way `sha256` in the manifest does for the tarball.
# The value was taken from that release's own SHA256SUMS file.
PBS_RELEASE="${PBS_RELEASE:-20260901}"
PBS_PYTHON="${PBS_PYTHON:-3.13.15}"
PBS_ASSET="cpython-$PBS_PYTHON+$PBS_RELEASE-aarch64-apple-darwin-install_only_stripped.tar.gz"
PBS_SHA256="d3904bd6a072246e07aa0bdadee9a14e80521e42a943c0848059feb16a2816dc"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_RELEASE/$PBS_ASSET"

# Where the interpreter keeps third-party packages, and what a wheel has to
# declare to be installable into it. Both are constants of the pinned build.
PY_SERIES="3.13"
# The pinned build spells its own filenames both ways — `python3.13` and
# `_tkinter.cpython-313-darwin.so` — so a path assembled from $PY_SERIES alone
# silently matches nothing. It did: `_tkinter` survived a prune that named it.
PY_TAG="${PY_SERIES/./}"
PBS_SITE="lib/python$PY_SERIES/site-packages"
PBS_PLATFORM="macosx_11_0_arm64"
PBS_ABI="cp313"

CACHE_DIR="${PBS_CACHE_DIR:-${TMPDIR:-/tmp}/netwatch-dash-pbs}"

# sha256sum is GNU coreutils; macOS ships shasum instead. Both callers can run
# on either host — the release step is a Linux container, the smoke gate is a
# native Mac — so this has to work in both. The digest is *validated* rather
# than trusted: mac-studio resolves `sha256sum` to an unexpected
# /sbin/sha256sum wrapper (2026-09-20), and a digest helper that silently
# returns a decorated string would turn every payload check into a mismatch
# against a pin that is correct.
sha256() {  # <file>
  local out
  if command -v sha256sum >/dev/null 2>&1; then
    out="$(sha256sum "$1" 2>/dev/null | awk '{print $1}')"
    if printf '%s' "$out" | grep -Eq '^[0-9a-f]{64}$'; then
      printf '%s\n' "$out"
      return 0
    fi
  fi
  shasum -a 256 "$1" | awk '{print $1}'
}

# A file's leading bytes, hex, no whitespace — used instead of `file`, which is
# not guaranteed to exist on a host or in a slim container.
magic4() { od -An -tx1 -N4 "$1" | tr -d ' \n'; }

# The gate that makes the cross-install safe to do at all. `pip --platform`
# resolves wheels for the *target* platform; if it ever resolves one for the host
# instead, the failure is invisible until a host execs the payload and gets
# "bad CPU type" — the original netwatch bug, one layer down. Every shared object
# in the payload must therefore be a macOS arm64 Mach-O (thin arm64, or a
# universal binary, which is what `--platform macosx_11_0_arm64` also matches).
# A universal binary is accepted rather than rejected because it is a real
# resolution outcome, not a mistake; an ELF, or an x86_64-only Mach-O, is the
# payload lying about the architecture it declares.
assert_binaries_for_target() {  # <directory>
  local bad=0 f magic cputype
  while IFS= read -r f; do
    magic="$(magic4 "$f")"
    case "$magic" in
      cffaedfe)
        cputype="$(od -An -tx1 -j4 -N4 "$f" | tr -d ' \n')"
        if [ "$cputype" != "0c000001" ]; then
          echo "Mach-O but not arm64 (cputype $cputype): $f" >&2
          bad=1
        fi
        ;;
      cafebabe|cafebabf)
        : ;; # universal binary: contains arm64 by construction of the tag match
      *)
        echo "not a macOS binary (magic $magic): $f" >&2
        bad=1
        ;;
    esac
  done < <(find "$1" \( -name '*.so' -o -name '*.dylib' \) -type f)
  [ "$bad" = 0 ] || die "refusing to publish: the payload contains binaries for another platform."
}

assert_macho_arm64() {  # <file>
  [ "$(magic4 "$1")" = "cffaedfe" ] ||
    die "not a 64-bit Mach-O: $1"
  # cputype follows the magic: 0x0100000c little-endian is CPU_TYPE_ARM64.
  [ "$(od -An -tx1 -j4 -N4 "$1" | tr -d ' \n')" = "0c000001" ] ||
    die "not an arm64 Mach-O: $1"
}

# --- the wheel the tests ran against -----------------------------------------
# Exactly one wheel is expected: several would make "which one is the payload" a
# guess, and no wheel means the payload would build without the app inside it.
wheels=()
for w in "$WHEEL_DIR"/*.whl; do [ -e "$w" ] || continue; wheels+=("$w"); done
[ "${#wheels[@]}" -eq 1 ] ||
  die "expected exactly one wheel in $WHEEL_DIR/, found ${#wheels[@]}. The build step runs 'python -m build' and uploads dist/**; check that it ran before assembling a payload."

rm -rf "$PAYLOAD"
mkdir -p "$PAYLOAD/bin"

# --- the interpreter, fetched and verified -----------------------------------
mkdir -p "$CACHE_DIR"
tarball="$CACHE_DIR/$PBS_ASSET"
if [ ! -f "$tarball" ] || [ "$(sha256 "$tarball")" != "$PBS_SHA256" ]; then
  log "fetching $PBS_ASSET"
  curl -fsSL "$PBS_URL" -o "$tarball.part"
  mv -f "$tarball.part" "$tarball"
fi
actual="$(sha256 "$tarball")"
[ "$actual" = "$PBS_SHA256" ] ||
  die "sha256 mismatch for $PBS_ASSET
  expected $PBS_SHA256
  actual   $actual"
tar -xzf "$tarball" -C "$PAYLOAD"
assert_macho_arm64 "$PAYLOAD/python/bin/python$PY_SERIES"
log "unpacked cpython-$PBS_PYTHON+$PBS_RELEASE (aarch64-apple-darwin)"

# --- the app and its dependencies, resolved for the target -------------------
# --only-binary=:all: is not a preference, it is the guarantee: no sdist can be
# chosen, so nothing is ever built by the host's compiler and the payload cannot
# pick up an artifact of whatever machine assembled it. --no-compile matters for
# the same reason in the other direction: pip compiles bytecode with *its own*
# interpreter, and the assembling host may be a different CPython than the
# bundled one (the release container is 3.13, the Mac agent's is 3.14). A .pyc
# written by the wrong version is dead weight at best. Bytecode is written on the
# first run, by the interpreter that will read it.
PYTHON_BOOTSTRAP="${PYTHON:-python3}"
command -v "$PYTHON_BOOTSTRAP" >/dev/null 2>&1 ||
  die "$PYTHON_BOOTSTRAP not found - needed only to run pip"
"$PYTHON_BOOTSTRAP" -m pip --version >/dev/null 2>&1 ||
  die "pip is not available via $PYTHON_BOOTSTRAP -m pip - the assembly host needs a python with pip (macOS: the Xcode CLT shim has none; Homebrew's python3 does)"

log "installing ${wheels[0]} + dependencies into $PBS_PLATFORM"
"$PYTHON_BOOTSTRAP" -m pip install \
  --quiet --disable-pip-version-check \
  --no-cache-dir --no-compile --no-warn-script-location \
  --only-binary=:all: \
  --platform "$PBS_PLATFORM" --python-version "$PY_SERIES" \
  --implementation cp --abi "$PBS_ABI" \
  --target "$PAYLOAD/python/$PBS_SITE" --upgrade \
  "${wheels[@]}"

# --- prune what a supervised service must not carry --------------------------
# Only things that cannot load and cannot be reached are removed, because the
# payload's own platform is the one this script cannot execute on: nothing here
# is a guess about what works, everything is a removal of a thing nothing can
# call once it is gone.
#   * pip, twice (site-packages + ensurepip) — a host-side pip in a release-owned
#     directory is an invitation to mutate the thing the release manages, and the
#     console scripts advertise it.
#   * tkinter and the tcl/tk stack — the dashboard is headless; removing
#     _tkinter.so is what makes removing the libraries safe.
#   * include/, config-3.13-darwin, python3-config — for compiling extensions
#     against this interpreter, which nothing on a host does.
#   * idlelib, pydoc_data, man pages, bytecode caches — IDE help text.
#   * site-packages/bin — pip's `--target` puts console scripts there, and each
#     one's shebang names the interpreter that ASSEMBLED the payload, not the
#     bundled one. Nothing execs them (bin/netwatch-dash is the entry point), and
#     a script with the build machine's path baked in is a host's name leaking
#     into an artifact.
rm -rf \
  "$PAYLOAD/python/$PBS_SITE/pip" \
  "$PAYLOAD/python/$PBS_SITE"/pip-*.dist-info \
  "$PAYLOAD/python/$PBS_SITE/bin" \
  "$PAYLOAD/python/lib/python$PY_SERIES/ensurepip" \
  "$PAYLOAD/python/lib/python$PY_SERIES/idlelib" \
  "$PAYLOAD/python/lib/python$PY_SERIES/pydoc_data" \
  "$PAYLOAD/python/lib/python$PY_SERIES/tkinter" \
  "$PAYLOAD/python/lib/python$PY_SERIES/config-$PY_SERIES-darwin" \
  "$PAYLOAD/python/lib/tcl9.0" "$PAYLOAD/python/lib/tk9.0" "$PAYLOAD/python/lib/tcl9" \
  "$PAYLOAD/python/lib/itcl4.3.8" "$PAYLOAD/python/lib/thread3.0.6" \
  "$PAYLOAD/python/lib/libtcl9.0.dylib" "$PAYLOAD/python/lib/libtcl9tk9.0.dylib" \
  "$PAYLOAD/python/lib/python$PY_SERIES/lib-dynload/_tkinter.cpython-$PY_TAG-darwin.so" \
  "$PAYLOAD/python/include" "$PAYLOAD/python/share/man"
# An allow-list, not a deny-list. The invariant is "the payload's bin/ contains
# the interpreter and nothing else"; written the other way round it has to name
# `pip`, `pip3`, `pip3.13`, `idle3`, `idle3.13`, `pydoc3` ... individually, and
# every name it gets wrong is a dev tool that ships. Getting it wrong is silent:
# the first version of this pruned `idle3` and left `idle3.13` beside it, and the
# tarball still packed either way. Here an unrecognised name is removed, so a
# rename in the pinned build fails towards shipping less rather than more.
for entry in "$PAYLOAD/python/bin"/*; do
  case "$(basename "$entry")" in
    python | python3 | "python$PY_SERIES") ;;
    *) rm -rf "$entry" ;;
  esac
done
[ -x "$PAYLOAD/python/bin/python$PY_SERIES" ] ||
  die "the prune removed the interpreter from $PAYLOAD/python/bin"
find "$PAYLOAD/python" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$PAYLOAD/python" -name '*.pyc' -delete 2>/dev/null || true

assert_binaries_for_target "$PAYLOAD/python/$PBS_SITE"
assert_binaries_for_target "$PAYLOAD/python/lib/python$PY_SERIES/lib-dynload"

# --- the payload contract ----------------------------------------------------
# The launcher execs `current/bin/<name>`, and nothing upstream of that exec can
# catch a payload missing it: the manifest key resolves, the sha256 matches, the
# unpack succeeds, `current` flips, and the process exits after one log line.
# See LAUNCHING.md, "The payload contract".
#
# `pwd -P` rather than `pwd`: it resolves `current` to the concrete
# `releases/<version>/` before anything is exec'd, so a launcher that flips the
# symlink underneath a running process cannot leave it exec'ing out of a
# half-replaced tree.
cat > "$PAYLOAD/bin/$PROJECT" <<EOS
#!/bin/sh
# Entry point. Written by scripts/build-payload.sh — edit it there.
set -eu
self="\$0"
case "\$self" in /*) ;; *) self="\$(pwd)/\$self" ;; esac
root="\$(CDPATH= cd -- "\$(dirname -- "\$self")/.." && pwd -P)"
exec "\$root/python/bin/python$PY_SERIES" -m netwatch_dash "\$@"
EOS

# Host wiring ships in the payload beside the producers it wires, so the plists
# a host runs and the code they start come from one release instead of one
# release and one checkout.
if [ -f deploy/install-host.sh ]; then
  mkdir -p "$PAYLOAD/share/$PROJECT/deploy"
  cp deploy/install-host.sh "$PAYLOAD/share/$PROJECT/deploy/"
  chmod +x "$PAYLOAD/share/$PROJECT/deploy/install-host.sh"
  [ -d deploy/plists ] && cp -R deploy/plists "$PAYLOAD/share/$PROJECT/deploy/"
  cat > "$PAYLOAD/bin/$PROJECT-install-host" <<EOS
#!/bin/sh
# Runs THIS payload's copy of deploy/install-host.sh: the wiring is versioned
# with the release, so a host is wired by the version it is running.
set -eu
self="\$0"
case "\$self" in /*) ;; *) self="\$(pwd)/\$self" ;; esac
root="\$(CDPATH= cd -- "\$(dirname -- "\$self")/.." && pwd -P)"
exec /bin/sh "\$root/share/$PROJECT/deploy/install-host.sh" "\$@"
EOS
fi

# --- the producers -----------------------------------------------------------
# D1: the producers live in this repo and ship in this release, so one artifact
# answers both "which dashboard is this host running?" and "which producer?".
# They are NOT installed from here — they install to stable paths and are never
# exec'd out of `current/`, so a bad payload cannot take alerting down (schema
# §4). What the copy in the payload is *for* is comparison: it is the reference
# the drift count is measured against, which is how an installed producer becomes
# a versioned thing instead of a file that was edited on a machine once.
if [ -d producers ]; then
  mkdir -p "$PAYLOAD/share/$PROJECT"
  cp -R producers "$PAYLOAD/share/$PROJECT/producers"
  [ -d schema ] && cp -R schema "$PAYLOAD/share/$PROJECT/schema"
fi

# --- what this payload is ----------------------------------------------------
# /healthz reports a dashboard version, a producer version and a drift count, and
# all three have to be traceable to one release. The manifest's `sha256` covers
# the payload; this file is what makes the payload able to *say* what it is
# without network access or a version string compiled into the code.
#
# Written by the interpreter this script already needs in order to run pip, and
# written to be read back: it is the file /healthz serves, and a hand-assembled
# JSON string is one stray byte away from a payload that cannot report on itself.
# It is generated AFTER the prune so that what it lists is what is in the payload
# — an earlier version of this recorded the preinstalled pip as a dependency of
# a payload that no longer has pip in it.
mkdir -p "$PAYLOAD/share/$PROJECT"

NB_NAME="$PROJECT" NB_VERSION="$VERSION" NB_COMMIT="${BUILDKITE_COMMIT:-}" NB_TARGET="$TARGET" \
NB_BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
NB_PBS_RELEASE="$PBS_RELEASE" NB_PBS_PYTHON="$PBS_PYTHON" NB_PBS_ASSET="$PBS_ASSET" NB_PBS_SHA256="$PBS_SHA256" \
NB_WHEEL_FILE="$(basename "${wheels[0]}")" NB_WHEEL_SHA256="$(sha256 "${wheels[0]}")" \
NB_SITE="$PAYLOAD/python/$PBS_SITE" NB_PRODUCERS="$PAYLOAD/share/$PROJECT/producers" \
"$PYTHON_BOOTSTRAP" - <<'PY' > "$PAYLOAD/share/$PROJECT/build-info.json"
import hashlib, json, os, pathlib, sys

env = os.environ

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()

# Every installed distribution, from its own metadata rather than from what we
# asked for: `--target` resolution may pull in more than the direct requirements
# (and drops nothing), and the payload's version of a dependency is the resolved
# one. Versions are recorded, not pinned — see docs/release-payload.md.
site = pathlib.Path(env["NB_SITE"])
deps = {}
for dist_info in sorted(site.glob("*.dist-info")):
    name, _, version = dist_info.name[: -len(".dist-info")].rpartition("-")
    deps[name.lower().replace("_", "-")] = version

# The producers' digests are what let a host answer "is the netwatch running here
# the one this release ships?" — the drift count's reference side. README.md is a
# document, not a producer, so it is not part of the comparison set.
producers = {}
producer_dir = pathlib.Path(env["NB_PRODUCERS"])
if producer_dir.is_dir():
    for f in sorted(producer_dir.iterdir()):
        if f.is_file() and f.name != "README.md":
            producers[f.name] = sha256(f)

json.dump(
    {
        "name": env["NB_NAME"],
        "version": env["NB_VERSION"],
        "tag": f"v{env['NB_VERSION']}",
        "commit": env["NB_COMMIT"],
        "target": env["NB_TARGET"],
        "built_at": env["NB_BUILT_AT"],
        "python": {
            "source": "python-build-standalone",
            "release": env["NB_PBS_RELEASE"],
            "version": env["NB_PBS_PYTHON"],
            "asset": env["NB_PBS_ASSET"],
            "sha256": env["NB_PBS_SHA256"],
        },
        "wheel": {"file": env["NB_WHEEL_FILE"], "sha256": env["NB_WHEEL_SHA256"]},
        "dependencies": deps,
        "producers": producers,
    },
    sys.stdout,
    indent=2,
)
print()
PY

# Pack time is where the execute bit is normalised, because it does not survive
# the trip on its own: the artifacts API's `curl -o` and a `cp` both create files
# under the machine's umask, and a host's first cold start is exactly where a
# payload it cannot exec gets noticed (mac-studio, 2026-09-16). It is set here as
# well as in release-artifacts.sh because the smoke gate runs this tree directly,
# before anything is packed.
chmod 0755 "$PAYLOAD/bin"/*
[ -f "$PAYLOAD/share/$PROJECT/deploy/install-host.sh" ] &&
  chmod 0755 "$PAYLOAD/share/$PROJECT/deploy/install-host.sh"

log "assembled payload root $PAYLOAD (as $TARGET)"
