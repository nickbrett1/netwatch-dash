# The release payload

What the payload is, why it is keyed by a Rust triple, and what verifies it
before it is published.

Three scripts, one payload:

| Script | Job | Called by |
| ------ | --- | --------- |
| `scripts/build-payload.sh <version> <root> [<wheel-dir>]` | **assembles** the payload root | the generated smoke step, the generated release step, and `release-artifacts.sh` as a fallback |
| `scripts/release-artifacts.sh <version> [<root>]` | **packs** it into `release/` and writes the manifest | the release step, with the version it just tagged |
| `scripts/smoke-launch.sh <root-or-wheel-dir>` | **runs** it before publication | the smoke step, on the macOS agent |

`release/` is uploaded whole by the release step (override: `OUT_DIR=…`).

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

## Why assembly is its own script

The payload root has to exist before it can be either **packed** or **run**, and
those two happen in different Buildkite steps on different hosts. Since genproj
gained the assembler hook (`docs/genproj-target-gap.md` §8) the generated steps
call it directly, so both consumers assemble the *same* root at `payload/`:

```
build step   (container)      python -m build              →  dist/*.whl
smoke step   (native macOS)   build-payload.sh → smoke-launch.sh payload
release step (container)      build-payload.sh → release-artifacts.sh → publish
```

The build step's own commands are genproj-owned and cannot be extended, which is
why the assembly is not there — and its output is a wheel, not a payload root, so
there is nothing there to assemble from anyway. It uploads `dist/**`, and both
consumers download that and hand it to the one assembler.

Because the gate assembles through the same script and into the same root the
release packs, it runs the real payload — same interpreter, same resolved
dependencies, same entry-point shim the tarball will contain — rather than a
thinner stand-in. Running the wheel in place would be a gate that passes whether
or not the assembly works, which is decoration. (The one honest difference
between what is smoked and what is released is two `build-info.json` fields; see
"Where the smoked payload and the released one differ" below.)

`release-artifacts.sh` still *can* assemble, and does when it is run on its own
or the assembled root is absent — but in CI it finds `payload/bin/netwatch-dash`
already there and packs it as-is. That keeps the script usable by hand without
making it a second assembler.

The cost is that the assembly runs twice per release (once on the Mac to smoke,
once in the release container to pack). That is accepted: it buys a gate that
executes the artifact, and the assembly is a cached download plus a pip resolve.

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

There are two layers, and they catch different things.

**At assembly time** (`build-payload.sh`), because the assembling host cannot
execute what it builds — the payload's platform is generally not the assembler's
— so the payload is *checked*, with the checks that would have caught the
original bug one layer down:

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

**At smoke time** (`smoke-launch.sh`, on the Mac), because checks are not
execution. The gate assembles the payload, then refuses to pass unless all of
these hold:

| Check | Catches |
| ----- | ------- |
| the payload root holds `bin/netwatch-dash` | a payload that unpacks and then exits after one log line |
| the entry point is executable | a lost execute bit |
| `python/bin/python3.13` is a Mach-O — via `file` | an interpreter that is not a macOS binary at all |
| the entry point answers `--version` or `--help` under a portable timeout | a payload that cannot start, which is the failure that used to reach a host first |

It is fail-closed on purpose, and the release step `depends_on` it, so a payload
that cannot start cannot be published. Its output is captured and echoed on
failure: this runs on an agent nobody logs into interactively, and the difference
between *cannot exec* and *raised on import* is the whole diagnosis. The gate
prints exactly that — e.g. on a Linux host, `Cannot run macOS (Mach-O) executable
in Docker: Exec format error`, which is the correct verdict there and proof the
checks are live.

The probes are deliberately `--version`/`--help` and nothing more. A native step
is **not** sandboxed: `$HOME` on that machine holds live alerting state, and the
build step's own `pip install -e` already writes to the agent's python. If this
gate is ever upgraded to start the real server, it needs a temporary `HOME` and
`NETWATCH_DASH_STATE`/`NETWATCH_DASH_GWCSV`/`NETWATCH_DASH_CONFIG` pointed at
fixtures on a random loopback port — not the host's real ones.

### Where the smoked payload and the released one differ

Precisely, because the difference is real and worth naming: they are assembled by
the same script from the same wheel on the same commit, so they agree on the
interpreter, every resolved dependency, the entry point and the producers. They
differ in exactly two fields of `build-info.json`:

| Field | Smoke step | Release step |
| ----- | ---------- | ------------ |
| `version` | `0.0.0-smoke` (`SMOKE_VERSION`) | the tag, e.g. `0.1.9` |
| `built_at` | when the gate ran | when the release ran |

Both are unknowable at smoke time: the release step is what *creates* the tag, so
at build time there is no version to record. The consequence for `/healthz` is a
rule, not a footnote: **the payload cannot be the source of its own release
version.** It reports what it is (target, commit, interpreter, wheel digest,
dependency versions, producer digests) and the release version comes from the
outside — the manifest, or `FETCH_LAUNCH_VERSION`, which `scripts/fetch-launch.sh`
exports to the process it starts. `build-info.json`'s `version` is a build-time
label and is honest only when the release step wrote it.

The same fact is why the `commit` field falls back to `git rev-parse HEAD`: the
docker plugin forwards only the env vars *named* in its `environment:` list, and
`BUILDKITE_COMMIT` is not one of them, so inside the release container it is
unset. v0.1.7–v0.1.9 shipped with `"commit": ""` before that fallback existed.

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

## How CI builds it

The pipeline is genproj's, and since the singular-`target` fix (2026-09-20,
`docs/genproj-target-gap.md` §7) it provisions the right steps for a darwin
target — no container, because a macOS binary cannot be linked in a Linux one:

| Step | Host | Runs |
| ---- | ---- | ---- |
| `build` | container, queue `mac-studio-linux` | `python -m build` → uploads `dist/**` |
| `smoke_aarch64_apple_darwin` | native macOS, same queue | downloads `dist/**`, `build-payload.sh … payload dist`, `bash scripts/smoke-launch.sh payload` |
| `release` | `python:3.13-slim` container, `linux/arm64` | downloads `dist/**`, `build-payload.sh "$VERSION" payload dist`, `release-artifacts.sh`, `gh release create` |

Both the build and smoke steps carry `RELEASE_TARGET`, and the release step
`depends_on` the smoke gate.

The release step stays containerised by design: it only downloads artifacts and
calls `gh`, so it needs no macOS toolchain and its `apt-get` bootstrap remains
valid. The smoke step is native *because* it has to be — a Mach-O binary cannot
run in the Linux container the docker plugin would provide, which is exactly why
the containerised release step fetches artifacts through the agent API instead of
`buildkite-agent artifact download`.

One consequence worth stating: the build step's commands run on the Mac host
*outside* any container, so `python -m pip install -e ".[dev]"` installs the
project into the agent's own python (Homebrew 3.14 on mac-studio). That is
genproj's generated step, not something this project chose.

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
- **`/healthz` reads `build-info.json` and stops there.** It reports the
  payload's identity, the producer comparison and the whitelisted config
  (`docs/app.md`); the status is deliberately `unknown` until the readers land,
  and `data.drift_count` is `null` for the same reason. `bin/netwatch-dash
  --version` / `--help` are answered before the ASGI stack is imported, which is
  what the smoke gate probes; no endpoint reads `events.jsonl` or the CSV yet.
- **The smoke gate runs the payload but does not serve it.** The generated step
  runs `build-payload.sh` → `smoke-launch.sh`, and the gate exercises
  `--version`. Starting the app and curling `/healthz` on the assembled payload —
  with a temp `HOME` and a loopback port, so it cannot touch the live alerting
  state or collide with the real service on 8791 (memo v3 §7) — is the remaining
  half, and it is `scripts/smoke-launch.sh`'s to add (app-owned, so it survives a
  regen).
