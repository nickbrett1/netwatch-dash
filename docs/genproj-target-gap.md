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
   see §7.)
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

### Three traps to know before regenerating

1. **A regen does not refresh `scripts/release-artifacts.sh`.** Under
   `src/generator/genproj-overwrite.js`, `scripts/` is app-owned and a diverged
   app file is *never* replaced - only `cloud_login.sh` and the
   wrangler/doppler helpers are genproj-owned scripts. Our copy (still the old
   placeholder) will therefore keep packing `...-any.tar.gz` after a regen. To
   take the generator's new wording, delete the file first so it re-seeds, or
   resolve that one path to `overwrite` in the regen call.
2. **A regen *does* overwrite `pyproject.toml`** (infra). Our
   `[tool.ruff] extend-exclude = ["producers"]` is not generator-owned and would
   be lost - re-apply it after the regen, or upstream the exclusion.
3. **Do not put the triple on the *current* payload.** `dist/` today is the
   wheel + sdist from `python -m build` - pure-python, i.e. genuinely
   architecture-*independent*. Labelling that `aarch64-apple-darwin` would be
   the mirror-image lie. The triple is honest only once `dist/` bundles the
   arm64 CPython (the launcher-shaped tree in the §10.2 rewrite). Land the
   declaration and the bundled payload together, never the label alone.
