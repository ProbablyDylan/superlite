# Config

In `skills/swarm/config/`:

- `models.json`: prices (USD per million tokens) and output speed per model. Edit it when they change.
- `predictor.json`: the time and context coefficients and the size band `predict` uses. Refit with `superlite-cli predict --refit`.
- `gitignore.json`: ignore entries per language plus common ones; Wave 0 appends the missing ones to `.gitignore` before any agent runs.
- `testprobe.json`: the hung-test probe's thresholds (see [Tools](tools.md)).
- `routing.json`: task features to tier (`haiku` or `sonnet`), tier agents and limits, and the stats gate. A critical-path task never gets haiku. `batch`: `dispatch` picks how many implementers run and which ready tasks each holds with an exact split (`split.choose`, see `specs/2026-10-25-implementer-batch-objective.md`), within each tier's `max_context_k`. A tier's `max_tasks` is an agent-reuse threshold based on completed tasks, not a batch-size limit. `critical_path_solo` (true) keeps critical-path tasks alone, so what they unblock never waits behind a batch. `superlite-cli dispatch --batch 1` forces one task per agent. The older `capacity` and complexity-weight keys are unused.
- `hosts/<host>.json` (e.g. `hosts/cursor.json`): per-IDE job → model slug candidates. `superlite-cli host_models resolve` writes `.superlite/host-models.json` once per run. Plans may set `**Execution:** host=cursor, parallel=multitask, swarm_by=tier` so `dispatch` prints a `SWARM` block; see `skills/using-superlite/references/cursor-tools.md`.

A committed `superlite.json` at the root of the project being built overlays those defaults. A missing file changes nothing. `version` must be `1`. Keys: `cap` (dispatch cap; an explicit `--cap` wins, including 0; otherwise 10), `gitignore` (lines appended after the shipped list and the plan's `**Ignore:**`; shipped lines are never removed), `hosts` (host name → job name → `candidates`, replacing that job's list), `testprobe` (threshold overrides; an explicit path or `SUPERLITE_TESTPROBE_CONFIG` wins), `models` (numeric price fields on existing model names, tagged `source=project`). Unknown keys, including `skills`, exit 2 and name the file. The file does not name skills and has no `check` field. It does not replace `routing.json`. A critical-path task still never uses haiku.

`superlite-cli plan_tools stubs` still writes Python stubs that `raise NotImplementedError` when the language is Python. Any other language prints one `stub:` line per Produces signature and a `contract:` line, and writes no `.py` file. `superlite-cli plan_tools adapter` still writes `.superlite/probes/adapter.py` and checks it with `compile` for Python. Another language gets `.superlite/probes/seam.ts`, `seam.js`, `seam.go`, `seam.rs`, `seam.cs`, `seam.java`, `seam.rb`, or `seam.txt`, and the first line is `// superlite seam: <language>`. `probe_run` exits 2 for a missing `adapter.py` only when the language is Python or `.superlite/test-cmd.json` is absent.

## Guard mode

How much the hooks hold the main session back. `superlite-cli guard set light|strict` writes `guard` in `preferences.json` in the superlite home (`$SUPERLITE_HOME`, else `$XDG_DATA_HOME/superlite`, else `~/.local/share/superlite`); `superlite-cli guard status` prints the mode in force. `SUPERLITE_GUARD` wins over the file. Anything but `light` is `strict`.

| | strict (default) | light |
|---|---|---|
| Coordinator writes project code during a run | denied: dispatch it | allowed |
| A plan waits for the human's approve | every agent that can write is denied | only `superlite:*`, implementer and fixer agents are denied |
| `superlite-cli dispatch`, `assign`, `wave0`, `checkpoint`, `finish`, `build_prime` before approval | denied | denied |
| superpowers during a run | suspended | left alone |
| `.superlite/orchestrator.json`, `feature.json`, `human-replies.jsonl` | never written by the model | same |
| Batch, question and tier checks on implementers and fixers | on | on |
| Killing processes by name or pattern (`pkill`, `killall`, `taskkill /IM`) | denied: kill by PID | allowed; only `kill -9 -1` is denied |
| `superlite-cli session ensure` | cuts a session branch, or a worktree beside an open run | works in place on the current branch |
| Shipping | merge, pull request or keep the session branch | commit here or leave uncommitted |
| Session start in a project with no `.superlite/` | the full superlite bootstrap (start menu, sizing, orchestrator question) | one line: superlite is installed but not in use here |

The feature flow's prototype still builds in its own throwaway worktree in both modes.

In both modes the hooks look for a run at or above the working directory but never above a checkout root (a folder with `.git`), the same rule as the scripts, so a run in one repo never governs another.
