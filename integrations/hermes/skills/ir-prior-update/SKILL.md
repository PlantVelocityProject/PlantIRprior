---
name: ir-prior-update
description: Use when a user requests PlantIRagent IR prior previews or updates, long-read sample discovery, task progress, interruption recovery, or repair of evidence, aggregation, publication, and resource or website delivery failures.
metadata:
  hermes:
    tags: [plantiragent, ir-prior, bioinformatics, automation]
---

# IR Prior Updates

Use the existing task client, scientific entry points and receipts. Do not replace the workflow with model-generated processing scripts. [中文说明](references/zh/skill.md).

Project: `/home/Zhuxiangrong/workspace/PlantIRagent`. Species: `ath` Arabidopsis, `gmx` soybean, `osa` rice, `sly` tomato, `zma` maize.
Hermes and the project share the local `Zhuxiangrong` UID. Verify `id -un`; for task-client calls also verify the `ping` owner. Use the local terminal, without SSH or account switching.

## Choose the operation

For client commands, IDs and result fields, read [Local tasks](references/local-tasks.md). For the selected maintenance or recovery mode, read only the relevant section of [Operations](references/operations.md).

| Request | Entry point | Boundary |
| --- | --- | --- |
| Offline preview | `agent_update.py plan` | Current catalog only; no remote discovery or scientific execution |
| Online discovery preview | `run_discovery.sh --dry-run` | Network access and a saved review batch; no catalog application or reads download |
| Background evidence update | `agent_update.py start` | Discovery and evidence accumulation; no publication or delivery |
| Explicit publication | `agent_update.py start --publish-only` | Only after the user explicitly requests publication: aggregate existing evidence, validate, publish and deliver; zero sample attempts |
| Progress | `agent_update.py status` | Query the original task using its recorded task-id, not batch-id; never launch work to check status |
| Scientific recovery | `agent_update.py resume` | Original task and settings; another authorized execution budget |
| Resource/website repair | `agent_update.py deliver` | Frozen published versions; no scientific computation or sample budget |
| Stop an execution | [Stop instructions](references/operations.md#stop-only-the-requested-execution) | Verified original unit/process only; confirm exit and do not restart |

Direct owner scripts cover standalone discovery, historical cycles and explicit maintenance. All `run_*.sh` commands use the pinned SIF; do not replace the image or bypass it with host scientific tools. Actual commands and absolute paths are in the references.

## Scope and execution rules

- Determine species and selectors from the request or verified metadata. `--project` filters discovery only. Project-only computation requires a **nonempty verified accession set** passed as `--accession`; an empty set must not fall back to all samples.
- Keep updates serial. The default budget is 10 sample attempts per execution across the selected species; resume grants it again. Respect any cumulative user limit using actual prior attempts. API budgets are 1–10; direct `--max-samples 0` is unlimited. Discovery's default 100-run enrichment budget is separate.
- Record the request/task ID, scientific batch ID, actual scope, budget and receipt. Lost replies and conversation timeouts do not prove termination. Query the original task; never create a fresh request ID to test whether work started. Unknown activity blocks concurrent work.
- Use the bounded recovery rules in Operations. Delivery-only failure requires `deliver`, not scientific resume. Completed-cycle replay stays frozen; it does not process deferred samples or refresh delivery.
- A registered filename or hash alone is not evidence readiness. Shared checks cover contents, the current scientific request and group admission. Preserve historical admission and unknown parameters. Incompatible, damaged or unfinished evidence needs an explained recovery/maintenance action, not implicit rebuilding.

## Scientific boundaries

Retrieve metadata from its original archive: SRR/NCBI, ERR/ENA, DRR/DDBJ, CRR/NGDC. Keep unreported fields unknown and ambiguous protocols pending. Verified FASTQ mirrors are allowed; use actual CRR RemotePaths rather than constructing URLs.

Publication is **manual only**. **50 new valid samples per species/reference/annotation group is a review checkpoint**, never an automatic publication trigger or a minimum for explicit publication. Count canonical accessions relative to the latest successful release; retries and recalculation are not new samples. Ordinary updates accumulate evidence and report waiting/checkpoint_reached without aggregating contributions, recovering pending releases, or delivering resources and the website. Require an explicit user publication instruction before `start --publish-only`; it skips discovery/download/alignment/extraction, attempts zero samples, and publishes the complete frozen compatible group. Do not combine publication with accession filters. Pending scientific releases require explicit publication recovery; existing published versions may use explicitly requested delivery repair.

Ordinary updates do not change scientific settings, initialize the five existing databases, force publication, withdraw samples, or manually delete FASTQ/BAM, caches, releases or failure artifacts. These are separate maintenance operations. Runtime caches may be written even by a scientific preview.

Full `--verify-evidence` is an explicit, potentially slow check, not a routine status query. Respect a user's cancellation; record the check as cancelled, never as passed, and do not restart it.

## Report from receipts

For an authorized long-running task, report every **15 minutes**, even without visible change; immediately report completion, a new verified failure or a need for user action. Keep each message to **3–4 plain-language lines**. Summarize overall progress; name a species only for a relevant problem. Waiting for manual publication, including a reached checkpoint, is not a failure. Report a reached checkpoint as a request to review the accumulated evidence; do not publish from that report.

Use verified current activity and receipts; omit unknown counts, percentages and finish estimates. Keep versions, hashes, internal IDs, paths and raw errors in detailed records, available when requested. Reporting never starts verification, computation or recovery. Send one final message and stop periodic reporting when the task ends or is cancelled.

Before arranging periodic reports, read [Progress reports](references/progress-reports.md) for trigger requirements, examples and result interpretation. Skill instructions alone are not a timer: if reliable scheduling or a verified destination is unavailable, disclose that limitation rather than claim reporting is active.

## Diagnose only as needed

- Access or launch failure: [Permission checks](references/permission-audit.md). Preserve owner-only access; do not widen permissions or delete locks.
- Download TLS/timeouts or host/container connectivity differences: [Container network](references/container-network.md). Verify the failing route before changing transport settings.
- Detailed scientific or result semantics: follow the project-document links in Operations. Do not dump full tables, reads, credentials or process environments into the conversation.
