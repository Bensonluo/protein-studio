# 运行与真实计算后端接入

## 1. 先启动可以直接体验的版本

在项目目录使用 Python 3.12：

```bash
cd /Users/luopeng/Documents/GitHub/protein-studio
python3 -m backend.server
```

打开 `http://127.0.0.1:8765`。默认示例为公开结构回放，不需要 Python 第三方依赖、GPU、模型权重或在线 MSA 服务。3Dmol 脚本已放在 `web/vendor/`，浏览器展示不依赖 CDN。

也可使用 Docker Compose：

```bash
docker compose up --build -d
docker compose logs -f studio
```

浏览器地址相同。容器内绑定 `0.0.0.0:8765`，宿主机端口只发布在 `127.0.0.1`。`studio-data` volume 保存任务与文件；`docker compose down` 保留它，删除 volume 会删除任务历史。Dockerfile 的 Python 镜像只运行平台，不包含 CUDA、数据库或模型参数。

基本接口检查：

```bash
curl http://127.0.0.1:8765/api/health
curl http://127.0.0.1:8765/api/system
curl -X POST http://127.0.0.1:8765/api/jobs \
  -H 'Content-Type: application/json' \
  -d '{"engine":"reference","example":"ubiquitin-afdb","name":"公开预测结构回放"}'
```

提交返回任务 ID。浏览器或 `GET /api/jobs/<id>` 可看结果；`GET /api/jobs/<id>/structure` 下载结构。`POST /api/jobs/<id>/cancel` 请求取消，`POST /api/jobs/<id>/retry` 创建新的尝试，保留旧任务用于追溯。

## 2. 应用配置

| 环境变量 | 默认值 | 用途 |
|---|---|---|
| `PROTEIN_STUDIO_HOST` | `127.0.0.1` | API 监听地址 |
| `PROTEIN_STUDIO_PORT` | `8765` | API 端口 |
| `PROTEIN_STUDIO_DATA_DIR` | 项目 `data/` | SQLite 与任务文件目录 |
| `PROTEIN_STUDIO_WORKERS` | `1` | 同进程并发执行线程，当前限制 1–64 |
| `PROTEIN_STUDIO_GPU_GROUPS_JSON` | 未配置 | 独占 GPU 资源组，例如 `[["0"],["1"]]` |
| `PROTEIN_STUDIO_GPU_DEVICES` | 未配置 | 单卡组简写，例如 `0,1` |
| `PROTEIN_STUDIO_TIMEOUT_SECONDS` | `7200` | 单任务外部进程累计时间上限；最多七天 |
| `PROTEIN_STUDIO_MAX_RESIDUES` | `5000` | 提交时总蛋白残基数上限，须与实际引擎容量匹配 |
| `PROTEIN_STUDIO_ALLOWED_HOSTS` | 仅本机主机名 | 额外允许的 HTTP Host，逗号分隔 |
| `PROTEIN_STUDIO_<ENGINE>_COMMAND` | 未配置 | 管理员设置的 JSON argv 数组 |
| `PROTEIN_STUDIO_<ENGINE>_VERSION` | 未报告 | 实际安装版本/commit，保存到结果来源信息 |

`<ENGINE>` 是 `ALPHAFOLD2`、`ALPHAFOLD3` 或 `COLABFOLD`。`WORKERS` 是本应用并发线程数，**不是 GPU 数量**。本地真实预测可通过 `GPU_GROUPS_JSON` 配置独占设备组，任务取得资源组后设置子进程 `CUDA_VISIBLE_DEVICES`；例如 2 个单卡组与 2 个 worker。使用外部 Slurm/云 wrapper 时不设置本地 GPU 池，由远端调度器分配设备。未设置资源组时沿用启动环境的设备可见性，不能通过提高线程数假定已经隔离显存。资源组租约只协调本服务进程内的 worker；其他进程也使用同一 GPU 时，需要外部调度器统一分配。

当前服务使用单机 SQLite，并持有数据目录锁；两个 API 实例不能共享同一 `data/` 目录来充当分布式 worker。重启后，排队任务可以继续取出；中断的运行任务标为失败或已取消，需重试成新尝试。它没有模型内部 checkpoint 恢复功能。

## 3. 用配置示例接入真实程序

`config/engines.example.json` 包含可修改的环境配置示例。服务本身读取环境变量，不会自动加载文件。先复制为本机配置，替换可执行路径、数据库路径和实际版本，然后显式启动一个 profile：

```bash
cp config/engines.example.json config/engines.local.json
```

编辑 `config/engines.local.json` 后，从项目根目录运行以下命令；示例选择 `colabfold-local`：

```bash
python3 - <<'PY'
import json, os, sys
from pathlib import Path
settings = json.loads(Path('config/engines.local.json').read_text())
profile = settings['profiles']['colabfold-local']
os.environ.update(profile['environment'])
os.execv(sys.executable, [sys.executable, '-m', 'backend.server'])
PY
```

AF2 选择 `alphafold2-local` profile：命令中的 runner 路径指向本项目实际文件，移到 Linux 主机后改成新的绝对项目路径；容器内对应 `/app/scripts/af2_runner.py`。`--` 之后是官方数据库参数示例，目录名和数据库文件前缀须匹配已有安装；`max_template_date=2026-10-23` 仅是示例截止日期，应换成实验协议要求的日期。AF2 Python 必须能导入其既有模型环境。可先运行 `python3 scripts/af2_runner.py --help` 检查适配接口，正式预测仍由配置中的 AF2 Python 执行。

示例文件中 `REPLACE_WITH_INSTALLED_VERSION_AND_COMMIT` 需要填写实际值；它不是一个可验证的版本号。界面显示引擎可用，只说明已找到配置的可执行文件；模型参数、数据库和 GPU 的可用性通过首次真实任务检查。

命令模板以数组传给子进程，使用 `shell=False`。HTTP 请求只能传科研输入及预测参数，不能传可执行命令。可用占位符如下：

| 占位符 | 内容 |
|---|---|
| `{fasta}` | 已规范化的蛋白 FASTA；ColabFold 多链以冒号分隔 |
| `{input_json}` | AF3 格式的蛋白输入 JSON，包含全体 modelSeeds |
| `{job_json}` | 本应用规范化任务参数 JSON |
| `{output_dir}` | 本次进程应写入的输出目录 |
| `{seed}` | 当前进程 seed；AF3 全部 seeds 已在输入 JSON 中 |
| `{num_samples}` | 引擎对应的样本参数，含义见下表 |
| `{num_recycles}` | 请求的 recycle 参数，由接入程序落实 |
| `{model_preset}` | 单链 `monomer_ptm` 或多链 `multimer` |
| `{name}` | 固定安全作业名 `protein` |

| 引擎 | 本应用怎样执行 seeds | `numSamples` 的含义 |
|---|---|---|
| AF3 | 一次进程，JSON 中包含所有 seeds | 每个 seed 的 diffusion samples |
| AF2 | 每个 seed 一次进程，分开输出目录 | Multimer 的每个模型预测数；monomer 必须为 1，多个权重仍可输出多个结构 |
| ColabFold | 每个 seed 一次进程，`--num-seeds 1` | `--num-models`，1–5 个模型权重；不是 diffusion samples |

不能用同一个 `numSamples` 数字直接比较三个引擎的计算预算。AF2 v2.3.2 官方 CLI 没有 recycle 选项。本项目提供实际脚本 `scripts/af2_runner.py`，在独立模型进程内设置 `model.num_recycle`，并对 monomer 同步设置 `data.common.num_recycle`，随后执行官方入口；不会修改已安装源码。脚本从模型返回值直接导出安全 confidence JSON。AF3 的示例核对官方 **v3.0.4** 的 `run_alphafold.py`：采样参数为 `--num_diffusion_samples`，recycle 参数为 `--num_recycles`；运行版本变化时要重新检查。[固定版本 CLI 源码](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/run_alphafold.py)

ColabFold 自动探测到 `colabfold_batch` 时采用 `single_sequence` 模式。这便于验证本地程序且不会隐式调用公共 MSA 服务。科研使用若需要 MSA，应配置本地搜索、缓存或团队批准的 MSA 服务，并把来源和数据库版本写入任务产物。不要将默认 single-sequence 输出的质量当成完整 MSA 流程的质量。参见 [ColabFold 官方说明](https://github.com/sokrypton/ColabFold)。

## 4. legacy 命令接入与 resident workers

AF3 的跨主机常驻执行优先使用已提供的 `scripts/worker_node.py`、worker 注册与 fleet 配置；控制面留在 CPU/数据库一侧，GPU 主机安装模型环境及权重，参见 [混合云常驻管线](hybrid-resident-pipeline.md)。ColabFold 保留 legacy staged，AF2 保留完整任务 runner。以下通用命令契约用于 legacy 外部命令或已有 Slurm 作业适配，不是必须重新实现的 resident worker 服务。

wrapper 不是仅执行一次 `sbatch` 后立即退出的脚本。它须完成以下顺序：

1. 读取 `{job_json}` 和输入文件，生成外部任务 ID；将输入复制到目标执行节点，或使用双方可见且路径映射清楚的工作目录。
2. 申请正确的 GPU 数量、显存类别、CPU/内存，运行固定版本的引擎。独立结构任务通常各自领取一个任务资源组。
3. 等待外部任务结束，持续将可公开的进度/诊断写到 stdout/stderr；退出码反映真实成功或失败。
4. 把完整结构、置信度和版本元数据复制回 `{output_dir}`；先完成落盘再以 0 退出。
5. 收到 SIGTERM 时撤销外部任务并回收临时资源。平台终止本地 wrapper 进程组不等于云任务自动取消；这一步必须在 wrapper 中实现。

本项目提供 AF2 runner、legacy 命令接入点和 AF3/reference 认证 resident worker。云平台凭据、SSH/Slurm 作业适配仅在选择这些外部系统时接入；已有 resident HTTP 传输与目标租约可直接使用。先用一条小任务验证配置的实际执行路径，再增加并发。

容器接入外部 wrapper 时，需要在派生镜像中安装 wrapper 依赖，并以只读方式挂载 wrapper、模型代码及参数；输出目录必须可写且与 wrapper 路径一致。不要只把宿主机 Python virtualenv 挂进不同基础镜像后假定 ABI/CUDA 兼容。默认 Compose 不挂载 Docker socket、不获取 GPU、不装 SSH 客户端；适配现有计算设施时按实际运行路径补充依赖即可。

## 5. 输出文件要求

| 引擎 | 结构发现 | 置信度发现 |
|---|---|---|
| AF2 | 优先 `ranked_N.pdb`，否则 `unrelaxed_*.pdb` | `ranking_debug.json`；可选安全导出 `result_<model>.json`；PDB 提供 pLDDT |
| AF3 | `*_model.cif`，优先 `seed-N_sample-N/` 里的样本 | 同前缀 `*_confidences.json` 与 `*_summary_confidences.json` |
| ColabFold | 带 `_rank_N_` 的结构文件；相同预测 relaxed/unrelaxed 去重 | 对应 `_scores_*.json` |

输出必须属于该任务目录。平台检查返回蛋白链序列与输入匹配，避免脚本误返回另一条结构。AF2 原始 `result_*.pkl` 不由 Web 服务反序列化；随项目提供的 AF2 runner 在可信模型进程中从返回值直接导出 JSON，再交给平台。自定义外部程序也须提供相同 JSON，才能显示 PAE/pTM。没有置信度字段就显示缺省，不生成替代分数。legacy 完整外部任务的 wall-clock 包括实际发生的搜索、编译、预测和输出；staged/resident 分阶段记录各自时间，均不能直接作为纯 GPU kernel 时间。[AF3 官方输出契约](https://github.com/google-deepmind/alphafold3/blob/main/docs/output.md)

外部预测的可选 confidence JSON 超过 64 MiB 时，平台保留原始文件供下载，并记录跳过可视化解析的提示；仍可使用结构中的 pLDDT 和较小 summary JSON 的 pTM/ipTM/ranking score，任务不会仅因这个可选文件过大而失败。直接导入和 API 请求继续执行原有体积限制。

当前 UI 输入是标准氨基酸蛋白链。AF3 上游能处理的核酸、配体、修饰残基并未全部暴露在本界面；需要这些实体时应扩展输入 schema 和解析验收，不能以“AF3 已配置”推断这些交互已实现。

## 6. 本地 16 卡加云：已实现的 resident 路径

`PROTEIN_STUDIO_DISTRIBUTED=1` 与 `PROTEIN_STUDIO_PREDICTION_MODE=workers` 启用认证 pull worker：CPU 准备与验证保留，campaign 的 GPU 阶段由注册节点领取；旧本地 prediction 线程关闭。AF3 原生模型权重常驻，reference 用于协议回放；ColabFold 继续使用 legacy staged。

本地 16 卡以 [fleet.local16.example.json](../config/fleet.local16.example.json) 声明每台主机的实际设备，再由 [launch_worker_fleet.py](../scripts/launch_worker_fleet.py) 启动对应 resident worker。该 AF3 实现每个进程使用一张卡，单卡目标并行；分配多设备不会使它自动变成多卡模型。卡分布于多台机器时分别启动本机 fleet，节点 ID 全局唯一。

控制面 [worker 注册配置](../config/workers.example.json) 绑定各节点 token、位置与引擎；ECS/ACK 既有 GPU 节点使用同一 agent 经 HTTPS 领取目标。campaign placement 限制 local/aliyun/hybrid，管理员与 runner 的 maxResidues 共同限制可领取输入，须依据真实显存和质量测试设置。已有任务身份、兼容批次、SHA-256 准备/结果包、逐项回执与租约恢复，不用另写一层 SSH 文件搬运实现这些功能。

fleet 对失败 slot 使用有次数上限的退避重启，健康进程不受影响。Linux 也可按单节点使用 [foldspace-worker@.service](../deploy/foldspace-worker@.service) 与 [worker.env.example](../deploy/worker.env.example) 托管；同一个 GPU 只选择一种启动管理方式。完整步骤和参数见 [混合云常驻管线](hybrid-resident-pipeline.md)。

此架构仍是单控制器 SQLite：未提供控制面高可用、阿里云实例自动创建或真实多机模型并行。生产百万目标应按实测验证账本、磁盘、网络与 GPU 吞吐，不将十万条元数据入库测试当作真实推理测试，也不先承诺一个未经测量的优化百分比。

## 7. 最小上线验收

先在默认 reference 模式验证 P0CG47 的结构、真实 PAE、下载与来源信息，以及 1UBQ/1CRN 的实验结构不出现伪造 pLDDT。再对真实引擎用一个短序列跑通，检查 `input/submission.json`、进程日志、`execution.json` 和原始输出是否一致。

继续验证失败路径：缺数据库必须报错、超时必须结束进程、取消必须撤销远端作业、错误序列输出必须被拒绝、重试要保留原尝试。然后在选定 GPU 上测 1 个与多个合法资源组的并发，记录峰值显存和合格吞吐，再决定 worker 数、GPU 组或调度器资源配置。

当前默认只供本机使用；多人访问需要企业认证入口、访问授权和 HTTPS 代理配置。`ALLOWED_HOSTS` 仅校验请求主机名，不是登录系统。若通过代理访问，需实测同源 Origin 校验与代理头映射，不直接把默认本机服务暴露为公共 API。

## 8. 本次实际验证记录

- `docker compose config --quiet` 已通过，Compose 配置可解析。
- 配置文件的 JSON argv、占位符、AF3 v3.0.4 参数名、项目内 AF2 runner 路径检查通过；`python3 scripts/af2_runner.py --help` 可在未安装模型环境中正常显示接口。
- 本机 Docker CLI 存在，但 Docker daemon 未运行（socket 不存在），因此本次未执行镜像构建或容器启动；不能将 Compose 解析通过写成容器运行通过。
- 模型权重和数据库未下载，真实 GPU 预测需在配置的模型环境执行；项目测试验证适配器协议和公开样例流程。

其他 API、解析和界面测试由项目根 README 记录；以上记录只覆盖此部署配置本身。

## 9. 多目标 FASTA 的批量提交与续传

`scripts/submit_batch.py` 将 **每条 FASTA 记录提交为一个独立的单链预测任务**。例如 `>candidate_A` 和 `>candidate_B` 是两个任务；它们不会被合并成两条链的复合物。预测复合物请使用页面的单任务多链输入。

先验证文件、预测参数和预计提交数，不连接 API、不写提交清单：

```bash
python3 scripts/submit_batch.py \
  --server localhost --engine colabfold --input candidates.fasta \
  --name-prefix screening- --seeds 1,2 --samples 5 --recycles 3 \
  --configuration-id colabfold-1.6.3-local-db-2026Q3 \
  --chunk-size 100 --manifest batch-run.jsonl --dry-run
```

确认后使用相同参数去掉 `--dry-run`，加上 `--resume` 开始或继续提交。`--limit 20` 可只检查或提交输入文件前 20 条记录；它包含续传时被跳过的记录，不表示额外提交 20 条。`--samples` 对 AF2 单体必须为 1；对 AF3 表示 diffusion samples，对 ColabFold 表示模型权重个数。

工具逐行读取 FASTA，每次仅保留当前记录及一个提交块；客户端每块最多 100 个任务。它调用 `POST /api/jobs/batch`，服务端按整块先校验、再以一个 SQLite 事务入队。某块有非法任务则该块不入队，前面已成功的块仍然保留。空记录、模糊氨基酸和冒号分链语法会报错，建议完整执行一次 dry-run 后再正式提交。

服务端确认成功后，工具将每个任务的 ID、原始完整记录名、序列 SHA-256、预测参数和指纹追加到 JSONL 清单。指纹包含记录名、序列、参数、名称前缀、服务地址及 `configuration-id`；同一序列的不同候选名保持为不同任务，完全相同的重复记录也用出现次数分别标识。显示名称超过 120 字符会截短，但完整名称仍参与指纹。`--resume` 只跳过已有确认记录，不查询或改变任务执行状态。模型、权重或数据库改变时，更新 `configuration-id`；重跑已失败任务可使用页面重试，或明确采用新清单开始新的提交。

续传索引使用临时磁盘 SQLite，不把整个历史清单加载到内存。清单只保证已收到确认并写入的提交可跳过；网络断开、服务已接受但回执丢失，或客户端在写清单前中断时，结果可能未知。工具不会自动重发此块，应先核对服务器任务记录，避免重复耗用 GPU。续传时保持相同服务地址、候选标识与参数，且一个清单只由一个提交进程写入。

此工具解决文件流式读取、分块提交和客户端续传，**不会把当前单机 SQLite 服务变成百万任务分布式调度器**。不要将百万条候选拼成一次请求，也不要把批量入队速度当成预测吞吐。先用小批量验证真实 GPU 引擎和存储容量，再交由现有批处理系统管理生产规模任务。页面只显示最近 200 个任务；批量历史以 JSONL 清单中的任务 ID 与 API 记录追溯。

客户端标准库测试命令：

```bash
python3 -m unittest discover -s tests -p 'test_submit_batch.py' -v
```

测试覆盖流式解析、独立候选、参数校验、干跑、HTTP 分块、确认清单、续传和失败不自动重发。真实本地 API 的提交与续传核验记录保存在 `artifacts/batch-verification.json`；这验证提交协议，不代表执行了 GPU 模型预测。

## 10. 本地数据库与分阶段大批次

十万条候选请使用 campaign 管线：数据库连接、CPU 搜索准备、ready 背压、GPU 预测及独立 CPU 验证见 [数据库与批次管线](database-and-batch-pipeline.md)。执行环境配置见 [staged runners](staged-runners.md)。数据库注册由 `PROTEIN_STUDIO_DATABASE_CONFIG` 指向管理员 JSON；API 只接受已注册的 profile ID。`scripts/submit_campaign.py` 支持流式 FASTA、100 条分块、seal/resume、稳定 targetId 幂等重传；原 `submit_batch.py` 保留为完整单任务队列入口。

默认容器仍运行公开结构回放。真实数据库只读挂载、模型权重与运行环境按新文档配置；不要把大型库 COPY 进镜像，也不要把增加 `WORKERS` 当成跨主机 GPU 调度。

## 11. 一键本机双节点演示

```bash
python3 scripts/start_hybrid_demo.py \
  --port 8766 --data-dir ./data-hybrid-demo \
  --seed-demo --targets 24 --batch-targets 3
```

打开 `http://127.0.0.1:8766/#cluster`。脚本启动真实控制面、两个真实 HTTP worker 与常驻 reference 子进程，生成仅供本次进程使用的临时凭据；它不创建云实例，也不下载模型。local 与 aliyun 标签都指向本机演示，modelLoads 为 0。日志和账本保留在指定数据目录，Ctrl+C 停止进程；已有端口占用时另选端口，不结束不属于本次启动的服务。

实际双节点记录见 [24 目标 / 8 批 / 48 次传输](../artifacts/hybrid-http-verification.json)，逐项失联恢复见 [恢复证据](../artifacts/hybrid-recovery-verification.json)。真实 GPU 与公网条件仍需独立验收。
