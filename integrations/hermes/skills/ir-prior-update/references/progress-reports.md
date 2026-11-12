# Progress reports

## Timing and delivery

For an authorized long-running update, use a reliable available background trigger to send a brief report every **15 minutes**, including when nothing visibly changed. A new verified failure, task completion or required user decision is reported immediately; count that message as the latest report to avoid a duplicate periodic message.

Use the originating conversation or another destination explicitly selected by the user. Verify the task/destination binding; do not choose an unrelated chat, account or group. If the destination or persistent scheduling capability cannot be established, say periodic reporting is unavailable. The task API and this skill do not themselves schedule or send messages. Use only capabilities verified in the current environment; do not invent scheduling tools or deploy a new scheduler as part of a routine update.

Keep only the monitor state needed to avoid duplicates: original task and latest attempt, verified destination, last successful report time/event, and whether the final report was sent. Reuse an existing binding. Do not replay missed messages after reconnecting; check the original task and send one current update. Reading or editing this skill does not create a monitor.

## Read facts without starting work

Use the original task's status and small, current checkpoints. Consult a bounded log tail only for missing context. Resolve task IDs through [Local tasks](local-tasks.md); never poll by starting or resuming an update, discovery, aggregation or full verification.

- A live process proves activity, not forward progress. Report an unchanged observation neutrally; silence in a long stage alone is not a failure.
- Use confirmed counts from the same invocation; attempts are not successes. Do not add replayed receipts or infer readiness from file counts. Omit unavailable counts, percentages and finish estimates.
- Distinguish scientific processing, publication and resource/site delivery. Check actual outcomes rather than equating a successful status query with task success.
- Sum only compatible, nonoverlapping progress counts for the overall message. Review checkpoints remain independent for each reference group; never pool samples across species to claim a release.

Detailed count and receipt meanings remain in [Operations](operations.md#interpret-counts-and-outcomes); do not duplicate them in every message.

## Message format

Use **3–4 short lines** in the user's language: current overall work, confirmed progress or lack of new information, and next step or needed user action. A short local query time may be included. Normal reports do not list each species. Name a species only when a verified problem affects it, explaining the impact and whether the rest can continue only when known.

Keep software/release versions, hashes, task/batch IDs, internal paths and raw stack traces in detailed records. Provide them when requested or needed for a concrete recovery action. Preserve task IDs, evidence and log references internally even when omitted from the brief.

Examples below are illustrative, not live results; use each claim only when confirmed.

**Normal progress**

> 更新进行中，当前主要处理数据比对。  
> 本轮已完成6个样本，正在继续处理后续样本。  
> 目前无需你操作。  

**No confirmed change**

> 最近确认任务处于数据下载阶段。  
> 最近15分钟没有可确认的新进展。  
> 尚未收到完成或失败结果。  

**Species-specific problem**

> 大豆有1个样本下载失败，已完成结果保留。  
> 任务仍在处理其他样本。  
> 暂不需要你操作。  

**Finished**

> 本轮处理已结束，完成8个样本。  
> 新生成的先验已完成资源和网站交付。  
> 本轮定时汇报已结束。  

If no release was generated, say so instead of using the delivery claim. An up_to_date delivery may refer to an existing release, not new work in this invocation. Waiting below a publication threshold is normal, not an alert. A completed invocation may still have deferred work; do not say all samples are finished unless verified. Ask for user action only when it is actually required.

## Ending reports

Once the original execution is confirmed stopped/completed or cancelled, send one outcome-appropriate final message, stop its reporting trigger and retain the last report marker. Do not automatically launch another cycle for deferred work. An explicitly authorized resume may reactivate reporting for its new attempt; a frozen replay must not repeat an old completion notification.

If only reporting is cancelled, stop notifications without stopping the scientific task. If the user cancels the task or full verification, follow the verified [stop procedure](operations.md#stop-only-the-requested-execution), then stop its reports. Never restart cancelled work or label cancellation as success. Unknown execution state is not completion: report uncertainty and observe the same task without restarting it.
