#!/usr/bin/env bash
#
# Produces the files that get attached to the GitHub Release.
#
# genproj seeds this file once; after that it is yours. `scripts/` is app-owned,
# so regeneration never overwrites it — unlike .buildkite/pipeline.yml, which is
# genproj's and is rewritten on every regeneration.
#
# Contract: write the files to attach into $OUT_DIR (default: release/). The
# release step uploads every file it finds there and nothing else. Producing no
# files is valid: the release then carries notes and no assets.
#
# Called as: bash scripts/release-artifacts.sh <version>
# The version is the tag without its `v` prefix, e.g. "1.2.4" for tag v1.2.4.
#
# This script PACKS and PUBLISHES; it does not assemble. The payload root is
# built by scripts/build-payload.sh, which the smoke gate also calls, so the tree
# this packs and the tree CI ran are one tree. That split is deliberate; see the
# header of build-payload.sh for why the assembly cannot live in the build step.
set -euo pipefail

VERSION="${1:?usage: release-artifacts.sh <version>}"
OUT_DIR="${OUT_DIR:-release}"

# --- the declared target -----------------------------------------------------
# The manifest key is a CONSTANT here, never derived from the asset filename.
# The seeded version of this script parsed `target` back out of
# `basename "$file"`, which made the filename the declaration and wrote the
# manifest to agree with whatever had been packed. With the triple declared
# upstream (`github-release.target`, see docs/genproj-target-gap.md) the filename
# becomes an assertion instead: $ASSET is checked below and the manifest key is
# written from $TARGET. Publishing this payload under `any` would put an arm64
# Mach-O interpreter in front of an Intel Mac, which installs cleanly and then
# dies at exec with "bad CPU type".
PROJECT="netwatch-dash"
TARGET="${RELEASE_TARGET:-aarch64-apple-darwin}"
ASSET="$PROJECT-$TARGET.tar.gz"

log() { echo "$*"; }
die() { echo "$*" >&2; exit 1; }

# The docker plugin forwards only the env vars NAMED in its `environment:` list,
# so BUILDKITE_COMMIT is unset inside the release container -- which is how the
# first published manifests carried `"commit": ""`. The step has a checkout of
# the commit it is releasing, so git answers when the environment does not.
COMMIT="${BUILDKITE_COMMIT:-$(git rev-parse HEAD 2>/dev/null || true)}"

# sha256sum is GNU coreutils; macOS ships shasum. The release step runs in a
# Linux container today, but this file has to work natively on the Mac agent too
# (the smoke gate's host), so the two are not interchangeable. The digest is
# validated rather than trusted: mac-studio resolves `sha256sum` to an unexpected
# /sbin/sha256sum wrapper (2026-09-20), and a helper that silently returns a
# decorated string would report a mismatch against a pin that is correct.
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

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
PAYLOAD="$WORK/payload"

# --- assemble -----------------------------------------------------------------
# The release step downloads what the build step uploaded into dist/ before
# calling this, so the wheel the assembly installs is the exact one pytest ran
# against. Everything the payload contains — the interpreter, the resolved
# dependencies, the producers, build-info.json — is decided in one place, and the
# gates that refuse a payload containing another platform's binaries live there
# too.
bash scripts/build-payload.sh "$VERSION" "$PAYLOAD" dist

# --- pack --------------------------------------------------------------------
# `tar -C "$PAYLOAD" .` puts the CONTENTS of the payload at the root of the
# tarball, so the launcher's `current/bin/$PROJECT` exists after the unpack. The
# execute bit was normalised by build-payload.sh; it does not survive the trip on
# its own (the artifacts API's `curl -o` creates files under the machine's
# umask), which is why it is set at assembly time rather than here.
#
# The name is asserted against the declared key rather than used to derive it, so
# a rename cannot silently re-key the manifest — the publish stops instead.
[ "$ASSET" = "$PROJECT-$TARGET.tar.gz" ] || die "asset name does not match the declared target: $ASSET"
tar -czf "$OUT_DIR/$ASSET" -C "$PAYLOAD" .
log "packaged $ASSET ($(du -h "$OUT_DIR/$ASSET" | cut -f1)) as $TARGET"

# --- the launcher ------------------------------------------------------------
# The launcher is what a host's init system supervises, so a fix to *it* has to
# be shippable the same way a fix to the payload is - otherwise the code that
# supervises everything else only ever changes when a human visits the box (this
# is a real host's failure: mac-studio ran a launcher four commits stale).
#
# It is published verbatim rather than packed - it is one script, not a payload
# tree - and advertised in the manifest's "launcher" entry, which the running
# launcher reads to replace itself before it execs.
if [ -f scripts/fetch-launch.sh ]; then
  cp scripts/fetch-launch.sh "$OUT_DIR/fetch-launch.sh"
  chmod +x "$OUT_DIR/fetch-launch.sh"
  log "published scripts/fetch-launch.sh as fetch-launch.sh"
fi

# --- manifest (a launcher's only stable URL) ---------------------------------
# releases/latest/download/manifest.json is what something fetching without
# knowing the version reads. It is written last, after every asset exists, so a
# manifest never advertises a file that is not there.
#
# One key, and it is the declared target: hosts whose candidate list contains
# aarch64-apple-darwin resolve this payload, and hosts that do not fall through
# their whole list and find nothing — which the launcher treats as a supported
# state ("no target for this host"), not an error. There is deliberately no `any`
# key.
{
  printf '{\n'
  printf '  "name": "%s",\n' "$PROJECT"
  printf '  "version": "%s",\n' "$VERSION"
  printf '  "tag": "v%s",\n' "$VERSION"
  printf '  "commit": "%s",\n' "$COMMIT"
  printf '  "assets": {'
  first=1
  for file in "$OUT_DIR"/*.tar.gz; do
    [ -e "$file" ] || continue
    base="$(basename "$file")"
    key="${base#"$PROJECT"-}"
    key="${key%.tar.gz}"
    sha="$(sha256 "$file")"
    [ "$key" = "$TARGET" ] ||
      die "asset $base would be published under '$key' but the declared target is '$TARGET'"
    [ "$first" = 1 ] || printf ','
    first=0
    printf '\n    "%s": { "file": "%s", "sha256": "%s" }' "$key" "$base" "$sha"
  done
  printf '\n  }'
  # The launcher, beside `assets` rather than inside it: `assets` is keyed by
  # *target* and consumed by the launcher's candidate lookup, and the launcher is
  # not a target - a candidate list must never resolve to it. Same one-line
  # shape, so the launcher reads both with the same grep+sed and needs no JSON
  # parser on a host that may not have one.
  if [ -f "$OUT_DIR/fetch-launch.sh" ]; then
    launcher_sha="$(sha256 "$OUT_DIR/fetch-launch.sh")"
    printf ',\n  "launcher": { "file": "fetch-launch.sh", "sha256": "%s" }' "$launcher_sha"
  fi
  printf '\n}\n'
} > "$OUT_DIR/manifest.json"
log "wrote $OUT_DIR/manifest.json"
