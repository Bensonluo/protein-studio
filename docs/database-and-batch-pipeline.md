# 数据库连接与十万条候选的分阶段预测管线

这套实现把“输入多少条”和“同时执行多少条”分开：输入清单持久化到 SQLite，CPU 准备池从清单领取任务并生成 MSA/模板准备产物，GPU 池只领取准备完成的任务，最后由独立 CPU 验证池检查与发布结构。默认直接运行公开结构回放；接入真实 AF3/ColabFold 需要管理员配置本地数据库与已安装模型环境。

本文核对日期为 **2026-10-23**。AF2 接口核对 v2.3.2，AF3 核对 v3.0.4；ColabFold README/setup 脚本按当日 main 与 1.6.3 接口核对。这些是文档依据版本，实际运行版本必须填入本地配置。

## 1. “连接序列库”具体连接什么

这里的数据库不是网页账号或 SQL 连接串。JackHMMER、HH-suite、MMseqs2 读取本地 FASTA、二进制数据库及索引；模板搜索还要读取 PDB 序列库和 mmCIF 文件。管理员将完整数据库挂载到搜索进程能看到的目录，注册路径、不可变版本及必要文件清单。浏览器只选择一个已注册的 profile ID，不能通过 HTTP 任意指定服务器文件路径。

本项目的文件分工：

| 文件 | 用途 |
|---|---|
| `config/databases.example.json` | 本地数据库、索引、版本与搜索参数示例 |
| `backend/databases.py` | 文件预检、准备缓存的数据库指纹、查询接口 |
| `config/staged-engines.example.json` | 已安装 AF3 / ColabFold 的 CPU 准备与 GPU 预测命令 |
| `scripts/staged_env.py` | 把选定执行配置输出为可检查的环境变量 |
| `docs/staged-runners.md` | 具体 runner 参数、环境启用与官方阶段语义 |
| `scripts/submit_campaign.py` | FASTA 流式导入、稳定目标编号与幂等重传 |

先复制数据库配置，保留实际使用的 profile，修改 `root`、`release`、`prepareVersion`、`searchVersion`、工具路径与资产清单。`release` 必须记录真实数据库发布版本及 PDB 快照；`prepareVersion` 记录模型数据管线与本项目 runner 的实际版本；`searchVersion` 记录已安装搜索工具版本。所有 `REPLACE` 占位都要替换，完成后移除 `example: true`。

```bash
cp config/databases.example.json config/databases.local.json
# 编辑 config/databases.local.json，指向已经安装的数据库。
export PROTEIN_STUDIO_DATABASE_CONFIG="$PWD/config/databases.local.json"
python3 -m backend.databases
```

网页“数据库连接”及 `GET /api/databases` 显示同一组预检结果。`reference` 不需要序列库。其他默认示例故意显示缺失；目录存在不等于已经配置完成。

一个可运行 profile 至少声明：

```json
{
  "schemaVersion": 1,
  "profiles": {
    "my-af3": {
      "engine": "alphafold3",
      "release": "实际安装的数据集版本与PDB快照",
      "root": "/srv/protein-databases/alphafold3",
      "prepareVersion": "实际AF3版本及runner提交号",
      "searchVersion": "实际HMMER版本",
      "assets": [
        {"name": "uniref90_database_path", "kind": "sequence", "type": "file", "path": "uniref90_2022_05.fa", "required": true}
      ],
      "searchParameters": {"jackhmmer_n_cpu": 8},
      "templateParameters": {"max_template_date": "2021-09-30"}
    }
  }
}
```

以上短例仅解释字段，**不是完整 AF3 库清单**；实际部署以仓库完整示例的九项资产为起点。预检验证管理员声明的清单，不会推测遗漏的业务依赖。

## 2. AF2、AF3、ColabFold 分别需要哪些文件

### AF3：序列库、模板库都挂到 CPU 准备侧

`af3-protein-local` 示例使用官方 v3.0.4 文件名：

| 配置资产名 / 官方参数 | root 下文件或目录 |
|---|---|
| `uniref90_database_path` | `uniref90_2022_05.fa` |
| `mgnify_database_path` | `mgy_clusters_2022_05.fa` |
| `small_bfd_database_path` | `bfd-first_non_consensus_sequences.fasta` |
| `uniprot_cluster_annot_database_path` | `uniprot_all_2021_04.fa` |
| `seqres_database_path` | `pdb_seqres_2022_09_28.fasta` |
| `pdb_database_path` | `mmcif_files/` |
| `ntrna_database_path` | `nt_rna_2023_02_23_clust_seq_id_90_cov_80_rep_seq.fasta` |
| `rfam_database_path` | `rfam_14_9_clust_seq_id_90_cov_80_rep_seq.fasta` |
| `rna_central_database_path` | `rnacentral_active_seq_id_90_cov_80_linclust.fasta` |

文件来源与下载脚本见 [AF3 官方安装文档](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/docs/installation.md) 和 [fetch_databases.sh](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/fetch_databases.sh)。管理员在模型仓库中使用官方 `./fetch_databases.sh /srv/protein-databases/alphafold3` 下载到指定位置；本应用不自动执行下载。

虽然当前产品输入是蛋白链，未修改的 v3.0.4 CLI 构建 `DataPipelineConfig` 时仍解析 RNA 数据库路径，所以这三个文件在本配置中也必须存在。不能用空文件绕过。`seqres_database_path` 是 AF3 参数名，不能照搬 AF2 的 `pdb_seqres_database_path`。[AF3 官方运行脚本](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/run_alphafold.py)

### AF2：full 与 reduced 数据配置要配套

官方数据包含 UniRef90、MGnify、BFD/Small BFD、UniRef30、PDB70、mmCIF；多聚体还使用 UniProt 与 PDB SeqRes。`scripts/download_all_data.sh <DOWNLOAD_DIR>` 是官方安装入口，管理员应记录具体数据快照。`full_dbs` 与 `reduced_dbs` 对数据库需求不同。[AF2 v2.3.2 官方安装与数据库说明](https://github.com/google-deepmind/alphafold/blob/v2.3.2/README.md)

本仓库 `af2-reduced-local` 是 reduced 清单示例，显式登记 Small BFD、UniRef90、MGnify、PDB70 的 HH-suite 文件前缀、mmCIF、obsolete.dat，以及多聚体所需 UniProt/SeqRes。`type: prefix` 必须列明 `requiredSuffixes`；存在一个同名前缀文件不能替代整套索引。启用 full_dbs 应再登记 BFD 与 UniRef30 所需实际文件族，并将直接执行命令的 preset 对齐。[官方 AF2 CLI 检查逻辑](https://github.com/google-deepmind/alphafold/blob/v2.3.2/run_alphafold.py)

当前 AF2 使用完整单任务 runner，**尚未接入本项目分阶段 campaign 适配器**。注册 AF2 库能做文件检查，不会自动把 AF2 变成已实现的拆分执行引擎。直接 AF2 命令配置见 `config/engines.example.json` 与部署文档。

### ColabFold：本地 MMseqs2 搜索，准备结果再送 GPU

官方大规模本地流程是：

```bash
# 在已安装的 ColabFold 环境执行；本应用不自动下载数据库。
MMSEQS_NO_INDEX=1 ./setup_databases.sh /srv/protein-databases/colabfold
colabfold_search --mmseqs /opt/colabfold/bin/mmseqs \
  candidates.fasta /srv/protein-databases/colabfold prepared_msas
colabfold_batch prepared_msas predictions
```

官方 README 推荐本地批量搜索先生成 MSA，再执行预测。CPU 批量搜索一般不需要预建 `.idx` 搜索索引；它和数据库必要的 `.index` / `.dbtype` 文件不是一回事。不能把 `MMSEQS_NO_INDEX=1` 理解成“不需要任何索引文件”。[ColabFold 官方批处理说明](https://github.com/sokrypton/ColabFold/blob/main/README.md)

本次核对的 setup 默认使用 `uniref30_2302` 与 `colabfold_envdb_202108`。配置登记相应 `_db`、`_seq`、`_aln` 文件族及 taxonomy/mapping，管理员必须按已安装脚本产生的文件清单核实。空的下载完成标记不当作数据库资产。开模板搜索时还要配置模板数据库和本地 PDB 目录，不应仅把 `use_templates` 改成 true。[官方 setup_databases.sh](https://github.com/sokrypton/ColabFold/blob/main/setup_databases.sh)

当前 staged helper 固定本地 CPU 搜索，不向公共 MSA 服务发送序列。`searchParameters.mmseqs` 使用实际工具绝对路径，避免“装在 Conda 环境中，但服务 PATH 找不到”的问题。默认直接单任务配置中的 `single_sequence` 没有 MSA；它不能替代这里的本地搜索流程。

## 3. 预检结果与数据库指纹怎样解释

普通文件检查是否存在、为普通文件、非空且能读取 1 字节；目录检查类别、可读性及至少一个条目，不递归遍历 mmCIF 全库；前缀检查每个明确列出的文件成员。缺少必需文件返回 `ready: false`，可选资产缺失给 warning。文件为 FIFO 等特殊类型也不当作可读数据库。

`profileFingerprint` 覆盖以下声明与元数据：模型/准备/搜索版本、数据库 release、搜索和模板参数、资产名、必需成员清单、绝对路径、文件大小与纳秒修改时间。它用于避免常见配置变更后误用准备缓存；不是 TB 级数据库的全内容校验。目录内文件原地修改可能不改变顶层目录元数据，因此实际运维使用不可变版本目录，升级时换 release/profile，保留旧批次快照。不要在运行中的路径内覆盖数据库后仍沿用同一个版本。

预检成功表明声明资产可读取，不代表模型权重授权、搜索二进制版本、数据库格式、GPU 环境和质量基线都已执行验证。正式运行先用一个小真实候选走完 CPU 准备→GPU 预测→验证，再释放大批次；实际命令与日志在各 attempt 目录保留。

搜索节点优先把热点数据库放本地 SSD/NVMe，完整库只读挂载，临时 MMseqs/HMMER 工作目录和准备缓存使用可写目录。容器内的 `root` 必须是容器挂载后的路径，不是宿主机未映射路径。将大型数据库放在 Docker build context 外，避免构建镜像时复制整个库。这些是部署选择；应用不会自行复制大型库或挂载远程盘。

## 4. 十万条输入在当前系统中怎样流动

```text
FASTA / 界面文件
  → 每 100 条写入 campaign_items（SQLite，仅持久化清单与状态）
  → waiting_input
  → CPU preparation 池：本地库搜索、模板准备、缓存命中检查
  → ready：带准备产物路径与校验信息
  → GPU prediction 池：申请一个配置的 GPU 组、调用模型 runner
  → CPU validation 池：结构/输入/产物检查
  → completed：发布结果指针，提供结构与原始产物下载
```

每条 FASTA 记录是一个独立单链候选。多条序列组成同一个复合物时，应使用每项包含多条 `chains` 的 campaign JSONL/API 输入，不能误把整个 FASTA 文件当成一个十万链复合物。

| 控制项 | 当前默认 | 作用 |
|---|---:|---|
| `PROTEIN_STUDIO_CPU_PREP_WORKERS` | 2 | 同时做搜索准备的目标数 |
| `PROTEIN_STUDIO_GPU_WORKERS` | `WORKERS`，通常 1 | legacy 本地预测并发门限；workers 模式由注册设备组领取执行 |
| `PROTEIN_STUDIO_CPU_VALIDATE_WORKERS` | 1 | 同时验证发布的目标数 |
| `PROTEIN_STUDIO_READY_HIGH_WATER` | 64 | ready 加在途 preparing 的准备背压门限 |
| `PROTEIN_STUDIO_PIPELINE_LEASE_SECONDS` | 60 | 本地任务租约与心跳周期依据 |
| `PROTEIN_STUDIO_GPU_GROUPS_JSON` | 未设 | legacy 进程的可见 GPU 组；resident 使用各主机 fleet 的实际设备 |
| `PROTEIN_STUDIO_PREDICTION_MODE` | `legacy` | `workers` 将 campaign 预测交给认证的常驻节点，保留 CPU 准备与验证 |

门限限制准备积压，不限制整批输入总条数；CPU 不会因为已经导入十万条就立刻启动十万个搜索进程。假如 GPU 消费速度落后，ready 达到门限后停止领取新的准备工作，消化后继续。CPU 准备 worker 数还要结合每次搜索线程数与数据库 IO 实测调整；增加 worker 不等于免费增加吞吐。

整目标缓存键覆盖链 ID/序列、引擎、准备版本、数据库指纹、搜索与模板参数以及准备命令；改变随机种子或采样数不会要求重新搜索相同输入。AF3 整目标未命中时，再解析独立链缓存，为未缓存的唯一序列运行准备。两层缓存均要求 manifest、文件集合、大小和 SHA256 验证通过，均有 single-flight 与原子发布。

模型预测阶段已经包含一次原生结构/置信度解析；独立验证池继续做输出序列、路径、结果完整性与发布校验，并非所有 PAE 解析都已经迁到独立 CPU 池。

任务执行使用租约和 attempt 路径。过期 worker 的结果不能覆盖新 attempt 已发布的指针；重试会保留自己的产物与诊断。暂停是停止领取后续工作，在途工作可能继续；取消和本地子进程处理按 runner 协议执行。workers 模式由 worker 协议和逐项租约处理远端生命周期；若另行使用 Slurm/SSH wrapper，仍需将取消传播到实际远端作业。

界面的“重试失败项”调用 `POST /api/campaigns/{id}/retry-failed`，保留清单封存状态、目标 ID 和已完成项。准备失败重新进入准备队列；预测或验证失败且准备描述仍安全可读时回到 ready，否则重新准备。旧 attempt 的结果发布权被撤销，缓存完整性在实际执行时再检查。这个动作由用户明确触发，不会对有问题的输入无限重试。

完整数据库与执行配置每批只保存一次，领取任务时注入运行参数；每个候选仅保存其输入与采样设置，避免十万条任务重复保存相同资产清单。

## 5. 用 CLI 导入及恢复批次

浏览器与CLI已补固定靶标×候选模式；CLI也支持多链JSONL。操作、输入身份与资源预算见[筛选与准备资源说明](screening-and-preparation.md)。

先验证输入格式，不访问网络、不创建 manifest：

```bash
python3 scripts/submit_campaign.py \
  --input candidates.fasta --engine alphafold3 \
  --database-profile af3-protein-local --seeds 1 --samples 5 --recycles 10 \
  --dry-run
```

管理员已启用 staged runner、预检 ready 后再提交：

```bash
python3 scripts/submit_campaign.py \
  --server localhost --input candidates.fasta --name campaign-20261023 \
  --engine alphafold3 --database-profile af3-protein-local \
  --execution-profile '{"id":"local-af3","version":"实际运行环境版本"}' \
  --seeds 1 --samples 5 --recycles 10 --chunk-size 100 \
  --manifest candidates.accepted.jsonl
```

默认先创建 paused 批次，分块追加，全部导入后 seal，再 resume。`--start-streaming` 在第一块入库后开始处理，后续边导入边执行；末尾仍封存。`--leave-paused` 导入并封存后保持暂停；它不能与流式执行同时启用。`--limit 100` 只导入前 100 条，用于小批验证。

重试时指定原 campaign ID、原始输入文件及原 manifest：

```bash
python3 scripts/submit_campaign.py \
  --server localhost --input candidates.fasta \
  --campaign-id 原批次的32位ID --manifest candidates.accepted.jsonl
```

执行参数省略时继承原批次设置；明确指定但与原批次冲突会报错。`--name-prefix` 如首次设置，恢复时也保持相同值。已有 manifest 不能在无 `--campaign-id` 的情况下再创建另一批，以免一次误操作重复整批。

UI 与 CLI 使用同一目标标识算法：`t-` 加 `SHA256(一基序号 + NUL + 完整原始标题)` 的前 24 位。原文件不变时，全部重传依靠服务端 `(campaign, targetId)` 唯一约束返回已有条目；同标识但序列或规范设置改变，会返回 409 并回滚该块。不同标题、不同位置的同序列候选不会被盲目合并。相同科学输入仍可复用准备缓存，但各候选的任务与结果索引独立保留。

manifest 记录源文件 SHA256、campaign ID、每个已确认块、实际 item ID 与序列 SHA。它是确认记录，不把所有候选装进内存，也不代替服务端账本。网络中断时重传同文件可解决“服务端已收、客户端未记”的不确定状态；不要先修改候选文件或重排记录。导入失败会尝试暂停批次并报告是否确认暂停，已成功的块保留；未完成导入不封存。

API 单次最多接受 200 项，本 CLI 控制在 100 项以内；请求体还有 12 MiB 限制。特别长的序列使请求超过上限时，降低 chunk-size。读取时只保留当前记录与当前块；源文件 SHA256 使用分块读取，不把整个文件读入内存。

## 6. 固定靶点链如何复用，哪些已经实现

例如固定靶点 A 与一批不同候选 B 组合：可以先准备 A 的单链搜索结果，再分别准备各 B，最终按模型要求装配复合物输入；这样无需对同一个 A 反复搜索。AF3 官方描述了复用 per-chain `unpairedMsa`、`pairedMsa` 与模板来构造组合输入的方式。[AF3 官方性能与数据管线说明](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/docs/performance.md)

链搜索缓存和复合物准备缓存是两层不同的数据。链层键至少包含标准化序列/分子类型、数据库不可变版本、搜索工具与参数、模板策略及截止日期；复合物层还要包含链顺序/拷贝数/ID、配对策略和组装规则。不能把单链 MSA 任意拼接成 paired MSA，也不能因为序列相同就忽略链身份及对应模板映射。

当前 AF3 自动准备路径已加入独立链缓存与复合物输入组装。链缓存保存原始 `unpairedMsa`、`pairedMsa` 与内联模板；只重映射目标链 ID，不手工拼接或重排行。完整组合仍由 native AF3 重新完成配对与特征化，随后进行每个目标自己的推理。实现、计数口径和测试见[链级复用专题](chain-reuse-design.md)。旧完整目标缓存继续可用，未记录链信息的旧条目不补造链命中统计。

## 7. 本地 16 卡与云端如何接入

当前有两条明确的执行路径。默认 `legacy` 保留现有 AF3/ColabFold staged helper：每个目标调用模型进程，CPU 搜索已拆离，模型初始化仍需重复。`PROTEIN_STUDIO_PREDICTION_MODE=workers` 则使用已实现的认证拉取协议，准备包经 SHA-256 校验传到实际节点，由 AF3 原生常驻进程连续执行兼容目标并逐项回传；reference 验证同一协议。ColabFold 尚未接入原生 resident，继续使用 legacy staged。

控制面仍为单个 SQLite 服务，多个 GPU worker 不共享或直接修改 SQLite。它们向控制面注册、心跳、领取目标，下载准备包并回传结果；完成项保留，失联未完成项经租约恢复，旧尝试不能覆盖新结果。这里已包含真实 HTTP worker 与文件传输，不依赖管理员重新实现这层协议；云实例创建、对象存储后端和控制面高可用仍由部署方案另行提供。

本地 16 卡使用 [fleet 配置](../config/fleet.local16.example.json) 与 [fleet launcher](../scripts/launch_worker_fleet.py)，原生 AF3 每个可见 GPU 一个 resident。多台主机分别维护真实设备列表，不通过一台主机的 CUDA 设备编号访问另一台。fleet 只对失败 slot 有界退避重启，健康 worker 保持运行；Linux 可选 [每 worker 的 systemd 服务](../deploy/foldspace-worker@.service)。不要对同一个 GPU 同时运行两套托管进程。

现有阿里云 ECS/ACK GPU 节点部署相同 agent，使用 TLS 与独立凭据拉取兼容工作。`placement=local|aliyun|hybrid` 控制 campaign 的允许位置；`maxResidues` 取管理员与 runner 上限的较小值，仅是实测后设定的分发资格，不是通用显存保证。模型批次按兼容设置和长度桶组织，当前为同 resident 内连续执行多目标，不宣称张量 minibatch 或跨地域模型同步。

完整配置、原生模型常驻、传输、接管与验收见 [混合云常驻管线](hybrid-resident-pipeline.md)。无需 GPU 的双节点演示可运行 `python3 scripts/start_hybrid_demo.py --port 8766 --data-dir ./data-hybrid-demo --seed-demo --targets 24`；两个位置标签均在本机，绝不作为实际阿里云 GPU 已连通的证据。实际吞吐仍须在相同质量设置下测量，账本写入速度不能替代结构预测速度。

## 8. 已完成的验证与上线时的第一组测量

数据库模块的临时小文件测试覆盖缺失、空文件/目录、不可读、文件类别、必要索引成员、版本/参数/元数据变化、相对路径、示例版本占位及非递归预检。CLI 测试验证 205 条以 100/100/5 入库、原文件重传零新增、候选身份保留、错误后暂停以及 dry-run 不联网。

原 campaign CLI 的真实本地 HTTP 验证提交两条公开 ubiquitin 回放，跑完准备、预测回放与验证，再重传同文件，最终保持两条结果；证据在 `artifacts/campaign-cli-verification.json`。它证明控制面与产物流转工作，未执行真实 GPU 模型。仓库中的大清单账本测试同样只衡量小序列元数据导入、幂等和队列内存行为，不能外推为蛋白推理吞吐。

另有 [双节点实际 HTTP 记录](../artifacts/hybrid-http-verification.json)，24 个公开回放目标、8 批、48 次哈希传输，用于验证常驻与远端协议。

连接真实环境后，先跑 10–100 个具有代表性的候选，分别记录搜索 wall time、准备缓存命中率、ready 等待、模型加载/编译/计算时间、峰值 GPU 显存、结果验证失败率和磁盘增长。观察 GPU 缺料就增加有效 CPU 搜索产能；ready 堆积则检查 GPU/模型启动；验证积压则检查解析、写盘和发布。只有这些实测能说明十万条在哪个阶段受限，并决定下一步优化对象。


搜索本身的后续实现见 [MMseqs2 CPU/GPU 搜索加速专题](msa-search-acceleration.md) 与 [AF3 适配器实现指南](mmseqs-af3-search.md)：支持配置级两段准备、GPU 专组互斥和逐批次策略快照；原生部署继续搁置。
