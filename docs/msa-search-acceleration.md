# MSA 搜索加速：MMseqs2 CPU、GPU 与 AF3 的接法

核查日期：2026-10-23。本文给技术选型、吞吐推理和验证顺序；配置、CLI、原生 API 与制品字段见[适配器实现指南](mmseqs-af3-search.md)。当前部署继续搁置。

## 先决定替换哪一段

本轮新增的是实际搜索后端。原生 AF3 路线保留，另有 MMseqs2 CPU / direct GPU 路线：ColabFold 搜索 UniRef30 与环境库，产出单链未配对 MSA；搜索设备释放后，由 CPU 按配置补齐原生 UniProt 配对候选与 AF3 模板。输出进入既有独立链缓存、完整目标组装和常驻推理。

| 路线 | 本轮状态 | 适用判断 |
|---|---|---|
| 原生 AF3 HMMER 数据准备 | 保留既有执行路径 | 作为原生流程对照，或经测试其结果更适合目标分布 |
| MMseqs2 / ColabFold CPU | 已接实际两段适配器 | CPU 与内存/I/O 充足，希望先避免占用结构预测 GPU |
| MMseqs2 / ColabFold direct GPU | 已接实际两段适配器及设备互斥 | 新链搜索是供料瓶颈，且能安排兼容的独立搜索 GPU |
| MMseqs2 `gpuserver` | 行业可选方案；当前适配器明确拒绝 `gpuServer=true` | 数据库常驻 GPU，可减少反复载入；需要独立托管服务生命周期、设备角色和请求并发 |
| NVIDIA MSA-Search NIM | 行业服务方案；本轮没有伪造 HTTP 接入 | 需要容器化 API、服务扩容和统一观测时评估，仍需转换并验证 AF3 配对/模板语义 |

MMseqs2 本身支持 GPU 搜索；ColabFold 固定 v1.6.3 已提供本地 GPU 参数和 AF3 JSON 导出。本项目采用独立链准备，以复用固定靶标；ColabFold 的复合物人工配对输出不直接成为独立链缓存。[MMseqs2 官方说明](https://github.com/soedinglab/MMseqs2)、[ColabFold v1.6.3 搜索实现](https://github.com/sokrypton/ColabFold/blob/v1.6.3/colabfold/mmseqs/search.py)

NVIDIA 当前 MSA-Search NIM 的公开接口覆盖单链搜索、复合物配对和模板检索，并另有标为 experimental 的 batch API。服务的返回值仍需按 AF3 的 A3M、配对方法、mmCIF 及索引要求适配，不能把“能返回 MSA”直接当作“所有 AF3 输入已兼容”。[官方概览](https://docs.nvidia.com/nim/bionemo/msa-search/latest/overview.html)、[批处理接口](https://docs.nvidia.com/nim/bionemo/msa-search/latest/batching.html)

## 公开性能证据怎样使用

2025 年 Nature Methods 的 MMseqs2-GPU 论文提供了两条直接影响选型的结果：在约 3,000 万序列库、6,370 条 query 的同源检索中，双路 AMD EPYC 7742（共 128 物理核）上的 CPU k-mer 路线比单张 NVIDIA L40S 快约 2.2 倍；使用 8 张 GPU 后，GPU 路线比 CPU 快约 2.4 倍。该基础系统还有 1TB DDR4 内存和 2TB NVMe。另在 20 个 CASP14 目标的 ColabFold 对照中，GPU 路线的 MSA 阶段较 CPU 路线加速 5.4 倍，整条结构预测加速 1.65 倍。该结构预测对照使用 ColabFold v1.5.5 / MMseqs2 commit 22115b、单张 L40S、5 个模型与 3 次 recycle，关闭模板搜索和 relaxation；两路使用相同的 UniRef30 2021_03 / ColabFoldDB 2021_08。两种测试范围、数据库和批量不同，说明搜索与端到端收益必须分开测。[原论文 Fig.2、Methods 与补充数据](https://www.nature.com/articles/s41592-025-02819-8)

这些是论文的公开测量，不是本项目的 GPU 基准，也不是 AF3 的百万级完工时间。它们支持“CPU 也应参与比较”“GPU 的收益受批量和数据库布局影响”两个判断。直接把某个检索工具倍率乘到 AF3 推理吞吐，会跳过配对候选、模板、特征化、结构预测和 I/O 的实际占比。

本轮代码采用单个独立链的直接搜索，沿用共享链去重与批量账本调度。它没有将所有独立链合成一次 ColabFold 多 query 搜索，也没有常驻 MMseqs 数据库进程。真实大批量验收要把数据库冷/热、每次启动开销和链完成率一起记录；后续多 query 聚合及独立常驻服务是可量化比较的演进路径。

## CPU 版怎样快起来

先去掉重复工作，再提高有效并发。固定靶标 A 与多个候选组合时，A 的搜索只做一次；采样 seed、recycles 改变不重复链准备。CPU 搜索用 ColabFold 的 UniRef30 代表序列搜索、扩展和重对齐路径，环境库按配置加入。单链搜索结果按同一数据库/参数身份复用，保留完整 A3M 与 header。

配置 `threads`、准备 worker 数量与 CPU 预算需要一起调整。提高并发后，观察每小时完成独立链数、单链分位耗时、内存峰值和数据库读带宽；磁盘饱和时继续增加线程会使更多任务互相等待。当前预算是管理员估计准入，真实 CPU/RSS/I/O 需在部署机器上采集。数据库使用已有本地文件，不在每个 query 中下载。[ColabFold v1.6.3 本地数据库构建逻辑](https://github.com/sokrypton/ColabFold/blob/v1.6.3/setup_databases.sh)

## GPU 版的数据库与参数

GPU 加速的重点是序列预筛选和对齐计算，数据读取、数据库传输、MSA 构建及后续 AF3 处理仍占时间。通用 MMseqs 搜索可以用 `makepaddedseqdb` 构建 GPU 友好数据库，但 ColabFold 的迭代搜索还需要可扩展的代表序列、成员和 alignment 数据族。只把 AF3 原生 FASTA 改名为一个 MMseqs prefix 不够。当前 registry 要求显式声明 expandable 数据族成员及 CPU/GPU 格式；这是预检，真实文件语义仍由安装后的小样本搜索验证。[MMseqs2 GPU 官方入口](https://github.com/soedinglab/MMseqs2/wiki#gpu-accelerated-search)、[ColabFold 数据库脚本](https://github.com/sokrypton/ColabFold/blob/v1.6.3/setup_databases.sh)

GPU profile 固定使用 `--gpu 1`、`--gpu-server 0`、`--prefilter-mode 1`，不接收被 GPU 路线忽略的灵敏度 `-s`。设备由调度器设置 `CUDA_VISIBLE_DEVICES`，没有设备分配时拒绝隐式使用 GPU 0。工具版本、数据库格式/版本、搜索参数、配对/模板策略和两段命令均进入缓存身份。

当前官方 MMseqs2 `latest` 页面指向 Release 18-8cc5c；适配器代码接口固定核对 ColabFold v1.6.3 和 AF3 v3.0.4。实际机器版本仍由部署时记录与验证，这些文档版本不表示本机已安装。[MMseqs2 发布页](https://github.com/soedinglab/MMseqs2/releases/tag/18-8cc5c)

## AF3 兼容性的具体处理

1. 每次独立链搜索输入一条标准蛋白 query，输出 A3M 的第一条必须与 query 精确一致；小写插入去除后，各行宽度须等于 query 长度。原始换行和 header 保留，QC 记录原文 UTF-8 SHA。
2. MMseqs/环境库结果进入 `unpairedMsa`。`pairedMsa` 默认由 AF3 原生 UniProt 搜索产生，保留候选重复及原始 UniProt 标识；最终完整复合物特征化时再跨链配对。
3. `templates` 默认由 AF3 原生模板逻辑从新的未配对 MSA 建 profile、搜索并过滤；输出单链 mmCIF 和成对的零基 query/template 索引，遵守配置日期。
4. 配对或模板选择 `none` 时写明示空串/空列表，并在配置和界面标记输入消融；不默默将缺失值变成科学策略。
5. 改变 MSA 来源可能改变模板命中及结构质量。本轮保证输入语义、接口和来源可追溯；研究质量采用固定 seed/采样设置的成对对照验收。

AF3 同时要求两个 MSA 字段明确设置或同时留空。该适配器最终写出自包含输入，因此 GPU worker 使用已有准备制品进行特征化和预测；不会在结构预测时再次启动搜索。[AF3 v3.0.4 MSA、配对与模板输入说明](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/docs/input.md#multiple-sequence-alignment)

## 本地 16 卡与阿里云怎样安排

先比较“CPU 搜索供料 + 16 卡预测”和“k 张本地卡做搜索 + 16−k 张预测”。不能只看 MSA 速度：如果原来预测已是瓶颈，拿走预测卡可能使整轮更慢；如果大量预测卡在等 MSA，独立搜索 GPU 可能提高整体完成率。已加载模型权重的常驻 worker 会持有设备租约，空闲时也不会被搜索抢占；调整角色应先 drain 对应 worker，再改变明确设备组。

用同一代表性目标集测量：设每个目标平均需要 `r` 条新链准备，单个搜索 GPU 完成率为 `S` 链/秒，CPU 收尾池为 `F` 链/秒，单个预测 GPU 为 `P` 目标/秒。在近似线性扩展且资源互不干扰的条件下，候选配置的上限可写成：

`目标完成率 ≤ min(k×S/r, F/r, (16−k)×P, 传输与结果验证完成率)`。

它是选型算式，不是实测线性扩展结论；多 GPU 的搜索率和缓存命中变化应替换成实际测量。搜索使用额外云端资源时不从本地 16 卡中扣除，但需加入云端库驻留、CPU 收尾与传输限制。当前新搜索执行器在准备控制节点上执行；阿里云 GPU worker 仍消费准备包。远端 MSA 服务属于需要单独 API 与生命周期接入的架构，没有用本轮配置冒充已部署。

就近传输对象应是按目标生成的准备包与结果，而非为每个预测任务搬整套序列库。库保留在准备/搜索节点，内容身份、链缓存和自包含 AF3 JSON 随批次可追溯。

## 为什么当前没有直接启用 gpuserver

MMseqs2 官方提供 `gpuserver`，数据库常驻能减少频繁搜索的加载开销；客户端与服务端的可见 GPU、预筛选方式及相关参数需要一致。[官方 GPU server 说明](https://github.com/soedinglab/MMseqs2/wiki#gpu-accelerated-search)

本项目的 direct GPU 租约覆盖一次搜索，而外部 gpuserver 会在请求结束后继续占显存。正确接法需要：服务进程生命周期持有设备，单独的请求并发限制，数据库/参数签名匹配，以及健康检查与排空。当前代码明确拒绝 `gpuServer=true`；把 direct GPU 后端接稳后，才能在真实条件下比较常驻服务对十万/百万链的收益。NIM 也应按独立服务的资源与接口契约评估，而不是只改一个开关。

## 部署后验收顺序

先比较原生 AF3、MMseqs CPU、MMseqs GPU 三路的同一组单体/同源多聚体/异源复合物/抗体复合物。固定数据库日期、seed、采样和 recycles，记录链长度及组分规模。分别测搜索、UniProt 收尾、模板、CPU 全阶段、GPU 特征化/推理、传输和整轮耗时；冷/热数据库分开。

检查原始深度、覆盖率、配对材料、模板命中与来源，比较 pLDDT/pTM/ipTM/PAE。有实验结构时再加入 TM-score 或 DockQ；置信度本身不替代结构正确性。确定质量可接受后再测并发档位、GPU 角色分配、每小时交付完整目标数和失败恢复成本。每张卡与每个阶段的观测要能解释最终吞吐。

本轮受控测试与真实 HTTP/浏览器验收另见验收报告。真实序列库、MMseqs GPU kernel、AF3 模型与云部署均未启动。


## 本轮验收结果

后端全测 298 发现、297 通过、1 个按需规模测试跳过（十万条账本在前一轮独立通过）；客户端 67 通过。后端包含 12 项适配器、8 项注册/阶段整合及 7 项设备生命周期测试。真实子进程测试覆盖：包装进程硬退出后的后代回收、常驻模型持锁跨 restart，以及父进程退出后子进程仍保留设备锁。

真实 HTTP 创建 CPU/GPU 两种配置绑定的批次，在无全局 AF3 默认准备命令时仍可运行注册命令；5 条独立链准备供给 3 个完整目标，原始 CRLF A3M、重复 UniProt 候选和零基模板映射保留。浏览器新增策略契约 10 项、真实适配器 HTTP 9 项通过，桌面与 390px 布局已检查。现有工作台 5 个批次、2 个任务及 2 个回环参考 worker 保留。

以上执行的数据库成员、搜索程序和 AF3 原生模块均为明确的合成夹具；GPU 可见性/设备租约采用虚构设备标识验证，没有运行 GPU kernel。完整范围、命令与源码哈希见 [总验收](../artifacts/mmseqs-search-verification.json)、[真实 HTTP 事件](../artifacts/mmseqs-flow-http-verification.json)、[浏览器结果](../artifacts/mmseqs-http-browser-verification.json)。

同节点的准备与推理进程须使用相同 `PROTEIN_STUDIO_GPU_LOCK_DIR` 和一致的物理设备标识；推荐使用 GPU UUID，避免容器索引别名指向同一卡却生成不同锁。互斥覆盖参加本项目协议的进程，跨主机调度仍由各节点资源配置负责。
