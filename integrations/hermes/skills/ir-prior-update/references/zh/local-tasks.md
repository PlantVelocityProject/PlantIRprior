# 本地任务

使用本机现有客户端。身份与科学边界见中文主说明，不另造接口或套接字。

```bash
IR_CLIENT=/home/Zhuxiangrong/workspace/PlantIRagent/scripts/agent_update.py
/usr/bin/python3 -B "$IR_CLIENT" ping
/usr/bin/python3 -B "$IR_CLIENT" plan --species ath
/usr/bin/python3 -B "$IR_CLIENT" start --species ath --max-samples 1 --request-id ath-example-001
/usr/bin/python3 -B "$IR_CLIENT" status --task-id ath-example-001 --tail 2048
/usr/bin/python3 -B "$IR_CLIENT" resume --task-id ath-example-001 --request-id ath-example-001-retry1
```

上面是独立示例，不是依次执行的脚本。start/resume 需要已有授权范围和预算；提交前替换示例编号。task-id 等于初始 request-id，科学 batch-id 则标识更新周期。

## 找到原任务

只有科学 batch-id 时，先从 `/home/Zhuxiangrong/workspace/PlantIRagent/state/agent/tasks/records/` 的可信记录取得任务 id，再查询 status。batch-id 与 task-id 不可混用；不猜编号，也不启动工作来取得编号。按任务类型读取范围与 attempts：交付任务参数与科学任务不同。

## 幂等和限制

每次新 start、deliver 或 resume 先选定并记录稳定 request-id。回复丢失时查询状态或以相同编号、相同参数重提；换编号可能产生新工作。每次 resume 有自己的编号，重复该编号不再次执行。

科学任务支持重复 species/accession，以及 skip-discovery、discovery-source、download-backend、download-timeout/retries、discovery-limit 和 max-samples。API 每次允许 1–10 次样本尝试，下载超时默认 900 秒，每个任务最多四次获授权执行。不接受任意路径、科学设置、project、历史周期接管或强制发布；精确参数范围看 --help。

科学 resume 再授予原执行预算。total_attempt_budget 是授权上界，不是实际尝试数；无法遵守用户累计上限时停止恢复并说明。成功完成的任务只回放冻结结果；继续处理延后样本需要另一次获授权的新更新。

## 只补交付

ping capabilities 应包含 delivery-v1。有来源任务时复用其固定计划：

```bash
/usr/bin/python3 -B "$IR_CLIENT" deliver --source-task ath-example-001 --target website --request-id ath-delivery-001
/usr/bin/python3 -B "$IR_CLIENT" status --task-id ath-delivery-001
/usr/bin/python3 -B "$IR_CLIENT" resume --task-id ath-delivery-001 --request-id ath-delivery-001-retry1
```

资源使用 --target resources，两个目标可重复传入。source-task 和 species 互斥。旧任务无计划或计划过期时，明确选择当前正式发布：

```bash
/usr/bin/python3 -B "$IR_CLIENT" deliver --species ath --target website --request-id ath-current-delivery-001
```

补交任务样本预算为 0，最多四次获授权执行；不改原科学回执，也不静默追新版本。DELIVERY_ONLY_REPAIR_REQUIRED 表示使用补交，不再授予科学尝试。

## 按顺序读结果

1. ok：接口调用是否成功。查询成功仍可返回失败任务。
2. execution.state/outcome：queued/running/unknown/stopped 与终态；phase 是兼容视图。
3. result.kind：update_result 含 summary/groups，science_outcome 与外层交付失败分开判断；delivery_result 含 job_id 和目标结果，没有科学 summary。
4. result.delivery/errors：分别解释资源、网站和错误。result_replayed=true 的统计属于原执行，不计为当前新增工作；历史未知值保持 null，retryable 不授予新执行。

任务通常可跨 Hermes 退出或 API 重启继续；服务器重启会终止计算，之后查询原任务再按授权恢复。unknown 阻止新工作。API 没有 stop；只有用户要求停止时才按[操作参考](operations.md#仅停止指定执行)核对原执行并终止。

定时面向用户汇报时读[进度简报](progress-reports.md)；status 本身不配置通知。

完整定义见项目 docs/local-task-interface.md、docs/task-result-contract.md 和 docs/delivery-recovery.md。
