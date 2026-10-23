# 现成方案调研与本项目的技术选择

核查日期：2026-10-23。这里只列官方代码、官方文档、数据库和作者论文。`sources.json` 保存逐条来源、访问日期和用途。下述版本是调研时见到的版本；实际运行版本由管理员在引擎配置中填写，并随任务保存，不能用文档版本替代实验版本。

## 本次交付采用什么

采用 **Python 标准库 API + 本地任务存储 + 外部预测程序适配 + 浏览器结构查看**。首先把提交、输入校验、排队、日志、结果解析、置信度展示和下载这条链路跑通，再把 GPU 预测程序接到同一接口。默认公开结构回放使没有 GPU 的电脑也能检查完整用户流程；它保留数据库来源，属于已有结构展示。

真实预测适配集中在 AF2、AF3 和 ColabFold。它们的输入格式、seed 和 sample 含义各自保留；不能给三个不同模型都套一个“生成数量”并假装计算量相同。真正的大批量执行交给已有调度器或工作流引擎，Web 服务提供提交与结果入口。具体接入见 [deployment.md](deployment.md)。

## 模型、管线与许可对照

| 方案 | 本次核实的版本或快照 | 代码与权重 | 适合什么 | 本项目状态 |
|---|---|---|---|---|
| 官方 AlphaFold 2 | 最新 release 页面为 v2.3.2 | 代码 Apache-2.0；官方参数 CC BY 4.0；数据库各有条款 | 蛋白单体及 AlphaFold-Multimer；已有 AF2 科研流程的复现 | 命令适配，管理员配置数据库、参数和运行环境 |
| 官方 AlphaFold 3 | 最新 release 为 v3.0.4；v3.0.3 起代码改为 Apache-2.0 | 代码 Apache-2.0；权重另有使用条款，仍限定指定非商业用途 | 包含蛋白、核酸、配体等的复合物；本界面先接蛋白输入 | 命令适配；不捆绑权重 |
| ColabFold | `pyproject.toml` 为 1.6.3 | 代码 MIT；明确声明模型权重另有许可证 | AF2 系列批量预测与 MMseqs2 MSA 流程 | 可调用本地 `colabfold_batch`；默认不向公共 MSA 服务发送输入 |
| nf-core/proteinfold | 2.0.0 文档 | 工作流 MIT；各引擎、数据库、参数保持自己的条款 | 把多模型、数据库、容器和批处理组织为可复用管线 | 后续批处理后端候选，当前未嵌入 |
| Nextflow | stable 文档快照；实际部署需固定发行版 | Apache-2.0；不提供模型权重 | 把计算步骤映射到本地、Slurm、云或 Kubernetes executor | 后续执行层候选，当前不是依赖 |
| OpenFold | 官方 main 快照 | 代码 Apache-2.0；使用 DeepMind 参数时遵循 CC BY 4.0 | 需要 PyTorch AF2 实现、训练或研究模型内部的团队 | 未适配；不从代码许可证推断其他权重权限 |
| Protenix | 官方 README 列 v1、v2，v2 发布于 2026-04-08 | 代码 Apache-2.0；v2 权重明确为 proprietary，不能概括为开源权重 | AF3 类体系与可研究的训练、推理实现 | 未适配，具体模型名与权重条款须逐一匹配 |
| Boltz | 官方 main，Boltz-1/2 系列 | 官方明确代码与权重 MIT | 生物分子复合物；Boltz-2 另有亲和力输出 | 优先评估的新增引擎候选；当前无按钮冒充可运行 |

来源：[AF2 代码与参数许可](https://github.com/google-deepmind/alphafold)、[AF2 releases](https://github.com/google-deepmind/alphafold/releases)、[AF3 releases](https://github.com/google-deepmind/alphafold3/releases)、[AF3 代码许可](https://github.com/google-deepmind/alphafold3/blob/main/LICENSE)、[AF3 权重条款](https://github.com/google-deepmind/alphafold3/blob/main/WEIGHTS_TERMS_OF_USE.md)、[ColabFold 版本及许可](https://github.com/sokrypton/ColabFold/blob/main/pyproject.toml)、[proteinfold](https://github.com/nf-core/proteinfold)、[Nextflow](https://github.com/nextflow-io/nextflow)、[OpenFold](https://github.com/aqlaboratory/openfold)、[Protenix](https://github.com/bytedance/Protenix)、[Boltz](https://github.com/jwohlwend/boltz)。

对于药企场景，AF3 **代码**改许可并不意味着其原始**权重**已开放商业使用；采用官方权重前要匹配现有授权。Boltz 是值得实测的替代候选，但模型替换必须重新验证结构和接口质量，不能依据许可证或论文标题推断两者结果相等。这里不自动下载模型，不注册公共推理服务。

## 为什么没有重写一个预测模型

平台需要解决的是可靠地把输入送到正确模型，并让结果可追溯。AF2 的 FASTA 与数据库流程、AF3 的 JSON 与多 seed/sample 输出、ColabFold 的 MSA 选择已经有成熟实现。重写这些模型会把模型正确性、数据库维护和平台交付绑在一起。本项目只适配输入、进程执行与输出解析；模型仍由原实现负责。

AF3 的官方输出包括 mmCIF、summary confidence 和完整 confidence 数组，并保留不同 seed/sample 的结果。`ranking_score` 是排序依据，不能显示成百分比准确率；PAE 是 token 间相对位置误差估计，pLDDT 是局部置信度。AF3 的 atom pLDDT 与 AF2 常见 residue pLDDT 不能未经聚合就拼成同一逐残基曲线。[官方输出字段](https://github.com/google-deepmind/alphafold3/blob/main/docs/output.md)

ColabFold 可把 MSA 搜索与预测分开，本地搜索路径可避免每个任务重新查询公共服务。官方还提供编译缓存和查询排序机制；是否提升当前批次吞吐取决于长度、MSA、设备和 JAX 环境，需实测。本项目没有给出虚构加速倍数，也不把“single sequence 可运行”写成与完整 MSA 质量等同。[官方 ColabFold 使用说明](https://github.com/sokrypton/ColabFold)

## 批量管线：优先复用 nf-core/Nextflow

proteinfold 2.0.0 的使用文档列出 AF2、AF3、ColabFold、ESMFold、Boltz 等模式，支持 samplesheet 及单个 FASTA 拆成多个独立目标；它也描述各模式数据库目录。该能力很适合把候选清单变成可恢复的批次。项目内的轻量队列仍用于单机体验和接入测试；百万级批次应由成熟工作流管理输入分片、阶段产物、重试与日志。[proteinfold 2.0.0 usage](https://nf-co.re/proteinfold/2.0.0/docs/usage/)

Nextflow 将工作流逻辑和执行平台分离，executor 可把 process 放到 Slurm 等资源系统。若团队已有 Slurm + Apptainer，保留该路径；若已有云端 Kubernetes，使用对应执行路径。容器运行环境与作业调度是两层，不需要为一个 Web 界面把集群整体迁移到 Kubernetes。[官方 executors 说明](https://docs.seqera.io/nextflow/executor)

本地 16 卡加云 GPU 的首选扩展单位是**独立目标或合法的 seed/sample 子任务**，共享输入标识和结果索引；不是让一个默认单卡程序跨互联网把多卡显存拼起来。组内多卡仅在选定实现真实支持且实测受益时启用。平台应在任务清单中记录所需 GPU 数与显存类，不能把调度器分配了 4 卡等同于模型用了 4 卡。

## 结构、序列与 PAE 可视化选择

| 组件 | 核实内容 | 本项目选择 |
|---|---|---|
| 3Dmol.js | WebGL 分子查看、PDB/mmCIF、cartoon/stick/surface、选择与着色；BSD-3-Clause，内含第三方许可说明 | 使用随项目存放的 JS 和许可证，页面无需访问 CDN 即可显示结构 |
| Mol* | 成熟的分子数据查看工具；MIT | 大型复合物、更多结构元数据和复杂交互的升级候选，当前未引入 |
| PAE Viewer | 作者论文与 GitLab 提供 PAE、结构、序列联动实现 | 参考其交互设计；当前用项目自身热图，不复制未核实许可的源码 |

来源：[3Dmol 官方仓库](https://github.com/3dmol/3Dmol.js)、[3Dmol 许可](https://github.com/3dmol/3Dmol.js/blob/master/LICENSE)、[Mol* 官方仓库](https://github.com/molstar/molstar)、[Mol* 许可](https://github.com/molstar/molstar/blob/master/LICENSE)、[PAE Viewer 作者论文](https://academic.oup.com/nar/article/51/W1/W404/7151339)、[PAE Viewer 作者代码](https://gitlab.gwdg.de/general-microbiology/pae-viewer)。PAE Viewer 网页本次抓取超时，GitLab 页面可访问但未取得完整许可证文本，因此不填写一个猜测的许可证。

可视化验收围绕科学语义：有 pLDDT 才显示 pLDDT 着色；实验结构 PDB 的 B-factor 不自动改名为 pLDDT；没有 PAE 时明确显示“该结果未提供 PAE”，不能生成看起来合理的矩阵。PAE 轴按相应引擎的 residue/token 顺序定义，不能将非对称矩阵强行对称化。交互既应能帮助定位低置信区，也应保留真实数值供下载复查。

## 示例数据如何追溯

项目包含 RCSB PDB 的 **1UBQ**（ubiquitin）与 **1CRN**（crambin）实验结构用于回放，以及 AlphaFold DB **P0CG47** 的公开预测结构和 PAE 示例。前两者没有模型预测置信度；后者元数据记录为 `AF-P0CG47-F1`、文件版本 v6，并附本地保存的原始元数据。数据库条目版本不是本机执行模型版本。来源：[1UBQ](https://www.rcsb.org/structure/1UBQ)、[1CRN](https://www.rcsb.org/structure/1CRN)、[AlphaFold DB P0CG47](https://alphafold.ebi.ac.uk/entry/P0CG47)。

对真实任务，建议把序列哈希、输入 JSON、引擎版本、权重标识、数据库版本、seed/sample/recycle、设备型号、执行日志和结构文件放在同一个任务目录。某字段未由外部程序提供时应保持缺省，不能从页面上选择的模型名字反推出实际参数版本。

## 接下来怎样选新增模型

先在代表性蛋白长度、链数与复合物类别上做同一输入集测试，保存所有模型的原始输出。先检查能否正确解析结构、链编号和置信度，再比较合格结构吞吐、GPU 峰值显存、队列等待与失败率。研究质量需另设适合任务的结构或实验评价，不把高 pLDDT 自动等同于正确结合界面。

优先级为：现有团队已经批准并跑通的引擎接入；同源输入的 AF2/ColabFold 可复现实验；复合物需求下评估合规可用的 AF3 或 Boltz；需要改模型训练或内核时再引入 OpenFold/Protenix。这样每新增一个引擎都有清楚的业务用途、输入输出契约和实测数据，而非只增加一个模型下拉框。

## 本地数据库与阶段拆分的实现映射

新增数据库注册与预检、持久化 campaign、独立 CPU 准备和验证池、有界 GPU 池。AF3 使用官方 data-pipeline/inference 开关拆分，ColabFold 使用本地 `colabfold_search`→A3M→`colabfold_batch`；AF2 仍使用原完整任务 runner。具体库版本、索引清单、缓存语义及十万条导入方式见 [数据库与批次管线](database-and-batch-pipeline.md)。AF3 已加入[固定链跨复合物准备复用](chain-reuse-design.md)与[常驻节点协议](hybrid-resident-pipeline.md)，两项分别减少重复搜索与模型初始化。云资源创建、实机部署与 GPU 性能验收未执行。
