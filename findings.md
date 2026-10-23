# 本轮技术依据与决定

- 现有 campaign 账本支持阶段、租约、attempt fencing、准备缓存和独立结果文件；预测仍每目标启动 subprocess。
- 已有本地数据包与输出路径不能直接供阿里云读取，需要真正传输、校验和独立发布协议。
- 现有HTTP服务只允许本地同源；远程worker数据面需独立认证端点，默认保持关闭。
- 已核查 AF3 v3.0.4：ModelRunner.model_params 与 _model 是 cached_property，可持有同一实例跨 process_fold_input 目标复用。来源：https://github.com/google-deepmind/alphafold3/blob/v3.0.4/run_alphafold.py
- ColabFold 1.6.3 batch.run 会加载模型并按目标重设 MSA 维度，不能用进程驻留或简单 CLI 包装冒称模型常驻。本轮 native resident 先支持 AF3，CF 保留旧 staged。
- 官方阿里云 ECS spot 文档说明回收通知后约5分钟释放；具体实例事件还需部署环境接入。来源：https://www.alibabacloud.com/help/en/ecs/spot-instance （2026-10-23查阅）。
- OSS 的 ETag 不总是对象 MD5；对象内容的端到端 SHA256 与 OSS CRC64 各自记录，不能把 multipart ETag 当内容校验。来源：https://www.alibabacloud.com/help/en/oss/user-guide/data-verification/
- 模式选项：legacy 保留原流程；workers 关闭本地逐目标预测线程，交由实际 worker 拉批次，CPU 准备与验证继续运行。控制节点 AF3 只需准备命令，模型权重/预测runtime留在worker侧。
- Agent 跨模块复核修正：OOM正常返回也需要显式重启broken resident；未执行/设备错误的retryable项等待租约回收，不应当作永久科学输入失败。
- 大结果默认以文件指针返回；批次聚合列表不再保留所有PAE。节点上传按目标即时进行，控制端重新解析结构而不信任节点绝对路径。
- 8771独立临时环境已启动，实际两个resident进程通过HTTP下载/上传ZIP，无付费云连接、无新GPU预测；正在验收24目标与后续故障注入。
- 首次实际联调：24/24目标交付，两个位置标签节点各处理12目标，共8个批次/48条传输；各自resident PID跨批次不变，后端初始化1次、真实模型加载0次（reference）。

新一轮核查：cluster只有5张流程卡和近期账本，没有全局排队态势/领取阻塞原因，batch成员未可视化。campaign只允许completed目标查看结构，其他状态缺乏详情入口。按这两个实际缺口改进。

工程审查确认并安排修复：不可路由ready占满全局64缓冲造成可运行队列饥饿；成功节点target目录永久保留造成磁盘随总量增长；实际OOM目标与未开始目标都标retryable造成无限重启。修改分别由B和D承担，界面显示真实全局账本/路由状态。

2026-10-23 管线打磨：核对 AF3 v3.0.4 官方 performance 与 pipeline.py，支持按链复用原始 unpairedMsa/pairedMsa/templates；pairedMsa 未完成复合物配对，物种header/顺序须逐字保留，完整目标仍重新特征化。固定T×N候选只把朴素2N链搜索降为N+1搜索身份，不减少N个完整推理，也不据此捏造GPU加速比例。

持续迭代依据：AF3 v3.0.4 performance.md 明确4个遗传库搜索并行，CPU核心配置为每个Jackhmmer线程数×4，深MSA可超过建议64GB RAM；因此ready任务数量水位无法替代CPU/内存/磁盘准入。资源模块使用管理员估计值进行领取前预留，不做未知硬件/占用猜测。来源：https://github.com/google-deepmind/alphafold3/blob/v3.0.4/docs/performance.md#data-pipeline （2026-10-23复核）。

缓存生命周期依据：源码确认prepare原子发布到ready登记之间有缺口；legacy复制/远端ZIP需覆盖整个共享文件读取。全局合作锁按maintenance→whole→leaf→短SQLite顺序。scan引用使用新prepared_cache_key及紧凑chainReuse，旧引用有界读取；completed结果指针/结构未确认继续pin。独立并发审查证明仅串行HTTP job不足以保护CLI并发，修为same-plan跨进程锁+每次独立refs+EX内完成收据；operation flock存活探测避免PID复用和重启永久busy。1000共享引用/大input_json测试验证只读紧凑列且Python峰值小于8MiB，这不是百万任务推理性能。后续优先核查大结果的紧凑发布索引：当前>4MiB结果JSON保守pin准备缓存，长蛋白PAE可能触及这一限制；应从可信发布事务提供小型结构指针，避免为了GC读取大矩阵。catalog历史/操作锁文件当前保留，后续需有独立可审计的元数据保留策略。

大结果核查：parsers先json.loads整份confidence才做2000-token限制，engine.models列表保留全部sample大对象，pipeline又candidate/result/deepcopy重复保存；256px图并未限制网络负载。计划使用官方ijson 3.6.0事件流（PyPI 2026-10-12 release），先轻量排名后仅top矩阵，float64 LE侧文件保留原JSON下载。项目隔离.venv已安装ijson3.6.0/yajl2_c、Python3.12.14，不改系统环境。

结果方案已实装：4KiB publication同completed事务；大PAE独立float64+SHA/source metadata；真实API4/worker传输去重1过。独立review先复现修复ijson容器/点号key前缀误读、小数tokenID截断与stat-open文件替换竞态。浏览器mock13、临时真实HTTP6、旧详情12过；截图左侧sticky sidebar取景问题仅修测试先scrollTop再截图。2048²合成77,134,000B JSON以~30MB进程RSS处理，逐值校验通过（非GPU）。最后审查避免resident先索引再被controller重复索引：worker将仅输出原生文件+轻量排名，控制端独立建制品；AF3第一遍有可用summary时避免全量置信度流扫。

下一轮高价值缺口候选：批量列表/metadata export目前只有阶段/长度/耗时，不含可排序科学评分。十万候选完成后不应打开十万详情找结果；应核查publication时的紧凑科学摘要与SQL分页筛选/排序，并把rankingScore/meanPlddt/iptm/ptm和模型/排名指标身份一起保留，禁止跨量纲混排。当前轮先完成PAE与发布验收，不扩展该功能。

## 2026-10-23 MSA供料与质量观察

共享固定链的阻塞锁曾占住准备worker及资源reservation。新增cooperative路径先做未锁链，全部依赖忙时持久化checkpoint、CAS暂缓并释放预算；原直连prepare_stage保留阻塞合同。搜索前pending+whole意图供GC保护；leaf生产item身份恢复发布后崩溃的计数。QC用原始A3M精确UTF8身份、raw/hit行数、coverage nearest-rank、gap/insert摘要，不改序列或最终配对。真实HTTP证明共享AG持有时另一个候选与FT继续完成，最后4搜索+1命中；无原生搜索速度结论。


## MMseqs2搜索本身加速：初步核查

官方ColabFold搜索实现已有GPU及AF3 JSON输出选项；AF3自定义MSA要求both unpairedMsa/pairedMsa均非null，A3M query首行与长度一致。CF复合物人工配对行位属于完整目标，不直接写入可独立复用链paired缓存。采用MMseqs未配对搜索与CPU原生UniProt候选/模板可选完成阶段；GPU只锁搜索段。现有engine GPU租约进程内，resident无跨process互斥，需同节点共用锁，不能只设CUDA可见性。资料来源以官方固定tag和研究论文就近引用，真实吞吐需部署后测量。

## MMseqs 迭代最终发现

ColabFold v1.6.3与AF3 v3.0.4固定接口；MMseqs latest页面2026-10-23指向18-8cc5c。CPU/GPU搜索只替换unpaired来源，nativepaired去重false及模板映射/日期经独立官方源码检查。GPUsearch仅第一phase租专组，finalize CPU。外部gpuserver占显存跨请求，不能被query lease覆盖，本轮明确拒绝；真实多query聚合/服务托管未实现。入口曾被globalprepare能力误禁用，server按profile检查并向registry API附campaignCapability后UI可选真实配置。进程FD只继承到协作层，MMseqs孙进程由managed原进程组回收；不能把FD说成跨所有工具自动传递。

## 前端UX审核发现（2026-10-23）

输入草稿和异步结果必须按用户最后意图保留；仅保留DOM不足以保证参数不变。预测提交成功应先落本地显示状态，不能被后续GET错误降格为失败。批次筛选身份包含campaign/stage/offset；队列表格提交不应依赖详情GET成功。科学视图必须按残基身份使用明确pLDDT，不假定所有结构B列都代表置信度。完整问题、修复和测试记录见docs/frontend-ux-review.md。
