# netwatch-dash — the genproj generation invocation

genproj stores **no** generation config in the repo it produces. A regeneration
is only reproducible if the inputs are written down somewhere, so this is that
record. It is reconstructed from three in-repo sources of truth, not assumed:

- the git history (`Initial commit: Generated project with 12 capabilities`),
- the generated `README.md` **Capabilities** section (emitted from the resolved
  capability list), and
- the live catalog (`list_genproj_capabilities`, which gives the dependency
  graph that explains the resolved set).

## 1. Capabilities

The resolved set is **12** (what the README lists). Eleven are *selected*;
`docker` is pulled in as a dependency:

```
selected (11):
  editor-tools  shell-tools  devcontainer-python  code-quality-python
  buildkite  github-release  fetch-launch  dependabot
  doppler  coding-agents  container-agent

resolved dependency (1):
  docker            <- devcontainer-python (and devcontainer-* generally) requires it
```

Dependency edges that matter (from the catalog): `github-release` → `buildkite`,
`doppler`; `fetch-launch` → `github-release`; `coding-agents` → `doppler`;
`container-agent` → `doppler`, `coding-agents`; `code-quality-python` →
`devcontainer-python`; `devcontainer-python` → `docker`.

> The GitHub repo description lists only 9 capabilities. That string was written
> at the **first** generation and is not updated by a later one — it is stale,
> and the README's 12 is the current truth.

## 2. Configuration

```jsonc
{
  "name": "netwatch-dash",
  "repositoryUrl": "https://github.com/nickbrett1/netwatch-dash",
  "selectedCapabilities": [
    "editor-tools", "shell-tools", "devcontainer-python", "code-quality-python",
    "buildkite", "github-release", "fetch-launch", "dependabot",
    "doppler", "coding-agents", "container-agent"
  ],
  "configuration": {
    "language": "python",
    "buildkite": { "queue": "mac-studio-linux", "provisionPipeline": true, "branchGating": true },
    "github-release": { "target": "aarch64-apple-darwin" },
    "fetch-launch": {},
    "dependabot": { "updateSchedule": "weekly" },
    "docker-container": { "pythonDependencies": ["fastapi>=0.115.0", "uvicorn>=0.30.0"] },
    "doppler": {},
    "coding-agents": {},
    "container-agent": {}
  }
}
```

Notes on the non-obvious keys:

- **`github-release.target` = `aarch64-apple-darwin`** — the declared label for
  a single, platform-specific payload (the payload bundles an arm64 CPython, so
  the universal key `any` would be a lie). This is the upstream fix; see
  `docs/genproj-target-gap.md` §7. It is **mutually exclusive** with
  `github-release.targets` (the plural Rust-only build matrix) and is **not**
  creatable for a rust project.
- **`docker-container.pythonDependencies` without selecting `docker-container`**
  — deliberate, and the only supported route to runtime deps:
  `generatePyProjectToml` reads that key directly out of the configuration and
  is **not** gated on the capability being selected. It is why the generated
  `pyproject.toml` carries `fastapi`/`uvicorn`. Selecting `docker-container`
  itself would be wrong: it conflicts with `fetch-launch`.
- **`language: "python"`** — declared explicitly rather than inferred (only one
  devcontainer is selected, so it would be implied; declaring it makes CI,
  release paths and sonar agree with no order-dependence).
- **`buildkite.queue: mac-studio-linux`** — the default, and the queue the
  self-hosted arm64 agent listens on. (Its name describes the *containers* the
  queue's steps run in, not its hosts — those are native macOS.)
- **`dependabot`** — `updateSchedule: weekly`; `groupUpdates` is left at its
  default (`true`), which is why the generated config carries the
  `minor-and-patch` groups.
- **`doppler`, `coding-agents`, `container-agent`** — defaults (`doppler`
  shares the `common` project rather than creating a per-repo one).

## 3. Regenerating — what a regen does and does not touch

Per `src/generator/genproj-overwrite.js`:

| Class | Paths | On regen |
| --- | --- | --- |
| **App-owned** | `src/`, `tests/`, `scripts/`, `worker/`, `app/`, `lib/`, `main.py`, `config.py` | a diverged file is **never** replaced. Only four scripts are genproj-owned (`cloud_login.sh`, `run-wrangler-dev.sh`, `setup-wrangler-config.sh`, `sync-doppler-secrets.sh`). |
| **Merge target** | `.devcontainer/devcontainer.json` | merged (capability contributions + manual edits) |
| **Infra** | `README.md`, `RELEASING.md`, `LAUNCHING.md`, `pyproject.toml`, `.buildkite/*`, `.devcontainer/*`, `.github/*`, `.agents/*`, `doppler.yaml`, `deploy/README.md`, `.vscode/*`, `.gitignore` | **overwritten** with fresh template content |
| **Not emitted** | `producers/`, `schema/`, `docs/`, `deploy/install-host.sh` | untouched |

Two consequences for **this** repo:

1. **`scripts/release-artifacts.sh` is app-owned → a regen will NOT refresh it.**
   Declaring `github-release.target` changes what a *new* project is *seeded*
   with — not a file already on disk. This is not hypothetical: it is why the
   declared triple was landed by rewriting the script by hand rather than by
   regenerating (`docs/release-payload.md`). To take the generator's wording,
   delete the file before regenerating (so it re-seeds) or pass
   `resolutions: { "scripts/release-artifacts.sh": "overwrite" }` — but note the
   seeded script packs a plain `dist/` tarball, so taking it means giving up the
   launcher-shaped payload again.

   Two more app-owned scripts now matter for the same reason, and a regen leaves
   all three alone:

   | Script | Origin | Why we diverge |
   | --- | --- | --- |
   | `scripts/release-artifacts.sh` | seeded, rewritten by hand | packs the launcher-shaped payload under the declared triple, and reuses the pipeline-assembled root rather than assembling a second one |
   | `scripts/build-payload.sh` | ours entirely | the assembly the seeded copy cannot do (a bundled CPython). Unchanged by §8: ours was already `<version> <root> [<wheel-dir>]`, which is the contract genproj standardised on, so the hook found a script that already matched |
   | `scripts/smoke-launch.sh` | seeded by the §7 change, rewritten | runs an already-assembled root as-is, and assembles from a wheel directory when handed one — so it works under both the pre-§8 pipeline (`smoke-launch.sh dist`) and the current one (`build-payload.sh … → smoke-launch.sh payload`) |

   A regen will therefore *seed* `smoke-launch.sh` for a project that has none,
   and leave ours exactly as it is.
2. **`pyproject.toml` is infra → a regen overwrites it.** This loses
   `[tool.ruff] extend-exclude = ["producers"]` (added so the verbatim host
   producer copies are never reformatted). Re-apply it after any regeneration, or
   upstream the exclusion.

The full list of things a regen would silently undo — six of them, including
`pyproject.toml` (which loses the ruff exclusion), `.gitignore` (which does not
ignore `dist/` or `release/`) and `RELEASING.md` — is in
`docs/genproj-target-gap.md` §7. `.buildkite/pipeline.yml` is the exception: that
trap **retired on the 2026-09-20 regen** (`b9a2a58`), which came back with the
containerised build step, the native smoke gate and the release step's
dependencies, all emitted by the generator rather than hand-patched
(`docs/genproj-target-gap.md` §9). The file is still overwritten — it is still
infra — but what it is overwritten *with* is now correct, so verifying it is a
check rather than a repair.

Also note: declaring `target` (singular) does **not** change
`.buildkite/pipeline.yml`. Only the plural `targets` creates a build matrix;
`releaseBuildUnits` still emits one build step for a Python project, uploading
`dist/**`.
