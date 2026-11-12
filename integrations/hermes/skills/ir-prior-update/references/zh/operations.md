# 操作与恢复

后台命令与编号见[本地任务](local-tasks.md)。本参考只展开范围、维护、证据和恢复；命令示例不额外授予维护权限。

```bash
IR_PROJECT=/home/Zhuxiangrong/workspace/PlantIRagent
```

## 范围与预算

| 选择 | 含义 |
| --- | --- |
| species | 所选参考组；仅在用户要求全物种时使用五个代码 |
| project | 只筛选检索，不能作为计算边界 |
| accession | 筛选检索与逐样本计算；聚合仍检查完整组 |
| max-samples | 所有所选物种合计的单次尝试预算，含清理恢复 |
| discovery-limit | run 补全预算，不是远端候选总量或计算预算 |

只计算某项目时，从原库元数据或已验证目录取得 accession 集合，逐个传入；为空或未验证则停止，不能省略空筛选扩大范围。

API resume 自动使用已存设置。恢复独立历史周期须读取 cycles.request，使用实际 batch-id 重现原筛选、预算、参考和科学配置；编号本身不重建参数。省略编号且目录变化时可能创建新周期。不得新建周期来绕过累计预算或身份冲突。

## 独立预览与已审核检索

以下是独立示例，离线更新预览与在线检索预览的副作用不同：

```bash
# 离线元数据计划：不联网，不写科学状态
bash "$IR_PROJECT/scripts/run_update.sh" --species ath --dry-run --json

# 在线检索：保存审核批次，不修改主目录
bash "$IR_PROJECT/scripts/run_discovery.sh" --species ath --batch-id ath-review-001 --dry-run --json

# 用原筛选离线应用实际审核过的批次
bash "$IR_PROJECT/scripts/run_discovery.sh" --species ath --batch-id ath-review-001 --offline --apply --json

# 查询某参考组的发布计划，不发布
bash "$IR_PROJECT/scripts/run_aggregation.sh" --species ath --dry-run --json
```

检索用 --source，统一更新用 --discovery-source，均按原库路由。启动器可能创建运行缓存，预览仍需要对应目录访问权限。

## 证据与状态

| 项目内相对路径 | 可说明什么 |
| --- | --- |
| state/agent/tasks/ 下的回执与进程记录 | 执行身份、活动性、退出码和可信结果 |
| state/updates.sqlite3 的 cycles | 原请求、冻结输入和历史周期检查点 |
| state/updates/<batch_id>/samples.snapshot.tsv | 所选组的完整冻结目录 |
| logs/update/<batch_id>/ | 样本阶段和聚合诊断 |
| storage/evidence/<species>/<reference>/<annotation>/ | 候选 TSV、运行报告、登记与准入 |
| storage/aggregates/<species>/<reference>/<annotation>/aggregate.sqlite3 | 已应用贡献与发布成员，不是文件清单或就绪结论 |
| releases/<species>/<reference>/<annotation>/current.json | 当前正式科学发布 |
| plantvelo/data/priors.json、website/data/releases/index.js | 资源版本与已提交网站索引 |

文件数是清单数，不是就绪数。目录减 TSV 只能估计缺文件候选；已有文件可能不兼容、损坏或待恢复。聚合成员也可能落后于登记，或保留已从当前目录移除的活动样本。

共享检查区分内容有效、当前请求兼容和整组准入。未登记新样本可按预算进入正常流程，已登记源丢失则无效，不能据此自动重下载。原生证据绑定参考、协议、五个参数与软件身份，只接受已有精确审计的软件迁移；历史证据依赖正式准入，未知运行参数仍保持未知。先恢复已冻结的发布再解释替换结果；任何 publishing 日志未完成时，该组不得聚合。

普通进度使用可信回执和分组计划。聚合 dry-run 只是基于登记的计划，不是源内容完整逐行审计。注明查询时间，旧回执只代表原执行。SQLite 使用 mode=ro，不创建库或初始化 UpdateState；旧 failure.json 不覆盖后续成功回执。

只有用户要求完整证据核验时才用：

```bash
bash "$IR_PROJECT/scripts/run_update.sh" --species ath --dry-run --verify-evidence --json
```

它绕过成功缓存，大表可能耗时，不写应用状态；不是日常预览、积压计数器或修复命令。取消不等于核验通过。

## 恢复选择

| 观察结果 | 动作 |
| --- | --- |
| 执行 active 或 unknown | 查询原任务，不并行启动 |
| 可恢复的科学中断 | 确认停止、范围不变且预算仍获授权，再恢复原任务 |
| 科学成功而交付失败 | 独立 deliver，保留原 partial 回执 |
| 发布或清理待完成 | 按现有日志恢复，不能因旧 TSV 完整就判完成 |
| 哈希、参考、参数或准入冲突 | 说明具体冲突，保留现场等待明确维护 |
| 周期已完成 | 回放冻结结果，不检索、不处理延后样本、不自动补交 |

后端已有重试。在既有授权和剩余预算内，每次交互最多额外恢复一次同原因故障；仍失败则报告，不删除状态或锁。锁文件存在不等于锁仍被持有。

下载后端、方法、超时和重试数可在不改变冻结科学请求的前提下调整。ena-https 暂存未完成时沿用该后端，不换方式接管半成品；它不支持 CRR。连通问题见[容器网络](../container-network.md)，不改科学设置来修传输。

## 仅补交付

优先使用本地任务说明中的 deliver --source-task，复用固定 job 和选定的 resources/website 目标，不检索、计算、聚合或新增科学发布。网站目标是计划中的全局版本快照，不代表只修改一个物种的网站内容。

明确维护可直接使用已有 job：

```bash
bash "$IR_PROJECT/scripts/run_delivery.sh" --job-id ACTUAL_JOB_ID --target website --json
```

ACTUAL_JOB_ID 须替换为真实回执值。计划缺失或过期时，明确选择物种的当前正式发布，不静默追新或回退。低层资源/网站工具保留为维护选择，不作为日常恢复主路径；仅科学配置变化不要求重算已发布交付源。

## 仅停止指定执行

停止需要用户要求和原执行身份；查询进度不是停止授权。

- API 任务：按 task-id 查询真实 unit，再执行 `systemctl --user stop "$IR_UNIT"`。确认 unit 停止，并重新查询原任务。
- 直接命令或完整核验：优先用原工具/进程句柄；否则核对 PID、命令行与启动身份，仅向该执行发 SIGTERM 并确认退出。不按名称批量杀进程，不停止其他工作。
- 保留半成品、证据、日志和锁，按实际回执记录 stopped/interrupted/cancelled 与退出码；不猜固定代码，也不修改遗留周期行。
- 没有后续授权，不重启已取消核验、不恢复任务、不另起替代执行。

## 统计与结果

summary 只描述一次调用：

| 字段 | 含义与兼容别名 |
| --- | --- |
| attempted | 进入阶段链，含清理恢复；processed_samples |
| ready_before | 已有有效证据且没有恢复/清理待办；ready_samples |
| succeeded/failed/interrupted | 本次尝试的结果 |
| deferred | 因本次预算延后；deferred_samples |
| unvisited | 未处理，包含尝试前因证据问题被阻止的样本 |

历史未知值保持 null；result_replayed=true 的统计属于原执行。不要把全局数推成逐物种数，也不要把 total_attempt_budget 当作实际工作量。

waiting 报 new_samples/threshold；发布后用 pending_new_samples，缺失则说明未知或查询同组，不默认为零。complete 可有延后工作，published 只代表科学发布成功，不代表资源/网站成功。后续失败不回滚有效科学输出。

## 仅限明确维护

证据重建、初始基线、强制发布、替换/撤回和目录编辑按项目文档执行，不自动把 legacy 重新贴标、放宽准入、改阈值或删样本。原始信号提交需有原库文件类型证据；下载慢或失败不足以判定。删除目录行不撤回已有贡献，检索也可能再次加入该样本。

项目文档：docs/update-workflow.md、docs/task-result-contract.md、docs/delivery-recovery.md、docs/evidence-admission.md、docs/sample-discovery.md、docs/aggregation-tool.md、docs/intermediate-cleanup.md。

Publication policy: 50 new valid samples is an advisory checkpoint. Ordinary updates never publish or deliver; explicit `start --publish-only` performs publication and delivery with zero sample attempts. Pending scientific publication also requires an explicit publication request.
