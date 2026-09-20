# The release payload

What `scripts/release-artifacts.sh` builds, why it is keyed by a Rust triple, and
what verifies it before it is published.

Built by `bash scripts/release-artifacts.sh <version>`; the release step calls it
with the version it just tagged. Output lands in `release/` (override:
`OUT_DIR=…`), which the release step uploads whole.

## The shape

```
manifest.json                              the launcher's only stable URL
netwatch-dash-aarch64-apple-darwin.tar.gz  the payload, keyed by the triple
fetch-launch.sh                            the launcher, published verbatim
```

Unpacked, the tarball is the payload root — the launcher `exec`s
`current/bin/netwatch-dash`, so `bin/` is at the top:

```
bin/netwatch-dash                 entry point: exec python/bin/python3.13 -m netwatch_dash
bin/netwatch-dash-install-host    runs this payload's own deploy/install-host.sh
python/                           a bundled CPython 3.13.15 (python-build-standalone)
share/netwatch-dash/producers/    the producers this release ships (D1)
share/netwatch-dash/schema/       the data contract they are held to
share/netwatch-dash/deploy/       install-host.sh + the plists it installs
share/netwatch-dash/build-info.json   what this payload is; /healthz reads it
```

## Why the triple is honest here

`dist/` holds a wheel, and the wheel is `py3-none-any` — genuinely
architecture-independent. The payload is not, because it carries an **arm64
macOS Mach-O CPython**, and that is what a host executes. A macOS `python3` is
the Xcode CLT shim: its version is whatever toolchain happens to be installed,
it has no pip, and it is absent from a machine that never ran `xcode-select
--install`. Bundling the interpreter is what makes the payload self-contained,
and it is *also* what makes the triple true rather than merely convenient.

So the release declares `github-release.target: aarch64-apple-darwin` (the
generation invocation is recorded in `docs/genproj-invocation.md`) and publishes
**one** asset under that key. See `docs/genproj-target-gap.md` — briefly: `any`
in that manifest would put an arm64 Mach-O interpreter in front of an Intel Mac,
which installs cleanly and then dies at `exec` with `bad CPU type`.

## The interpreter is pinned by digest

```sh
PBS_RELEASE=20260901  PBS_PYTHON=3.13.15
PBS_SHA256=d3904bd6…   # install_only_stripped, aarch64-apple-darwin
```

The digest was taken from that release's own `SHA256SUMS` and is checked on every
run, cached download or not. Pinning the *version* alone would let a re-published
or mirrored asset change under us without the payload changing name; the digest
is what makes "the interpreter in v1.2.4" mean one set of bytes, the same way
`sha256` in the manifest does for the tarball.

`install_only_stripped` rather than `install_only`: the symbols are dead weight
in a payload nothing debugs.

## Installing the app for a platform we are not on

```sh
pip install --only-binary=:all: \
  --platform macosx_11_0_arm64 --python-version 3.13 --implementation cp --abi cp313 \
  --no-compile --target python/lib/python3.13/site-packages dist/*.whl
```

Three of those flags are load-bearing, not tuning:

- **`--only-binary=:all:`** — no sdist can be selected, so nothing is ever built
  by the assembling host's compiler and the payload cannot absorb an artifact of
  whatever machine produced it.
- **`--no-compile`** — pip compiles bytecode with *its own* interpreter. The
  release container is 3.13 and a dev machine may not be; a `.pyc` written by the
  wrong CPython is at best dead weight. Bytecode is written on first run, by the
  interpreter that will read it.
- **`--platform`/`--python-version`/`--implementation`/`--abi`** — resolve wheels
  for the target, not the host. This is the one place where the payload's
  platform is decided by a string rather than by the machine.

The last point is why the next section exists.

## The gates

The script cannot execute what it builds — the payload's platform is the one it
is not running on. So the payload is *checked*, and the checks are the ones that
would have caught the original bug one layer down:

| Gate | Catches |
| ---- | ------- |
| `assert_macho_arm64 python/bin/python3.13` | an interpreter that is not arm64 Mach-O (magic `cffaedfe`, cputype `0100000c`) |
| `assert_binaries_for_target` over `site-packages/` and `lib-dynload/` | any `.so`/`.dylib` that is ELF, or a Mach-O that is not arm64. Universal binaries are accepted — `--platform macosx_11_0_arm64` matches `universal2`, so that is a real resolution outcome, not a mistake |
| the asset name is asserted, not derived | a rename re-keying the manifest |
| `[ -x …/bin/python3.13 ]` after the prune | a prune that removed the interpreter |

Verified negatively, which is the only way to know a gate is not vacuous: point
`PBS_PLATFORM` at `manylinux_2_17_aarch64` and the run stops with

```
not a macOS binary (magic 7f454c46): …/pydantic_core/_pydantic_core.cpython-313-aarch64-linux-gnu.so
refusing to publish: the payload contains binaries for another platform.
```

`pydantic_core` is the interesting case: every other dependency resolves to a
`py3-none-any` wheel, so it is the one file in the payload whose architecture is
actually decided by a wheel tag.

## What is removed, and on what rule

The prunes are an **allow-list**, not a deny-list. `python/bin/` is reduced to
`python`, `python3`, `python3.13` — anything not on that list is removed, so a
rename in a future pinned build fails towards shipping *less* than *more*. The
deny-list this replaced silently kept `idle3.13` while removing `idle3`, and the
tarball packed either way.

Removed beyond that: pip (twice — `site-packages` and `ensurepip`), the tkinter
and tcl/tk stack, `include/` and the config dirs for compiling extensions against
this interpreter, `idlelib`/`pydoc_data`, man pages, `__pycache__`, and pip's
`--target` console scripts (their shebangs name the *assembling* host's
interpreter, which is a build machine's path leaking into an artifact).

Nothing here is a guess about what works: everything removed is something that,
once removed, nothing can call. `libpython3.13.dylib` is deliberately **kept** —
the executable does not reference it, so it looks prunable, but "looks prunable"
is not a thing to bet 17 MB of someone else's runtime on from the wrong platform.

## `build-info.json`

Generated *after* the prune, by the interpreter the script already needs for pip.
An earlier version ran before it and recorded the preinstalled pip as a
dependency of a payload with no pip in it.

```json
{ "version": "1.2.4", "tag": "v1.2.4", "target": "aarch64-apple-darwin",
  "python": { "release": "20260901", "version": "3.13.15", "sha256": "…" },
  "wheel": { "file": "…", "sha256": "…" },
  "dependencies": { "fastapi": "0.141.1", … },
  "producers": { "netwatch": "d728858a…", … } }
```

That is the raw material for `/healthz`'s three answers — dashboard version,
producer version, drift count — all traceable to one manifest `sha256`. The
`producers` digests are the reference side of the drift count: the payload's copy
of a producer is what the host's copy is compared against, which is how an
installed producer stops being "a file someone edited on a machine once".

## The manifest

One key, written from the constant `TARGET`, never parsed back out of the asset
filename (the seeded script did that, which made the filename the declaration).
The loop still asserts `key == TARGET`, so a rename stops the publish.

No `any` key, deliberately. Verified both directions against a local HTTP server
and the real `scripts/fetch-launch.sh`:

| Host as seen by the launcher | Result |
| ---------------------------- | ------ |
| `Linux/aarch64` (this container) | `the manifest publishes no target for this host - starting what is installed`; nothing downloaded beyond the manifest. The cold-host exit is non-zero and says so |
| `Darwin/arm64` | `installed 0.1.0 (aarch64-apple-darwin)` — downloaded, sha256-verified, unpacked to `releases/0.1.0`, `current` flipped, then exec'd |

On the Darwin path the exec failed with `Cannot run macOS (Mach-O) executable in
Docker: Exec format error` — which is the *expected* result on Linux, and also
proof the entry-point shim resolved: `bin/netwatch-dash` found the interpreter
and the kernel refused the wrong-platform binary.

## How CI builds it, and the gap that remains

The release step runs in the fleet's container (`python:3.13-slim`,
`linux/arm64`, from `.buildkite/pipeline.yml`), so this script assembles the
payload **cross-platform** and the gates above are what make that safe rather
than trusting. The agent is natively arm64 macOS — the docker plugin is the only
reason a macOS binary cannot execute there.

So the payload is currently checked but never *run* before publication. Closing
that is the macOS smoke step: run `bin/netwatch-dash` on the exact artifact, with
a temporary `HOME` and `NETWATCH_DASH_STATE`/`NETWATCH_DASH_GWCSV`/
`NETWATCH_DASH_CONFIG` pointed at fixtures and a random loopback port, because a
native step is **not** sandboxed — `$HOME` on that machine holds live alerting
state. `buildkite-agent pipeline upload` can *add* that step, but it cannot
re-provision an existing one, so running the payload *build* natively means
editing a genproj-owned file. That is recorded as a trap in
`docs/genproj-target-gap.md` rather than done quietly.

## Open

- **Dependency versions are recorded, not pinned.** A rebuild of the same commit
  tomorrow may resolve a newer patch of a dependency. `build-info.json` makes
  that answerable after the fact; a lock file would prevent it. Decide before
  something depends on byte-reproducibility.
- **The producers ship but are not adopted.** They are in the payload as the
  comparison reference; `deploy/install-host.sh` does not yet copy them to
  `~/netwatch/` and `~/.local/bin/`. That is the "co-release, co-adopt" half of
  D1, and it overwrites files on the alerting path, so it is a deliberate
  decision rather than a follow-up (memo v3 §6.1).
- **`/healthz` does not read `build-info.json` yet** — the FastAPI app does not
  exist. The file is written and its shape is fixed by what `/healthz` owes.
