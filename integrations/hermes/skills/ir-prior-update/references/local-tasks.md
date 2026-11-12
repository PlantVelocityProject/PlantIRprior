# Local tasks

Use the deployed client on the same server. Identity checks and scientific boundaries are defined in SKILL.md; do not invent another socket or interface.

```bash
IR_CLIENT=/home/Zhuxiangrong/workspace/PlantIRagent/scripts/agent_update.py
/usr/bin/python3 -B "$IR_CLIENT" ping
/usr/bin/python3 -B "$IR_CLIENT" plan --species ath
/usr/bin/python3 -B "$IR_CLIENT" start --species ath --max-samples 1 --request-id ath-example-001
/usr/bin/python3 -B "$IR_CLIENT" status --task-id ath-example-001 --tail 2048
/usr/bin/python3 -B "$IR_CLIENT" resume --task-id ath-example-001 --request-id ath-example-001-retry1
```

These are separate examples, not a script to execute in sequence. Start and resume require an authorized scope and budget. Replace example IDs before submission; task-id equals the initial request-id, while scientific batch-id identifies the update cycle.

## Manual publication and the review checkpoint

Ordinary start accumulates evidence and never publishes or delivers. Fifty new valid canonical accessions per species/reference/annotation group is an advisory review checkpoint. Require an explicit user publication instruction before this command:

```bash
/usr/bin/python3 -B "$IR_CLIENT" start --species ath --publish-only --request-id ath-publication-001
```

Require `manual-publication-v1` in ping capabilities. Publication uses the complete frozen group of existing compatible evidence, skips discovery and all per-sample computation, and reports zero sample budget. It then aggregates, validates, publishes, and synchronizes resources and the website. `plan --publish-only` previews this mode. It rejects accession filters and never upgrades an ordinary evidence task through resume. Pending scientific publication is also manual; delivery-only repair remains a separate explicit action. Replace the example request-id before submission.

## Find the original task

If only a scientific batch-id is known, resolve the task's `id` from the trusted records under `/home/Zhuxiangrong/workspace/PlantIRagent/state/agent/tasks/records/` before calling status. A batch-id is not interchangeable with task-id; do not invent one or start work to obtain it. Read the record's kind, scope and attempts as applicable: delivery records have different parameters from scientific tasks.

## Idempotency and limits

Choose and record a stable request-id before each new start, delivery or resume request. On a lost reply, query status or repeat the same ID with identical parameters. Changing IDs may create new work. A resume has its own stable request-id; repeating it does not execute twice.

Scientific tasks accept repeated species/accession selectors, skip-discovery, discovery-source, download-backend, download-timeout/retries, discovery-limit and max-samples. The API allows 1–10 sample attempts per execution, defaults to a 900-second download timeout, and permits at most four authorized executions per task. It does not accept arbitrary paths, scientific settings, project selectors, historical cycle attachment or forced maintenance publication. Explicit `--publish-only` is the supported manual publication mode. Use `--help` for exact limits.

Each scientific resume grants the original execution budget again. `total_attempt_budget` is the authorized upper bound, not actual attempts. If a cumulative user limit cannot be respected, stop recovery and report the limitation. Successful completed tasks replay their frozen result; continuing deferred computation requires a separately authorized new update.

## Delivery without recomputation

Require `delivery-v1` in ping capabilities. Use the source task's fixed plan when available:

```bash
/usr/bin/python3 -B "$IR_CLIENT" deliver --source-task ath-example-001 --target website --request-id ath-delivery-001
/usr/bin/python3 -B "$IR_CLIENT" status --task-id ath-delivery-001
/usr/bin/python3 -B "$IR_CLIENT" resume --task-id ath-delivery-001 --request-id ath-delivery-001-retry1
```

For resources use `--target resources`; both targets may be repeated. Source-task and species are mutually exclusive. If an old task has no fixed plan, or the plan is superseded, obtain an explicit current-release selection:

```bash
/usr/bin/python3 -B "$IR_CLIENT" deliver --species ath --target website --request-id ath-current-delivery-001
```

Delivery tasks have zero sample budget and at most four authorized executions. They do not change the original scientific receipt or silently switch an old plan to current. `DELIVERY_ONLY_REPAIR_REQUIRED` means use delivery recovery, not another scientific attempt.

## Read the result in order

1. `ok`: whether the RPC succeeded. A successful status query may describe a failed task.
2. `execution.state/outcome`: whether execution is queued, running, unknown or stopped, and its terminal outcome. `phase` is a compatibility view.
3. `result.kind`: `update_result` has scientific `summary/groups`; inspect `science_outcome` separately from outer delivery failure. `delivery_result` has job_id and target results, without a scientific summary.
4. `result.delivery` and `errors`: resource and website outcomes are independent. `result_replayed=true` belongs to the original invocation; do not add those counts to the current attempt. Historical unknown fields remain null. `retryable` does not grant another attempt.

A task normally survives Hermes exiting or the API restarting, but server reboot stops computation. Query the original task after reboot; unknown activity blocks new work. The API has no stop operation: use the verified owner-side [stop procedure](operations.md#stop-only-the-requested-execution) only when requested.

For scheduled user-facing messages, read [Progress reports](progress-reports.md); status alone does not arrange notifications.

Full contracts: project `docs/local-task-interface.md`, `docs/task-result-contract.md` and `docs/delivery-recovery.md`.
