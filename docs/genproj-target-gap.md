# genproj gap: a single artifact that *is* platform-specific has no target label

**Status:** ✅ **fixed upstream 2026-09-20** — a singular `github-release.target`
was added (see §7). The gap below is kept as the record of what it looked like.
**Found:** 2026-09-20 · **Affects:** any non-rust genproj project that bundles a
platform-specific interpreter (a python-build-standalone CPython, a vendored
Node, a JRE)
**Repos:** `nickbrett1/genproj` (generator) · `nickbrett1/netwatch-dash` (first
project to hit it)

## 1. What genproj assumes

genproj's release model has exactly two shapes, and the vocabulary in
`src/generator/target-labels.js` is built to enforce that there are only two:

1. **Architecture-specific** — the primary language is **rust**. A target is a
   Rust triple, one build step per triple, and the artifact publishes under that
   triple.

   ```js
   export const TARGET_LABELS = Object.freeze([
     "aarch64-apple-darwin",
     "x86_64-unknown-linux-musl",
     "aarch64-unknown-linux-musl",
   ]);
   ```

2. **Architecture-independent** — any other language. One artifact, published
   under the universal key. The key's contract is stated in the same file:

   ```js
   /**
    * The universal target key: an artifact that is not architecture-specific (a
    * Node bundle, a pure-python `.pyz`) publishes under this key rather than
    * claiming a triple.
    */
   export const UNIVERSAL_TARGET = "any";
   ```

Two generation guards turn that mental model into hard rejections:

- `validateReleaseTargets` **throws** if a non-rust project declares targets:
  > `A "{language}" project's output is architecture independent, so it ships
  > as one asset under the universal key instead.`
- `validateFetchLaunch` **blesses** node/python precisely on the grounds of
  shape 2:
  > `A node or python project is allowed: its release publishes one
  > architecture-independent asset under the universal key, which the launcher's
  > candidate list already falls back to.`

And the seeded `scripts/release-artifacts.sh` defaults every non-rust project to
shape 2 by packing `dist/` as `<project>-any.tar.gz`.

## 2. The gap

There is a **third** shape genproj has no name for:

> **One artifact, built once, that is genuinely platform-specific.**

A Python project that bundles its own interpreter is exactly this. Our payload
(built from python-build-standalone) contains an **arm64 macOS CPython**; the
`.pyz`/wheel on top of it is portable, but the thing that executes them is a
Mach-O arm64 binary. The artifact is architecture-specific, and there is no way
to say so:

- Declaring a target is **refused** — `validateReleaseTargets` fires the moment
  `github-release.targets` is non-empty on a non-rust language (§1, guard 1).
- So the only thing left is `any` — whose documented meaning is the *opposite*
  of what we are shipping (§1, key doc: "an artifact that is **not**
  architecture-specific").

The result is a payload that **is** architecture-specific, published under a key
that **asserts it isn't**. genproj has no target vocabulary for the truth and
actively rejects the correct answer, so the generator's own tooling labels the
artifact dishonestly.

### Why the failure is silent and delayed

The mislabel does not fail at build, at release, or at install. It fails at
`exec`, on a *different* host, later:

1. `scripts/release-artifacts.sh` names the asset `…-any.tar.gz` and the
   manifest key is `any`.
2. A launcher's candidate list for **every** host ends in `any`
   (`targetCandidates` appends `UNIVERSAL_TARGET` last, "no special case for
   `any` in the consumer"). So a host with no triple of its own — an Intel Mac,
   an x86-64 Linux box — resolves the arm64 macOS payload and **installs it**.
3. `sha256` **passes**, because the file is intact. The manifest is honest about
   the bytes and wrong about the platform.
4. `exec current/bin/<name>` fails with `ENOEXEC` / "bad CPU type". The host is
   now "installed" and broken.

Nothing in the chain can distinguish "architecture-independent, so `any` is
right" from "architecture-specific but mislabelled as `any`". The label is the
only signal, and it lies.

## 3. The fix

The fix is **not** a build matrix, and **not** reuse of `github-release.targets`
(which means "N build steps, one per platform"). It is a **declared label for a
single, platform-specific artifact** — the missing third shape. Concretely:

- Add a knob that says "this project publishes **one** artifact, and its label is
  `<triple>`" (a singular `github-release.target`, or a `targets` **entry**
  explicitly marked as non-matrix). The vocabulary is the existing
  `TARGET_LABELS`; nothing new is invented.
- `validateReleaseTargets` stops treating "declared a target" as "is rust" and
  instead asks whether the artifact is genuinely platform-specific.
- `validateFetchLaunch`'s node/python blessing is reworded: node/python is
  *permitted* to be architecture-independent, not *guaranteed* to be.
- The seeded `scripts/release-artifacts.sh` packs under the declared label
  instead of `any`.

**No consumer change is needed, and this is the encouraging part.** The launcher
already refuses to construct a label from `uname` — it intersects its
uname-derived candidate list with the manifest's keys:

```sh
for candidate in $(candidates); do
  if grep -q "\"${candidate}\"[[:space:]]*:" "$manifest"; then
    label="$candidate"; break
  fi
done
```

`Darwin`/`arm64` yields `["aarch64-apple-darwin", "any"]`, so an asset keyed
`aarch64-apple-darwin` resolves *first*, and a host that cannot run it (no such
triple in its list, or none published) falls through or fails open — the
correct, honest behaviour. The whole gap is **generation-time**: the vocabulary
is refused for non-rust, and the seeded script defaults to `any`. Both sides of
the runtime contract are already right.

### Why `any` cannot simply be reinterpreted

`any` is *load-bearing everywhere it appears*: it is appended to every host's
candidate list, and the consumer deliberately has no special case for it. Making
`any` mean "whatever the author meant" would remove the one guarantee that lets
a launcher fall back safely. The honest fix is a distinct, declared label — a
new fact, not a reinterpretation of an old one.

## 4. Working around it today (netwatch-dash)

> **Superseded** by §7, and by the §5 item below landing. The filename trick is
> recorded because it is what the first real payload would have shipped; nothing
> relies on it now — `scripts/release-artifacts.sh` writes the manifest key from a
> declared constant and *asserts* the asset name against it, and
> `docs/release-payload.md` has the current shape.

We do **not** need to wait for the genproj change. `scripts/` is **app-owned**
(it is seeded once and never overwritten on regeneration), and the seeded
`release-artifacts.sh` derives the manifest key **from the asset filename**:

```sh
base="$(basename "$file")"
target="${base#"{{projectName}}"-}"
target="${target%.tar.gz}"
```

So packing our payload as `netwatch-dash-aarch64-apple-darwin.tar.gz` keys it
`aarch64-apple-darwin` — with **no genproj change at all**. Our launcher's
candidate list puts that triple first and resolves it correctly; a host that
cannot run it resolves nothing and fails open rather than installing a binary it
cannot exec.

**Consequences to hold ourselves to:**

- Publish **only** the real triple. Do **not** also publish an `any` asset, or
  Intel/Linux hosts would fall back to it and reintroduce the silent failure.
- The upstream memo stays open because the workaround is *app-owned*: every
  future non-rust project that bundles an interpreter must rediscover the same
  filename trick. The generator should be able to say it.

## 5. Recommendation

1. **Now (netwatch-dash):** declare `github-release.target` = `aarch64-apple-darwin`
   and pack `dist/` as `…-aarch64-apple-darwin.tar.gz`; publish no `any` asset.
   (The label itself is only *honest* once the payload bundles the interpreter —
   see §7.) → **Done**, landed with the bundled interpreter rather than ahead of
   it (`scripts/release-artifacts.sh`, `docs/release-payload.md`). Verified both
   ways against the real launcher: a `Darwin`/`arm64` host resolves the payload
   and installs it; a `Linux`/`aarch64` host resolves **nothing** and starts what
   it has. And done **without regenerating** — see trap 1 for why a regen would
   not have been the thing that did it.
2. **Upstream (genproj):** add the singular declared target so the missing third
   shape has a name, and reword the two guards. → **Done 2026-09-20; see §7.**

## 6. References

| Fact | Location (genproj `main`, 2026-09-20) |
| --- | --- |
| `UNIVERSAL_TARGET = "any"` + its contract | `src/generator/target-labels.js` |
| `TARGET_LABELS` = the 3 Rust triples only | `src/generator/target-labels.js` |
| `targetCandidates` appends `any` last, per host | `src/generator/target-labels.js` |
| Non-rust targets **refused** | `src/generator/project-validation.js` (`validateReleaseTargets`) |
| node/python blessed as arch-independent | `src/generator/project-validation.js` (`validateFetchLaunch`) |
| `dist/` packed as `<project>-any.tar.gz` | `src/generator/templates/github-release-artifacts.template` |
| Manifest key derived **from the filename** | same template (manifest block) |
| Launcher intersects manifest keys, never builds a label | `src/generator/templates/scripts-fetch-launch.sh.template` → `candidates()` |

## 7. Resolution (upstream, 2026-09-20)

genproj named the missing third shape. The vocabulary gained a **singular**
declared label, distinct from the plural build matrix:

| Knob | Meaning | Emits |
| --- | --- | --- |
| `github-release.targets` (plural) | native build **matrix**: one build step per triple (rust only) | N steps, `build/<target>/` each |
| `github-release.target` (singular) | **one** artifact, genuinely platform-specific | the single `dist/` payload, keyed by the triple |

`validateReleaseTargets` now enforces all four cases: both set -> refused
(ambiguous); a `target` not in `TARGET_LABELS` -> refused; `targets` on a
non-rust language -> refused and *points at `target`*; `target` on rust ->
refused and *points at `targets`* (a one-entry matrix). The template packs
`dist/` under `githubReleaseDistTarget = singleTarget || UNIVERSAL_TARGET`, and
`validateFetchLaunch` was reworded to say node/python is **permitted** to be
architecture-independent, not **guaranteed** to be.

**Consequence for us:** declaring `github-release.target` =
`aarch64-apple-darwin` is now the supported way to publish our payload, so the
filename-derived workaround in §4 is no longer needed. Our launcher already
resolves that triple first (`Darwin`/`arm64` -> `["aarch64-apple-darwin",
"any"]`), and the singular target deliberately does **not** create a build
matrix - the pipeline still runs one build step uploading `dist/**`.

### Seven traps to know before regenerating

1. **A regen does not refresh `scripts/release-artifacts.sh`.** Under
   `src/generator/genproj-overwrite.js`, `scripts/` is app-owned and a diverged
   app file is *never* replaced - only `cloud_login.sh` and the
   wrangler/doppler helpers are genproj-owned scripts. Ours is therefore safe
   from a regen, and also *invisible* to one: the generator's newer wording (the
   singular-target key) will not reach it. That is the right trade here — ours is
   the one that bundles an interpreter, and the seeded script packs a plain
   `dist/`, so taking the generator's copy would mean giving up the
   launcher-shaped payload again (`docs/release-payload.md`).
2. **A regen *does* overwrite `pyproject.toml`** (infra). Our
   `[tool.ruff] extend-exclude = ["producers"]` is not generator-owned and would
   be lost - re-apply it after the regen, or upstream the exclusion.
3. **Do not put the triple on the *current* payload.** `dist/` today is the
   wheel + sdist from `python -m build` - pure-python, i.e. genuinely
   architecture-*independent*. Labelling that `aarch64-apple-darwin` would be
   the mirror-image lie. The triple is honest only once `dist/` bundles the
   arm64 CPython (the launcher-shaped tree in the §10.2 rewrite). Land the
   declaration and the bundled payload together, never the label alone.
   — Done: the declaration and the bundled interpreter landed in one commit.
4. **A regen reverts `.buildkite/pipeline.yml`.** This was the trap that made the
   §7 fix necessary — the release step was provisioned *inside* a Linux/arm64
   container, so a macOS payload could never be executed by CI. §7's change means
   the generator now emits that provisioning itself (native build step, native
   `smoke_<target>` gate), so **this trap retires on the first regen**: the
   regenerated pipeline should carry a no-plugin build step and the smoke gate.
   Verify it against `.buildkite/pipeline.yml` after regenerating — that check is
   the point of regenerating at all. (Still true and worth knowing:
   `buildkite-agent pipeline upload` can only **add** a step, never re-provision
   an existing one, so pipeline provisioning can never be moved into an app-owned
   file. If a regen reverts the pipeline to a containerised build, the fix has
   been lost upstream and this trap returns.)
   — **Retired 2026-09-20**, by the regen at `b9a2a58`. The pipeline came back
   with a containerised build step carrying `RELEASE_TARGET` in the docker
   plugin's `environment:` list, a native `smoke_aarch64_apple_darwin` gate, and
   a release depending on both — i.e. §9's fix, emitted rather than hand-patched.
   §9's `TEMPORARY` plugin block is gone with it. Re-verify on any future regen;
   the check is still the point of regenerating.
5. **A regen reverts `.gitignore`**, and genproj's copy does not ignore `dist/`,
   `release/` or `payload/`. `release/` holds a ~20 MB tarball with a CPython
   inside it, so an un-ignored `release/` is one `git add -A` away from committing
   a binary to history. Re-add all three entries after a regen (this one is worth
   upstreaming).
6. **A regen overwrites `RELEASING.md`**, which carries a blockquote pointing
   readers at this project's payload shape. It is infra, so the pointer goes with
   it; re-add it after a regen. Everything durable lives in `docs/`, which
   genproj does not emit and a regen does not touch.
7. **A regen overwrites `pyproject.toml`'s `[project.optional-dependencies] dev`
   list**, which is where the test dependencies live. `pytest` and `ruff` come
   back; anything the app's own tests need does not. Today that is `httpx2`
   (Starlette's `TestClient` is an httpx2 client — with plain `httpx` it still
   runs, but emits a `StarletteDeprecationWarning`; starlette 1.6.0), so the
   endpoint tests are unbuildable after a regen until it is re-added. Same
   mechanism as trap 2, different list.

## 8. Second gap: a smoke gate that assumes the build step produced the payload

Found while wiring the §7 fix, fixed on our side (`scripts/build-payload.sh`),
and now **open upstream as `nickbrett1/genproj` PR #38** in exactly the shape
argued for below.

The smoke gate genproj landed with §7 is right about what it wants to do: run the
payload before publishing it, on the one host in the fleet that can execute a
macOS binary, and let the release depend on it. But its contract for *where the
payload is* cannot hold for a payload that has to be assembled:

```yaml
      - |
        for pattern in "dist/**"; do
          buildkite-agent artifact download "$pattern" .
        done
      - bash scripts/smoke-launch.sh "dist"
```

with the seeded script documenting it as:

> Each root is a LAUNCHING.md payload: it must contain `bin/<name>` …

`dist/` is the payload root for a project whose build output **is** its payload —
a Node bundle, a wheel run in place. For this project `dist/` holds a wheel, and
the payload root (with `bin/netwatch-dash` and a bundled CPython) does not exist
until something assembles it. Assembling it from the build step is not available
either: the build step's commands are genproj's, and they are `pip install`,
`python -m build`, `ruff`, `pytest` — with no hook to add a step of our own
(`buildkite-agent pipeline upload` can only *add* steps, never extend an existing
one, and the pipeline file itself is regenerated).

So the assembly moved into an app-owned script and both callers use it:

| Caller | Uses the payload root to |
| --- | --- |
| `scripts/smoke-launch.sh <wheel-dir>` | assemble, then **run** it (fail-closed gate) |
| `scripts/release-artifacts.sh <version>` | assemble, then **pack** it |

`scripts/smoke-launch.sh` was seeded by §7's change, and `scripts/` is app-owned,
so the rewrite survives regeneration. The gate is not weakened: it runs the same
assembly from the same wheel, so it executes the payload the tarball will contain
— differing in two `build-info.json` fields that cannot be known before the tag
exists (`docs/release-payload.md`, "Where the smoked payload and the released one
differ").

**The upstream shape of this:** `github-release.target` now says "one artifact,
platform-specific" but says nothing about whether the artifact is *built by the
build step* or merely *packed from* it. A payload that needs an assembly step
(a bundled interpreter, a vendored runtime, anything signed in a later step) has
no place to put it. Either the build step needs an app-owned hook — a
`scripts/build-payload.sh` the generated step calls if present, which is the same
seeded-once pattern as `release-artifacts.sh` — or the smoke gate's contract needs
to become "call the assembler, then run what it produced". Worth reporting;
unfixed for now, and cheap for us because both files are app-owned.

### Reported, and settled as: one assembler, two callers

Reported to genproj-dev and open as **PR #38**. The shape it landed on is neither
of the two alternatives sketched above, and the argument that excluded both is
worth keeping:

- **Not a build-step hook.** `dist/` is where `python -m build` writes the wheel
  and what the build step uploads and the smoke gate downloads, so a payload root
  materialised into `dist/` collides with both. And there is no version at build
  time — the release step is what creates the tag — so a build-time assembler
  would write a `build-info.json` that misreports its own release.
- **Not smoke-gate-only.** The release step would then need its own assembler, and
  there are two of them again — the exact drift this gap is about.

So: **one assembler, called by both steps that need a payload**, version passed
positionally because the two callers legitimately pass different ones (a smoke
label; the release tag):

```
bash scripts/build-payload.sh <version> <output-root> [<input-dir>]
```

Both callers pass the **same** output root (`payload/`), which is the property
that makes the gate meaningful. Seeded (app-owned, so it survives regen) only for
the singular non-rust unit — a rust matrix links its payload directly, so a hook
there would be noise — with a default body that copies the build output into the
root, so a project whose `dist/` already *is* a payload root keeps its old
behaviour without noticing.

Note for adoption here: our `scripts/build-payload.sh` is already
`<version> <payload-root> [<wheel-dir>]`, which matches the seeded contract
exactly, so taking #38 costs us the duplicate assembly call inside
`release-artifacts.sh` (the generated step will have done it already) and nothing
else. It will also start writing `payload/` into the checkout, so `payload/`
joins `dist/` and `release/` in `.gitignore` (trap 5).

**Adopted.** PR #38 needed a rebase onto `3b31389` first — it was branched before
#37 merged and both edited `capability-template-utils.js` in adjacent regions, so
the two had to land as one coherent `renderBuildStep` / `renderSmokeStep`. The
conflict was one block: #37's rewritten `isDarwinTarget` doc against this PR's
insertion of `RELEASE_PAYLOAD_ROOT`, resolved by keeping both. All 712 tests pass
after the rebase, `npm run build:templates` produces a byte-identical
`templates.generated.js` (so the new template was wired correctly by hand), and
it merged as `d3c49af`, deployed by genproj build 135.

The regen at `298c5d1` then emitted, for this project:

```yaml
# smoke step
bash scripts/build-payload.sh "${SMOKE_VERSION:-0.0.0-smoke}" payload dist
bash scripts/smoke-launch.sh "payload"
# release step
bash scripts/build-payload.sh "$VERSION" payload dist
bash scripts/release-artifacts.sh "$VERSION"
```

One assembler, two callers, one root — which is exactly the property §8 said was
missing. Our `scripts/build-payload.sh` was not overwritten (app-owned) and did
not need to change, because it was already `<version> <root> [<wheel-dir>]`.
What did change is `release-artifacts.sh`: it now reuses an assembled
`payload/bin/netwatch-dash` when one is present and only assembles for itself
when it is not, so CI assembles once instead of twice while the script stays
runnable by hand. The double assembly would have been harmless — the same bytes,
twice — but "harmless waste on a release path" is how a release path becomes slow
enough that someone starts skipping it.

## 9. Third gap: the darwin rule is applied to a step that does not build a Mach-O

Found on the first real run after regenerating (Buildkite builds 12 and 13,
2026-09-20). §7's fix makes the build step **native** for a darwin target, and
for this project that step cannot run at all:

```
$ python -m pip install --no-cache-dir -e ".[dev]"
error: externally-managed-environment

× This environment is externally managed
╰─> To install Python packages system-wide, try brew install xyz ...
```

The agent is macOS, so its `python3` is Homebrew's (3.14), and Homebrew marks
itself externally managed — PEP 668 makes pip refuse to install into it. The
generated commands (`pip install -e ".[dev]"`, then bare `ruff`/`pytest`) assume a
container, where writing into the image's site-packages is the normal thing to
do. §7 removed the container without changing the commands.

Underneath that symptom is the real mistake: **the step does not need to be native
at all.**

| Step | Output | Needs a Mac? |
| --- | --- | --- |
| `build` (this project) | `netwatch_dash-0.1.0-py3-none-any.whl` | **No** — it is architecture-*independent* |
| `release` | the packed tarball, assembled by `build-payload.sh` | No — cross-install, and the gates make it safe |
| `smoke_<target>` | nothing; it *runs* the payload | **Yes** — a Mach-O cannot run in a Linux container |

The darwin → "no docker plugin, macOS queue" rule is exactly right for a **rust**
build step, because `cargo build --target aarch64-apple-darwin` links a Mach-O
binary and only a Mac has the SDK and linker. For a non-rust project with a
singular darwin target it fires on a step that produces a portable wheel, and buys
nothing — while costing a working build.

So `isDarwinTarget(unit.target)` is being consulted one step too early. The
distinction it needs is not "is the target darwin" but "**does this step produce a
platform binary**": true for a rust build unit, false for a python/node/java one.
For non-rust, the darwin-ness lives in the *payload* (assembled by the app's own
script, in the release step) and in the smoke gate, which is correctly native.

**Worked around app-side, temporarily.** `.buildkite/pipeline.yml` is
genproj-owned, so the build step's container was restored by hand, marked
`TEMPORARY … DELETE THIS PLUGIN BLOCK when the fix lands`, and the smoke gate was
left native. This is a workaround of exactly the kind §7 was meant to retire, so
it is recorded here rather than done quietly: **a regen reverts it**, and until
the upstream fix lands, a regenerated pipeline puts this project back to a build
that cannot install itself.

Reported to genproj-dev with the build logs. Suggested fix: gate the
native/no-container decision on the build unit actually producing a platform
binary (rust), not on the target label alone — and, for a language whose
generated commands must write into a Python environment, either keep those steps
containerised or create a virtualenv before installing.

### Resolved upstream (2026-09-20)

Fixed in `nickbrett1/genproj` **PR #37**, squashed to `3b31389`, and the suggested
shape above is the one that landed — with one refinement worth recording, because
it is the general form:

- `releaseBuildUnits` now stamps each unit with `platformBinary`: `true` for the
  rust matrix (`cargo build --target` links a per-target binary), and
  `language === "rust"` for the single unit. Written as a property of the
  language rather than a bare `false`, so it stays truthful if the validation
  guard that keeps `target` non-rust ever changes.
- `renderBuildStep` decides `nativeDarwin = unit.platformBinary &&
  isDarwinTarget(unit.target)` — "does **this step** produce a platform binary",
  not "which host may run the artifact".
- The **smoke gate keeps the target-based predicate**, deliberately. The two
  questions are genuinely different and the asymmetry is the fix: the build step
  must be able to *install itself*, the smoke gate must be able to *run the
  payload*. A regen that ever "corrects" the smoke gate to match the build step
  would have broken it.

Verified end to end rather than assumed. Merging is not the same as deploying:
`generate_project` is served by the deployed Cloudflare Worker, so the fix is
only live once the Worker's own pipeline (Buildkite `genproj` #133) has finished.
After that deploy, the regen at `b9a2a58` produced the correct pipeline on the
first attempt, and the hand patch below was deleted.

**The workaround is retired.** `.buildkite/pipeline.yml` is genproj-owned again in
the sense that matters: the container on the build step is now the generator's
own output, not a hand-edit, so a regen no longer reverts it. The `TEMPORARY …
DELETE THIS PLUGIN BLOCK` that this section used to describe has been removed,
and trap 4 of §7 is retired with it.

## 10. Fourth gap: a generation triggers two builds, and the release step races

Found immediately after the `b9a2a58` regen, on the same generation (Buildkite
builds 16 and 17, 2026-09-20). One generation produced **two builds on one
commit**, both running the release step:

| Build | Commit | Message | Source | State |
| --- | --- | --- | --- | --- |
| 16 | `b9a2a58` | `Initial commit: Generated project with 12 capabilities` | webhook | passed (created `v0.1.11`) |
| 17 | `b9a2a58` | `First build (genproj)` | api | **failed** |

Both resolved `LATEST=v0.1.10` → `TAG=v0.1.11`, both passed the generated
existing-tag guard, and both pushed the tag. Build 16 won:

```
! [remote rejected] v0.1.11 -> v0.1.11
    (cannot lock ref 'refs/tags/v0.1.11': reference already exists)
```

The guard is **check-then-act** —

```bash
if git ls-remote --exit-code --tags origin "refs/tags/$TAG" >/dev/null 2>&1; then
  echo "$TAG already exists on origin - nothing to release."
  exit 0
fi
```

— so it only protects a build that starts *after* the winner has finished. Its
comment ("Retried or re-run builds must not fail on a tag that already exists")
names the sequential case and misses the concurrent one. A check cannot be a
lock; the remote is the only thing that can arbitrate this atomically.

Two independent causes, and they are worth keeping separate:

1. **A generation triggers two builds** — the push fires the webhook *and* the
   generator starts a build through the API. That is a duplicate by
   construction, so this is not a race we can lose only under bad luck; the
   effect is deterministic on every generation that releases.
2. **The release step is not safe to run concurrently**, whatever the trigger
   does.

Neither is a defect in anything this repo controls: the release step's commands
are genproj's, and the double trigger is the generator's. Reported as
[`nickbrett1/genproj` issue #39](https://github.com/nickbrett1/genproj/issues/39).

**What it does and does not cost us.** The release is *correct* — build 16
published `v0.1.11` with the payload and manifest, and the loser fails before it
packs anything. The cost is a red build on every generation, which is worse than
it sounds: a pipeline that is red for a reason everyone learns to ignore is a
pipeline whose real reds stop being read. Until it is fixed, a red `release` job
whose log ends in `cannot lock ref` is **expected**, not an incident — and the
check is that the *other* build on the same commit passed.
