# Foldspace · 蛋白结构预测工作台

把序列提交、真实预测进程、结构与置信度解析、三维探索和结果交付连接起来的本地应用。参考 AlphaFold2 / AlphaFold3 的实际数据流程，接入 ColabFold，并采用 3Dmol.js 展示真实分子坐标。

[腾讯云公开演示](https://benluo.art/foldspace/) · [作品集介绍](https://benluo.art/projects/protein-studio/)

公开演示使用独立的已发布结构样例，支持三维、PAE 和批次结果浏览；部署与访问方式见 [公开展示部署说明](deploy/PUBLIC_DEMO.md)。

![工作台预览](artifacts/studio-desktop.png)

## 启动

需要 Python 3.10+，推荐 3.12。先在项目虚拟环境安装流式 JSON 依赖；界面无需安装 Node.js，公开结构回放无需下载模型。

```bash
cd /Users/luopeng/Documents/GitHub/protein-studio
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./start.sh
```

打开 **http://127.0.0.1:8765**。macOS 也可以双击 `启动工作台.command`。

启动器优先使用项目 `.venv`。如果指定独立控制器或 AF3 resident Python 环境，也需要在该环境安装 `requirements.txt`；不会自动联网安装依赖。

若系统默认 Python 版本较旧，指定解释器：

```bash
PROTEIN_STUDIO_PYTHON=/path/to/python3.12 ./start.sh
```

默认示例是 AlphaFold DB 的 **P0CG47 / Polyubiquitin-B，229 残基**历史预测，包含原始结构、pLDDT 与 PAE。界面明确标注公开参考数据；它用来直接体验结果分析流程。选择已配置的 AlphaFold2、AlphaFold3 或 ColabFold 后，自定义序列会交给真实程序计算。

## 已实现的工作流

1. **提交**：单链或多链 FASTA、输入校验、引擎选择、种子、引擎对应的采样参数、recycles、队列优先级；多记录 FASTA 可选择作为独立批量任务。
2. **执行**：持久化任务与 campaign 队列；legacy 模式调用单目标运行器，workers 模式由本地/远端 agent 拉取批次并复用 AF3 常驻模型进程；支持取消、超时和逐项失败记录。任务支持 `#job/{id}` 深链直开、选择跟随 URL 与浏览器前进/后退（AF DB / SWISS-MODEL 工作区稳定结果 URL 惯例）。
3. **资源**：本地独占 GPU 组与认证的远端 worker；按引擎、参数、位置和实测后配置的目标长度上限分发，区分规划 16 卡与真实注册在线节点。
4. **解析**：PDB / mmCIF、模型排名、pLDDT、PAE、pTM / ipTM（原始输出存在时）。实验结构 B 因子保留其含义。
5. **可视化**：WebGL 三维 cartoon、球棍及分子表面；按置信度、链或序列光谱着色；多排名采样可在三维视图间切换（下拉列出排名 / 样本名 / pLDDT / 排名分，表格提供"三维查看"，非主排名按结构文件 B 因子即 pLDDT 着色与逐残基曲线导出——AF3 Server / ColabFold 的选中采样置信度惯例——文件无可用 B 因子时回退按链着色且不虚构曲线）；多采样任务在模型面板顶部给出各采样排名分（或平均 pLDDT）横向对比条，标注首名领先幅度——ColabFold ranking plot / AF3 Server 的采样分差解读惯例——当前查看采样高亮）；旋转、缩放、全屏、截图和残基定位。
6. **分析与交付**：序列联动、逐残基置信度曲线、PAE 热图数值查询、多链任务的链间界面置信度表（逐链 pTM 与平均界面 ipTM，AF3 chain_ptm / chain_iptm 口径，ipTM 按 EBI 阈值分带，附非对称界面需结合 PAE 的提示——AF3 Server / EBI 结果页惯例）、结构与制品下载、"下载全部 zip"打包已注册制品（AF3 Server / ColabFold 的单档案交付惯例）、任务 JSON 导出、执行日志。任务列表（全部实验 / 最近任务）带置信度列：平均 pLDDT 按 AF DB 四带、ipTM 按 EBI 阈值显示分带徽标，未评分任务显示占位符（SWISS-MODEL 结果表逐模型质量分列、AF3 Server 按置信排名的列表惯例），全部实验表头可按平均 pLDDT 降序排序（BLAST 结果表分值列排序惯例，aria-sort 标注）。
7. **导入**：上传本地 PDB / mmCIF，持久保存并进入同一分析界面。界面可同时导入 PAE、confidence 和 summary JSON，并选择实验 / 预测来源。
8. **恢复**：SQLite 保存任务与事件；resident campaign 按目标续租、校验回传并恢复失联未完成项，旧尝试不能覆盖新结果；legacy 单任务中断保留失败记录供重试。

## 大批次与搜索数据库

左侧 **批次管线** 接收多记录 FASTA 或多链 JSONL，浏览器流式读取文件、每 100 项写入一次。任务经过独立的 CPU 准备池、就绪缓冲、预测执行池和结果验证池；清单总量与就绪缓冲水位分别控制。支持阶段计数、25 项分页、暂停、继续、取消、仅重试失败项、逐项三维结果，以及导入中断后的幂等重传；已交付目标按 AF DB pLDDT 四带与 EBI ipTM 阈值给出置信度分带汇总与分布条，列表支持排名分/ipTM/pTM/pLDDT 排序与阈值筛选，导出跟随当前筛选。存在多个批次时提供批次概览列表（状态、目标、交付、失败与置信度分布条，点击切换）。固定靶标筛选模式将每条候选与同一组固定链组合，预览前两项后按块上传；刷新后的续传会核对固定靶标指纹。

左侧 **序列与模板库** 显示管理员注册的本地数据库资产、版本和必要索引的预检状态。搜索进程直接读取挂载路径，不通过网页查询一个远程 SQL 序列库。示例路径没有安装真实数据库，会如实显示缺失。

- AF3：原生数据准备生成含 MSA / 模板的 JSON，预测阶段使用 `--run_data_pipeline=false`，其中仍包含 AF3 原生 featurisation。
- ColabFold：本地 `colabfold_search` 生成 A3M，再交给预测进程，默认不调用公共 MSA 服务。
- AF2：保留原有完整任务运行器，当前没有拆分接入新的阶段调度器。
- 准备缓存包含输入、数据库指纹和搜索版本，支持跨进程 single-flight、完整性检查和原子发布。AF3 自动准备采用整目标与独立链两层缓存：同一靶标与不同候选组合时，复用靶标原始 MSA 与模板，再组装各自的完整预测输入。

数据库安装、配置、十万级导入、混合云落地与剩余吞吐工作见 **[数据库与批量管线说明](docs/database-and-batch-pipeline.md)** 和 **[分阶段运行器契约](docs/staged-runners.md)**。新批次使用 `scripts/submit_campaign.py`；旧 `submit_batch.py` 保留兼容原任务接口。

准备池另支持 CPU、内存与磁盘估计预算准入：预算不足先不领取新目标，释放后继续。预算、预留与等待原因在批次页可见。配置与筛选操作见[固定靶标筛选与准备资源准入](docs/screening-and-preparation.md)。

MSA 准备支持共享链依赖让出：同一靶标链正在搜索时先准备其他候选链，全部剩余依赖繁忙时保存续跑记录并释放准备槽位。批次页展示搜索、共享等待、组装的当前目标数；逐链详情展示原始 MSA 深度、覆盖率分位、gap 与首次准备程序墙钟。完整机制与供料诊断见 [MSA 搜索与吞吐](docs/msa-performance.md)。

搜索本身新增 MMseqs2 CPU / GPU 两段适配器：先做 ColabFold 未配对搜索，释放搜索 GPU，再在 CPU 补齐原生 UniProt 配对候选与模板。数据库配置绑定后端、版本和输入策略，批次保留快照；GPU 使用专组及同节点设备互斥。选型与公开性能条件见 [MSA 搜索加速专题](docs/msa-search-acceleration.md)，字段与实际命令见 [MMseqs → AF3 实现指南](docs/mmseqs-af3-search.md)。示例配置未部署，外部 gpuserver 生命周期尚未接管。

当前使用一个 SQLite 控制节点保存账本，并已实现认证的远端拉取、带 SHA-256 的准备/结果包传输、目标级租约和恢复。`legacy` 仍按目标调用模型子进程；`workers` 模式支持 AF3 原生权重常驻及 reference 协议回放，ColabFold 仍使用 legacy staged 路径。控制面十万项验证不代表十万次真实结构预测已执行。

所有前端资源与示例都在本项目内。默认体验不需要访问 CDN，不会将输入序列自动发送到外部服务。

## 一键体验混合执行

使用独立端口和数据目录运行两个真实 HTTP 演示节点：

```bash
python3 scripts/start_hybrid_demo.py \
  --port 8766 --data-dir ./data-hybrid-demo \
  --seed-demo --targets 24 --batch-targets 3
```

打开 **http://127.0.0.1:8766/#cluster**，查看两个常驻进程、批次、准备包下发与逐项回传。两个节点均在本机运行，`local` / `aliyun` 是明确的演示位置标签，模型权重加载数为 0；这不是已连接真实阿里云 GPU。Ctrl+C 停止演示进程，数据目录保留记录。

真实部署入口：[混合云与常驻管线](docs/hybrid-resident-pipeline.md)、[本地 16 卡 fleet 示例](config/fleet.local16.example.json)、[16卡与云端 worker 注册示例](config/workers.local16-aliyun.example.json)、[systemd 单 worker 服务](deploy/foldspace-worker@.service)。fleet 只重启失败 slot，使用有次数上限的退避，不因一个进程退出重启其他健康 GPU worker。

## 管线可视化与排查

**混合云执行**提供状态驱动的本地/云端拓扑、16卡规划格、全局阶段计数、可路由准备缓冲、按位置的领取条件与近五分钟交付/失败/恢复统计。动画随实际执行状态启停，卡位计划与节点上报设备分别显示。点击执行池筛选节点，点击批次展开其成员，再进入目标详情。

**批次管线**中所有阶段的目标都能检查：事件、失败阶段、阶段累计时间、准备缓存、输入指纹、执行节点/常驻PID及近期租约记录；已交付目标详情含 AF3 排名分与 EBI/AF DB 口径分带的置信度评分。已完成目标可直接进入三维视图；全部、已交付与失败项均可下载为一个JSONL清单。导出只取metadata与结构链接，以每次200行写临时文件形成一致快照，结束数据库读事务后再发送给浏览器，不聚合整个批次的PAE或坐标。

AF3 的链复用统计单列新增链搜索、链缓存命中与整目标命中。单个目标详情可展开所用链 ID、序列 SHA 与缓存身份；整目标命中时明确标注本次没有查询链缓存。固定靶标 × N 个不同候选，理想冷缓存需要 N+1 个独立链搜索身份，仍执行 N 次完整复合物推理。这是官方支持的准备复用机制，不能直接换算为 GPU 加速百分比。完整机制、配对语义、适用输入与验证见[链级复用专题](docs/chain-reuse-design.md)。

2026-10-23：按用户决定，真实节点部署暂缓。新增 Linux 配置生成器、数据库/GPU探针与服务模板保留；现场安装与 GPU 推理验收尚未执行，其中 `verify_af3_campaign.py` 是未验收草稿。当前继续改进准备、分发、常驻和结果回收管线。

workers模式仅为当前在线且兼容的模型节点预取输入；无匹配节点的任务保持待准备，已有准备产物留在缓存。不可路由的旧ready不会挡住其他位置/引擎的可执行任务。成功上传须收到匹配item和SHA的接收回执才清节点暂存；不确定上传继续保留。已执行目标发生OOM后停止自动重算，调整输入、设置或硬件后手动重试；未开始目标仍可恢复。

![混合执行预览](artifacts/pipeline-rich-preview.png)

## 连接真实预测

模型在已准备好权重、数据库、依赖和 GPU 的环境中运行。工作台提供三种适配器，管理员通过 JSON 参数数组配置实际命令：

| 引擎 | 输入与执行 | 输出 |
|---|---|---|
| AlphaFold2 | FASTA；按 seed 独立运行；提供官方环境运行器 | ranked PDB、排名与可获得的置信度 |
| AlphaFold3 | 官方 dialect JSON；legacy 一次运行包含全部 seeds，workers 复用常驻进程 | mmCIF、sample 排名、置信度 JSON |
| ColabFold | 单链或冒号分隔多链 FASTA；按 seed 独立运行 | ranked PDB、scores JSON、PAE |

更大 FASTA 可用 `scripts/submit_batch.py` 流式分批提交并记录断点清单。完整配置、命令模板、官方参数引用、16卡设备池和云端运行器契约见 **[部署说明](docs/deployment.md)**；可编辑配置位于 **[engines.example.json](config/engines.example.json)**。

当前实现是单控制器、可连接多台实际 worker 的任务协议，未提供控制面高可用、云实例创建或自动扩缩容。已有 Slurm/ACK 等系统可负责设备分配与服务托管；worker 负责领取兼容目标、常驻执行和结果回传。有关现成工作流的采用理由见 **[研究与实现依据](docs/research.md)**。

## API 示例

```bash
# 参考结构：执行校验、读取真实制品、解析并保存结果
curl -X POST http://127.0.0.1:8765/api/jobs \
  -H 'Content-Type: application/json' \
  -d '{"name":"Polyubiquitin-B","engine":"reference","example":"ubiquitin-afdb"}'

# legacy 模式且已配置真实引擎时提交自定义蛋白；workers 模式使用 campaigns
curl -X POST http://127.0.0.1:8765/api/jobs \
  -H 'Content-Type: application/json' \
  -d '{"name":"Protein experiment","engine":"alphafold3","chains":[{"id":"A","sequence":"YOUR_PROTEIN_SEQUENCE"}],"seeds":[1],"numSamples":5,"numRecycles":10}'
```

| 接口 | 功能 |
|---|---|
| `GET /api/health`、`GET /api/system` | 服务、引擎与本机 GPU 检测 |
| `GET /api/databases`、`GET /api/pipeline/config` | 数据库预检、阶段并发与引擎能力 |
| `GET /api/cluster` | 全局pipeline态势、规划资源、实际节点、resident批次、哈希传输与恢复事件 |
| `GET /api/campaigns/{id}/export?stage=...` | 一致快照JSONL下载；可选按阶段筛选，行内含置信度分数；支持与目标列表相同的 `sort=ranking/iptm/ptm/plddt` 与 `min_*` 阈值，导出筛选后的子集 |
| `POST /api/campaigns`、`GET /api/campaigns` | 创建 / 查看批次 |
| `POST /api/campaigns/{id}/items`、`GET /api/campaigns/{id}/items` | 逐块入库、按阶段分页；`sort=ranking/iptm/ptm/plddt` 置信度降序，`min_iptm/min_ptm/min_plddt/min_ranking` 阈值筛选 |
| `POST /api/campaigns/{id}/seal`、`pause`、`resume`、`cancel` | 封存输入清单与控制执行 |
| `POST /api/campaigns/{id}/retry-failed` | 仅重新排队失败目标，保留已完成项 |
| `GET /api/storage`、`/entries`、`/plans/{id}` | 已有缓存目录清单、操作状态与具体预览；不启动目录扫描 |
| `POST /api/storage/scan`、`plan`、`apply`、`cancel` | 有界扫描、具体计划、确认执行与取消；永久删除默认禁用 |
| `GET /api/campaigns/{id}/items/{itemId}`、`/structure` | 单项目标详情与结构 |
| `GET /api/examples/ubiquitin-afdb` | 参考输入与来源 |
| `GET /api/jobs`、`GET /api/jobs/{id}` | 任务列表、参数、事件和结果 |
| `POST /api/jobs`、`POST /api/jobs/batch` | 单条或批量提交，批量每次最多200条 |
| `POST /api/jobs/{id}/cancel`、`POST /api/jobs/{id}/retry` | 取消或创建新尝试 |
| `GET /api/jobs/{id}/structure` | 下载主结构 |
| `GET /api/jobs/{id}/archive` | 打包下载该任务全部已注册制品（zip 一致快照；仅已完成任务） |
| `GET /api/jobs/{id}/artifact?name=...` | 下载已注册的任务制品 |
| `POST /api/import` | 导入结构与可选置信度 |
| `DELETE /api/jobs/{id}` | 删除已结束任务及其本地结果 |

## 验证

```bash
.venv/bin/python -m unittest discover -s backend/tests -v
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
node --check web/app.js
node tests/campaign_client.cjs
node --check web/storage.js

# 可选浏览器自动测试；服务先运行
npm install
npx playwright install chromium
npm run test:ui
```

测试使用公开结构、真实 HTTP、受控外部进程及浏览器。MMseqs 迭代后：后端发现 298 项，297 通过、1 项按需规模测试跳过（十万账本已在上一轮单独通过）；客户端 67 项通过。新增 12 项搜索适配器、8 项注册/阶段整合、7 项设备生命周期测试包含在后端全测中。新策略浏览器契约 10 项、真实适配器 HTTP 浏览器 9 项、当前工作台只读 4 项与管线只读 8 项通过；原 MSA 11 项与链复用 8 项亦通过。条件与源码哈希见 [本轮 MMseqs 验收](artifacts/mmseqs-search-verification.json)，此前 [MSA 让出与 QC 验收](artifacts/msa-preparation-verification.json)、[大结果与 PAE 验收](artifacts/pae-results-verification.json) 独立保留。

已保存的 [双节点真实 HTTP 证据](artifacts/hybrid-http-verification.json) 为 24 / 24 目标、8 批、48 次哈希传输；[真实进程暂停恢复证据](artifacts/hybrid-recovery-verification.json) 记录逐项保留与接管。两者都是本机 reference worker，未执行真实 AF3 GPU 或阿里云公网性能测试。原生 AF3 常驻适配器已实现并通过受控协议/接口测试，科学输出与真实硬件性能仍需在已安装权重的 GPU 环境验收。

## 文件结构

```text
backend/        HTTP API、SQLite账本、远端worker协议、常驻进程与引擎适配
web/            无构建步骤的应用与本地 3Dmol 库
fixtures/       真实公开结构、PAE、来源与散列
config/         真实引擎配置示例
scripts/        模型运行器、resident worker、fleet与一键演示
deploy/         单worker systemd服务与环境模板
docs/           官方依据、方案取舍与部署
sources.json    可追溯的原始研究/实现来源
data/           运行后生成的任务数据库与制品
tests/          浏览器端到端验证
```

缓存生命周期见 [扫描、隔离、恢复与删除](docs/cache-lifecycle.md)。导航“存储与缓存”提供按需扫描和具体计划；准备发布、预测复制与远端打包参加同一维护锁协议。活动、暂停及失败可恢复任务保留引用；隔离内容仍占磁盘，永久删除需要独立计划与确认。主工作区只运行扫描，破坏性验收使用临时缓存。

大结果流程见 [流式 PAE 与按需查看](docs/large-results.md)。先按轻量指标选出最佳采样结果，再保存文件化矩阵；详情不返回大型 PAE，预览最多256²、局部窗口最多128²个原始值。原生置信度 JSON 保留下载，混合云只上传原生输出，控制端独立生成派生矩阵。真实模型部署仍搁置。

本项目的界面与编排代码按 MIT 许可证提供。第三方库、模型代码、权重和数据库分别遵循其原有条款，详见 `THIRD_PARTY_NOTICES.md` 与来源记录。

链级复用新增 16 项机制测试、7 项账本/调度测试；2×3 实际 CPU 准备子进程经调度器发布后得到 5 次新搜索与 7 次命中。新增浏览器契约 8 项使用模拟 API，真实当前服务架构检查 5 项只读，目标详情 12 项回归使用公开结构回放。十万条元数据在本轮新索引下重新验证：导入约 7.677 秒、末页约 0.124 秒、汇总约 0.046 秒；这些数值均不代表模型吞吐。测量条件见 [当前元数据基准](artifacts/pipeline-scale-pae-iteration.json)，此前链复用机制证据见 [链复用验收](artifacts/chain-reuse-verification.json)。

最新筛选/准入验收：固定输入浏览器11项（真实本地HTTP/SQLite入库、受控AF3可用性）、资源面板6项（mock API）、实际服务只读检查8项、详情回归12项通过。组合联测覆盖真实CLI/HTTP/调度器：2条固定链×3候选，磁盘不足零领取，恢复后5次合成搜索/4次链命中，幂等重放无新增搜索。十万候选扫描和1000块提交的Python跟踪分配峰值均小于8 MiB（后者API回执为替身，不是100000次真实预测）。

### 前端功能与体验检查

本轮提交、批次、结果视图与运维交互修复及验证记录见 [前端体验检查报告](docs/frontend-ux-review.md)。使用 `npm run test:workspace-ux` 运行隔离交互回归；`npm run test:workspace-ux-live` 只读检查当前服务。排名模型切换契约使用 `npm run test:model-ui`（模拟多采样 API，13 项，含"下载全部 zip"交付、任务列表置信度分带与排序、采样排名分对比条、链间界面置信度表、按采样置信度曲线与 `#job/{id}` 深链检查），逐残基着色函数回归在 `npm run test:workspace-ux` 的着色用例中。任务深链端到端契约使用 `npm run test:deeplink`（需 `STUDIO_URL` 指向隔离服务）。
