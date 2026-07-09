# Self-updater status (observed, not assumed)

`updater.py` is the simpler self-updater merged before the main-branch
reconciliation (the older delta/rollback system on the dropped branch was
intentionally not carried forward and is out of scope here). This file records
what was **observed** by actually running the code, not what the file appears
to do.

## What works (verified)

**Update check — reaches GitHub, reports state accurately.**
Ran `updater.check_for_update()` live (read-only, safe). Result:
```
current: 1.1.0   latest: v1.1.0   update_available: false   error: ""
```
It hit the GitHub releases API, compared semver correctly, and reported
"up to date". Offline/rate-limit paths return a state dict with `error` set
rather than raising (confirmed by reading `_latest_release` / `_get_json`,
which swallow failures) — launch is never blocked.

**Apply — full-zipball path installs and bumps VERSION.**
`tests/test_updater.py::test_full_update_applies_and_preserves_config` runs
`apply_update()` in a sandboxed tmp repo (`monkeypatch` of `updater.ROOT` and
`UPDATES_DIR`, which are module globals resolved at call time; network stubbed).
Observed: staged `app.py`/`pipeline.py` replaced the old ones, `VERSION` bumped
to 2.0.0, and the success message returned.

**Config preservation.**
Same test: a locally-modified `config.yaml` was **not** overwritten; the
incoming version landed as `config.yaml.new` for manual review. `PRESERVE_ALWAYS`
(output/, cache/, samples/, inbox/, .env, jobs.db, config.yaml, tools/, .venv,
.git) is honored via `_is_updatable`.

**Verify-before-apply rejects a broken update.**
`test_broken_staged_update_is_rejected`: a staged `.py` that does not compile is
caught by `_verify_staged` (py_compile) **before** any file is touched — the
working tree is left completely unchanged (app.py and VERSION intact).

**Rollback on mid-apply failure.**
`test_midapply_failure_rolls_back`: forced an `OSError` while copying the second
file into the repo (after the first was already overwritten). Observed: the
`except` path logged "rolling back", `_rollback` restored the overwritten file
from the backup, `VERSION` never advanced, and a `RuntimeError` containing
"rolled back" was raised. Backups live in `cache/updates/backup_<version>/`.

## Limitations / not tested against a live destructive update

- The **delta path** (`_changed_files` / `_stage_delta` via the GitHub compare
  API) and the **full network download** (`_download_resumable`) were **not**
  exercised end to end against real GitHub, because that needs two published
  release tags and would perform real network I/O. The tests force the full
  path and stub the download; the delta code is covered only by reading, not by
  execution. No real destructive `apply_update` was run against this working
  repo (correctly — the safe way to prove apply/rollback is the sandbox above).
- **No dry-run flag** exists. A `--dry-run`/no-op mode would let a user preview
  an update without applying; the sandbox test covers the same ground for CI,
  so this is a nice-to-have, not a gap that blocks safety.
- The running Python process keeps its old code in memory after an update; the
  UI correctly tells the user to restart. Not automated (by design).

## Bottom line
Check, verify, apply, config-preservation, and rollback all behave correctly
under observation. The untested surface is the live network delta/download path,
which is guarded by integrity checks (blob-sha / zip CRC) and a full-zipball
fallback in code but was not run against real GitHub here.
