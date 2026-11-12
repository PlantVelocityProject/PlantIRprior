# Operations and recovery

Routine background commands and IDs are in [Local tasks](local-tasks.md). This reference covers selection, direct owner tools, evidence interpretation and recovery. Commands below do not grant authorization for maintenance.

```bash
IR_PROJECT=/home/Zhuxiangrong/workspace/PlantIRagent
```

## Select scope and budget

| Selection | Meaning |
| --- | --- |
| species | Selected reference groups; use all five codes only when requested |
| project | Discovery filter only; never a computation boundary |
| accession | Discovery and per-sample computation filter; aggregation still uses the complete group |
| max-samples | Attempts per execution across selected species, including cleanup recovery |
| discovery-limit | Run enrichment limit; not a remote candidate count or computation budget |

For project-only computation, first obtain its accession set from original-archive metadata or a verified catalog. Pass every selected accession; stop if the set is empty or unverified. Never omit an empty accession selector and thereby expand scope.

API resume reuses stored settings. For a standalone historical cycle, read `cycles.request` and reproduce the original selectors, budget, references and scientific configuration with its actual `--batch-id`; the ID alone does not reconstruct arguments. Omitting it can select a new cycle if the catalog changed. Never create another cycle to bypass a cumulative budget or identity conflict.

## Direct previews and reviewed discovery

These are independent examples. Offline update preview and online discovery preview have different side effects:

```bash
# Offline metadata plan; no remote discovery or scientific state writes
bash "$IR_PROJECT/scripts/run_update.sh" --species ath --dry-run --json

# Online discovery; saves a review batch without applying the catalog
bash "$IR_PROJECT/scripts/run_discovery.sh" --species ath --batch-id ath-review-001 --dry-run --json

# Apply that actual reviewed batch with its original selectors
bash "$IR_PROJECT/scripts/run_discovery.sh" --species ath --batch-id ath-review-001 --offline --apply --json

# Plan pending publication for one reference group; does not publish
bash "$IR_PROJECT/scripts/run_aggregation.sh" --species ath --dry-run --json
```

Discovery uses `--source`; unified update uses `--discovery-source`. Both follow original-archive routing. Formal launchers can create runtime caches, so a preview still needs launcher-directory access.

## Evidence and status

| Source, relative to the project | What it establishes |
| --- | --- |
| Task receipt and process record under `state/agent/tasks/` | Execution identity, activity, return code and trusted result |
| `state/updates.sqlite3` cycles | Original request, frozen input and historical cycle checkpoints |
| `state/updates/<batch_id>/samples.snapshot.tsv` | Complete frozen catalog for the selected groups |
| `logs/update/<batch_id>/` | Sample-stage and aggregation diagnostics |
| `storage/evidence/<species>/<reference>/<annotation>/` | Candidate TSVs, run reports, registration and admission |
| `storage/aggregates/<species>/<reference>/<annotation>/aggregate.sqlite3` | Applied contributions and published membership; not a file inventory or readiness verdict |
| `releases/<species>/<reference>/<annotation>/current.json` | Current formal scientific release |
| `plantvelo/data/priors.json`, `website/data/releases/index.js` | Resource versions and committed website index |

A file count is an inventory, not a readiness count. A catalog-minus-TSV difference only estimates missing-file candidates. Existing files can be incompatible, damaged or awaiting recovery; aggregate membership can lag registration or retain active members absent from the current catalog.

Shared checks distinguish valid contents, compatibility with the current request and admission of the whole target group. Unregistered new samples may enter the normal budgeted pipeline; a missing registered source is invalid, not permission to redownload. Native evidence binds reference, protocol, five parameters and software identity; accept only existing exact audited software migrations. Historical evidence uses formal admission and keeps unknown execution parameters. Pending publication is recovered before interpreting a replacement; any remaining publishing journal blocks group aggregation.

For ordinary progress, use trusted receipts and per-group plans. An aggregation dry-run reports the registration-based plan, not a complete row-by-row source audit. Include the query time; old receipts describe their own invocation. Query SQLite with `mode=ro`, without creating databases or initializing UpdateState. A historical failure.json does not override a later successful receipt.

Only when the user requests complete evidence verification:

```bash
bash "$IR_PROJECT/scripts/run_update.sh" --species ath --dry-run --verify-evidence --json
```

This check bypasses success caches, can be slow on large tables, and writes no application state. It is not a routine preview, backlog counter or repair command. Cancellation is not verification success.

## Recovery decisions

| Observation | Action |
| --- | --- |
| Active or unknown execution | Query the original task; do not start concurrently |
| Retryable scientific interruption | Confirm stop, unchanged scope and sufficient authorized budget, then resume the original task |
| Science succeeded, delivery failed | Independent `deliver`; preserve the original partial receipt |
| Pending evidence publication/cleanup | Follow existing journals; do not infer completion from an intact old TSV |
| Evidence hash, reference, parameter or admission conflict | Report the specific conflict; preserve artifacts for explicit maintenance |
| Completed cycle | Return its frozen receipt; no new discovery, deferred processing or automatic delivery |

The backend already retries. Within existing authorization and remaining budget, allow at most one extra recovery attempt for the same cause per interaction; if it persists, report it. Do not delete state or locks. Lock-file existence is not proof of a held lock.

Transport backend, methods, timeout and retries may change without changing a frozen scientific request. For an incomplete ena-https stage, continue that backend rather than switching transports over partial files; it does not support CRR. See [Container network](container-network.md) for connectivity diagnosis. Do not change scientific settings to repair transport.

## Delivery recovery

Prefer `deliver --source-task` from Local tasks. It reuses the frozen job and selected resources/website targets, without discovery, calculation, aggregation or a new release. A website target is a global snapshot from the plan's complete version vector, not a promise to alter only one species.

For explicit owner maintenance, the same coordinator accepts an existing job:

```bash
bash "$IR_PROJECT/scripts/run_delivery.sh" --job-id ACTUAL_JOB_ID --target website --json
```

Replace ACTUAL_JOB_ID with the verified receipt value. If the original plan is missing or superseded, explicitly select the current formal release with species; do not silently retarget or roll back. Existing low-level resource/site tools remain maintenance options, not the routine recovery path. Scientific configuration changes alone do not require recomputing an already published delivery source.

## Stop only the requested execution

Stopping requires a user request and the original execution identity; a status query is not a stop request.

- API task: query its task-id, obtain the actual unit from its receipt, then `systemctl --user stop "$IR_UNIT"`. Confirm the unit stopped and re-query the original task.
- Direct command or full verification: use its original tool/process handle. Otherwise verify PID, command line and launch identity, then send SIGTERM only to that execution; confirm exit. Do not use a broad name-based kill or stop unrelated work.
- Preserve partial downloads, evidence, logs and locks. Record the actual stopped/interrupted/cancelled outcome and exit code when available; do not assume a fixed code or edit stale cycle rows.
- Do not restart cancelled verification, resume the task or launch a replacement without a subsequent instruction authorizing that action.

## Interpret counts and outcomes

`summary` describes one invocation, not the lifetime of a task:

| Current field | Meaning / legacy alias |
| --- | --- |
| attempted | Entered a stage chain, including cleanup; processed_samples |
| ready_before | Valid evidence with no recovery/cleanup pending; ready_samples |
| succeeded / failed / interrupted | Outcomes of this invocation's attempts |
| deferred | Deferred by the invocation budget; deferred_samples |
| unvisited | Not processed, including evidence blocked before an attempt |

Unknown historical values stay null. `result_replayed=true` counts belong to the original invocation, not new processing. Do not infer per-species counts from global totals or treat total_attempt_budget as actual work.

For waiting, report group new_samples/threshold. After publication use pending_new_samples; if absent, report unknown or query the same group rather than assuming zero. Complete can contain deferred work; published is scientific release success, not resource/site delivery success. Preserve valid scientific output after a later failure.

## Explicit maintenance only

Use the project docs for evidence rebuilding, initial baselines, forced publication, replacements, withdrawals and catalog edits. Never automatically relabel legacy evidence, relax admission, change thresholds or remove samples. Raw-signal submissions require original-archive file-type evidence; a slow/failed download alone is insufficient. Removing a catalog row does not withdraw its existing contribution, and discovery may re-add it.

Project references: `docs/update-workflow.md`, `docs/task-result-contract.md`, `docs/delivery-recovery.md`, `docs/evidence-admission.md`, `docs/sample-discovery.md`, `docs/aggregation-tool.md`, `docs/intermediate-cleanup.md`.

Publication policy: 50 new valid samples is an advisory checkpoint. Ordinary updates never publish or deliver; explicit `start --publish-only` performs publication and delivery with zero sample attempts. Pending scientific publication also requires an explicit publication request.
