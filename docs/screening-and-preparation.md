# 固定靶标筛选与准备资源准入

本轮把固定靶标 × 候选的输入构造、独立链缓存和准备资源准入连在一起。部署仍按用户决定搁置；本文说明已经实现的管线机制与可复现测试，不代表现场 AF3 数据库或 GPU 已运行。

## 先确定每个预测目标包含哪些链

固定靶标可以包含多条链。它与候选 FASTA 的每条记录分别组成一个完整目标，候选之间不会合并成一个超大复合物。

| 输入 | 目标 1 | 目标 2 | 目标 N |
|---|---|---|---|
| 一条固定链 T + N 条候选 | T + B₁ | T + B₂ | T + Bₙ |
| 两条固定链 T₁/T₂ + N 条候选 | T₁ + T₂ + B₁ | T₁ + T₂ + B₂ | T₁ + T₂ + Bₙ |

固定链按 FASTA 出现顺序编号 A、B…，每条候选使用下一个 ID。例如两条固定链的每个目标都包含 A/B/C 三条链。固定链最多 25 条，完整目标最多 26 条；总残基限制由服务端 `PROTEIN_STUDIO_MAX_RESIDUES` 决定，默认 5000。

同序列的两个候选仍是两项筛选记录；原始位置与标题构成稳定 targetId。它们可以命中准备缓存，但不会因此丢掉候选身份。当前模式将每条候选视为一条链；候选本身是重链/轻链等多链组合时，使用多链 JSONL 明确描述各完整目标。

AF3 官方支持复用固定链的原始 MSA/模板，并为不同组合重建输入。独立链搜索数由精确序列及准备配置决定，所有完整组合仍分别做特征化和推理。[AF3 v3.0.4 组合复用方案](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/docs/performance.md#pre-computing-and-reusing-msa-and-templates)、[本项目链缓存实现](chain-reuse-design.md)

## 浏览器操作

1. 打开“批次管线”，输入构造选择“固定靶标筛选”。
2. 在固定靶标框中填写一条或多条 FASTA 链，选择候选 FASTA 文件。
3. 查看前两项组合预览：固定链数、候选链 ID、完整目标的残基数和服务上限。预览不会扫描完整文件来猜总任务数。
4. 选择已经配置好的预测引擎、数据库和执行位置，开始流式导入。默认每 100 项上传一次。
5. 从目标列表检查链准备来源，从资源面板检查准备是否正在等待预算，从混合云页检查后续常驻节点和传输。

导入期间固定靶标、输入模式和文件选择被冻结，执行使用提交时的快照。中断后，选择同一文件并继续未封存批次。浏览器本地保存固定链规范内容的 SHA256 和组装策略，刷新后仍会核对；记录不保存固定链序列。更换靶标或改输入模式会在上传前拒绝，避免将不同筛选混入同一批次。存储失败时停止上传，不假装已经建立续传保护。

浏览器上下文只用于本浏览器的输入构造核对，不是跨客户端的服务器端 campaign 策略。换浏览器、清除本地存储后，固定靶标模式需要恢复原上下文或新建批次。服务端仍按目标 ID 和规范输入 hash 做幂等与冲突校验。CLI 则使用下一节的磁盘 manifest 绑定完整输入文件。

## CLI：固定靶标加候选

先只验证输入，不访问服务、不加载权重、不创建清单：

```bash
python3 scripts/submit_campaign.py \
  --input candidates.fasta --fixed-target target.fasta \
  --engine alphafold3 --database-profile af3-protein-local \
  --seeds 1 --samples 5 --recycles 10 --dry-run
```

dry-run 流式扫描并精确返回组合数、固定链数、总链数、总残基数和最多两个目标摘要。数量不是 GPU 性能测量。固定链与每条候选相加超过限制时，直接报告该输入错误。

已配置好引擎的环境可用同一路径提交：

```bash
python3 scripts/submit_campaign.py \
  --server localhost --input candidates.fasta --fixed-target target.fasta \
  --engine alphafold3 --database-profile af3-protein-local \
  --placement hybrid --seeds 1 --samples 5 --recycles 10 \
  --chunk-size 100 --manifest screening.accepted.jsonl
```

`--start-streaming` 可在导入期间开始后续处理；默认先导入、封存再执行。`--leave-paused` 用于只入库并封存，暂不开始执行。恢复时保留原文件、固定靶标及 manifest：

```bash
python3 scripts/submit_campaign.py \
  --server localhost --input candidates.fasta --fixed-target target.fasta \
  --campaign-id 原批次32位ID --manifest screening.accepted.jsonl
```

CLI 清单绑定候选文件 SHA256、固定靶标文件 SHA256、规范固定链 SHA256、输入格式、名称前缀和组装策略。固定靶标改变时，在发送任何修改请求前拒绝续传。旧版清单只能沿用原来的独立 FASTA 输入，不将缺失的固定链身份补猜出来。解析内存只持有固定链、当前记录和最多 100 项待提交块，不随候选总数增长。

## CLI：候选本身是多链组合

多链 JSONL 每行描述一个完整目标。以下仅为格式示例：

```jsonl
{"targetId":"candidate-001","name":"target + heavy + light 001","chains":[{"id":"A","sequence":"ACDE"},{"id":"B","sequence":"FGHI"},{"id":"C","sequence":"KLMN"}]}
{"targetId":"candidate-002","name":"target + heavy + light 002","chains":[{"id":"A","sequence":"ACDE"},{"id":"B","sequence":"PQRS"},{"id":"C","sequence":"TVWY"}],"settings":{"seeds":[1,7],"numSamples":5,"numRecycles":10}}
```

```bash
python3 scripts/submit_campaign.py \
  --input complexes.jsonl --format jsonl \
  --engine alphafold3 --database-profile af3-protein-local --dry-run
```

JSONL 保留受支持的逐目标采样设置，后续节点按兼容签名领取。`--fixed-target` 只与候选 FASTA 一起使用；JSONL 已明确描述完整目标，不再隐式叠加固定链。自定义 MSA、模板、修饰和化学字段不能经此普通蛋白入口静默丢弃。

## 准备并发为什么需要第二道准入

就绪水位控制“准备了多少目标”，CPU/内存预算控制“允许多少搜索同时执行”。这两者不能相互代替。AF3 v3.0.4 对四个遗传库并行搜索；若每个 Jackhmmer 使用 8 个线程，对应约 4×8=32 个搜索线程的配置。深 MSA 的搜索也可能需要超过官方建议 64 GB 的内存。实际配额应依据已配置的搜索参数及代表性输入测量，不按任务总数直接增加准备 worker。[AF3 数据管线资源说明](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/docs/performance.md#data-pipeline)

管理员可在启动控制器前设置 `PROTEIN_STUDIO_PREPARATION_RESOURCES_JSON`。字段均为本控制器的准备预算：

| 字段 | 单位和含义 |
|---|---|
| `cpuBudget` / `cpuPerPrepare` | 可供准备池使用的核配额 / 一次准备的估计核配额；成对配置 |
| `memoryBudgetBytes` / `memoryPerPrepareBytes` | 内存预算 / 一次准备的估计峰值；字节，成对配置 |
| `diskMinFreeBytes` | 准备目录需要保留的可用空间底线 |
| `diskPerPrepareBytes` | 一次准备的保守增量空间预留 |

[示例 JSON](../config/preparation-resources.example.json) 假设准备池分配 64 核、每次准备预留 32 核、磁盘保留 50 GiB、每次预留 8 GiB。这是展示配置单位的算术示例，不是本机资源或真实任务开销测量；内存字段未声明，面板显示未知。内存估计应另行按真实深度/长度分布测量后加入，不默认填成零。

```bash
export PROTEIN_STUDIO_PREPARATION_RESOURCES_JSON="$(cat config/preparation-resources.example.json)"
./start.sh
```

预算在启动时校验；例如一次准备的 CPU 估计超过整个 CPU 预算，会直接报错，而不是让任务永远等待。未配置预算时保留原来的线程数与水位控制，面板明确显示未声明。

## 已实现的调度语义

准备线程先以 FIFO 顺序取得资源预留，再到 SQLite 领取目标。预算不足时尚未领取，因此不会增加目标 attempt、启动 MSA 搜索或占 GPU。空队列、准备完成、异常、取消和停止都释放预留；释放或关闭立即通知等待者，磁盘状况按周期复查。原有在线节点资格、就绪高水位与目标租约继续生效。

若已有 k 个在途预留，每次磁盘预留为 R、保留底线为 M，下一次准备要求：

```text
min(缓存目录可用字节, attempt目录可用字节) - k×R >= M + R
```

这是一种保守准入：已经实际写入的字节可能还在预留中。磁盘探测失败时等待并显示原因。它不会删除缓存或结果，也没有为每个进程设置磁盘配额。CPU 和内存同样是管理员估计的调度预留，不是实测利用率或 OS 硬限制；其他程序仍可消耗同一机器资源。即使某目标最后命中缓存，当前也按整个准备阶段预留，优先保持保守的一致性。

接口 `GET /api/pipeline/config` 返回 `preparationResources` 的预算、每次估计、在途预留、等待线程数、阻塞原因和磁盘观察时间；`maxResiduesPerTarget` 提供输入上限。等待线程数不能当作候选任务数。浏览器面板分别呈现这些口径，未声明的 CPU/内存不画成 0% 利用率。

测试记录将输入解析、实际 HTTP 入库、合成 CPU 准备、资源传感器替身和真实模型执行分别说明。真实 AF3 特征等价、数据库搜索和 GPU/云吞吐仍需在后续现场环境验收。
