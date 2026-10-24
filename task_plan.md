# 常驻批量预测与混合云执行 · 2026-10-23

目标：同一 GPU 模型进程跨目标、跨批次复用；本地 16 卡与阿里云 GPU 通过实际节点协议领取批次，校验准备数据传输、逐项结果回收、心跳租约和故障恢复；界面展示真实节点与传输状态。

## 阶段

1. [完成] 核查已有阶段账本及官方 AF3/ColabFold 常驻接口，确定统一协议。
2. [完成] 实现可跨批次复用的 resident runner；原生AF3/受控安装模块与reference进程协议回归通过。
3. [完成] 实现节点注册、兼容性/长度桶批次领取、带校验准备包传输、逐项上传与提交、租约恢复。
4. [完成] 集成调度、API、节点/传输/批次可视化及配置文档。
5. [完成] 验证多批次同进程、部分失败、节点丢失、传输完整性与浏览器实际流转。

## 约定与约束

- 真实模型未安装，不用 fixture 性能冒充 GPU 推理性能；验证实际进程复用、控制面和文件流转。
- 常驻模型实例须真正跨请求复用，不能只驻留一个反复启动模型 CLI 的 wrapper。
- AF3 原生多个目标逐个执行属于批量执行；不冒称模型支持未实现的张量 minibatch 或组内多卡。
- 默认不购买云实例、不创建收费资源；阿里云节点以可部署 worker 接入现有 GPU。
- GPU 拓扑按配置与实际注册分开显示，本地16卡是用户事实，不冒称此Mac有16卡。
- 预备库主要留在CPU搜索侧，节点传输完整的准备产物与校验清单。

## 错误与修复

- SIGSTOP故障脚本首次在信号发送前退出：把批内deliveryState固定当作uploaded，实际API会随验证阶段变化。修正为按members.stage为validating/completed判定已接收，不涉及核心调度失败。

最终验收：后端101通过/1按需skip，客户端23通过，真实HTTP双节点24目标及失联18目标测试通过；live浏览器7组通过。真实GPU/阿里云实例仍需部署验收，fixture不作为性能证据。

## 新一轮：更丰富的可视化与管线操作

目标：把静态的流程卡片升级为真实状态驱动的拓扑与队列态势，提供批次/目标检查与排障，保留常驻和逐项提交的执行机制。

1. [完成] 复核当前服务/UI/API，确定新增态势与交互数据契约。
2. [完成] 后端全局阶段/可领取资格诊断，避免近期窗口冒充总量。
3. [完成] 动态混合执行拓扑、可点击阶段/批次/节点、目标详情与导出。
4. [完成] 实际服务与桌面/手机验收、修复、源码打包。

视觉约定：延续深绿和黄绿，动画反映真实工作状态；空闲显示空闲，规划16卡不冒充在线卡；不捏造利用率、GPU秒或ETA。

最终：新路由/清理代码下再次SIGSTOP恢复18/18，保留已接收1项并重领未提交2项；全测114过/1按需skip、客户端23、parser8；新增live浏览器8与隔离详情12通过。最终截图修正为页面顶部取景，归档排除运行数据与凭据。

## 真实 AF3 与数据库部署（用户明确搁置）

固定版本、配置生成器、启动器、数据库/GPU探针已保存并做局部工具测试。完整部署执行和现场验收搁置；verify_af3_campaign.py为未验收草稿，不宣称部署完成，不继续索取节点入口。

## 用户纠偏：部署搁置，继续打磨管线

- 真实服务器接入、部署执行及完整部署runbook由用户明确搁置，不再继续请求节点入口。已有未部署工具保存为草稿，不宣称实机运行。
- 当前重点：从官方AF3的固定链复用方案改进大规模target×candidate筛选，避免每个组合重新搜索公共链。
1. [完成] 核对独立链准备、paired MSA与template装配语义。
2. [完成] 链级内容寻址缓存、完整性检查、single-flight与原子发布；保留whole-target缓存兼容。
3. [完成] 共享链/新链计数、准备数据复用可视化与真实文件流转验证。
4. [完成] 回归、行业依据、最终应用与归档更新。

最新验证：后端156过/1按需skip，十万项另行过；客户端44过；链UI契约8、真实架构5、reference详情12过。源码包更新，服务8765恢复并保留原数据。

## 持续迭代：筛选输入与准备资源准入

用户要求持续迭代，部署仍搁置。上一轮为实质进展（代码/测试/运行应用均已改变）。本轮从当前源码验证出两项缺口：CLI只读取FASTA，固定靶标仍需手工JSONL；CPU准备只有线程/目标水位，没有CPU/内存/磁盘预算准入。

1. [完成] 固定靶标+候选的流式CLI与JSONL、断点上下文绑定。
2. [完成] 浏览器固定靶标模式、少量预览、刷新/失败后的语义校验。
3. [完成] 管理员估计CPU/内存/磁盘预算、领取前等待、可取消的预留与释放。
4. [完成] 资源可视化、组合回归、当前服务与证据、文档源码归档。

不把预算预留当OS实际利用率，不把候选输入预览当全量推理验收。保留完整目标与独立链缓存、模型常驻、混合分发和逐项恢复。

本轮最终：后端171过/1skip，客户端65，固定输入11、预算UI6、当前服务8、详情12。持续目标保持 active，未宣称部署或GPU验收完成。后续优先核查缓存占用和活动引用生命周期，避免长期筛选后磁盘积累。

## 持续迭代：缓存生命周期

上一轮为实质进展。当前源码核查显示：准备制品没有服务端回收目录或引用目录；prepare原子发布到SQLite ready指针有间隙，预测复制与远端打包无维护锁。直接按目录年龄删除会有竞态。

1. [完成] 协作维护读写锁，覆盖准备→ready提交、预测复制及远端打包；新增缓存键索引。
2. [完成] 有界磁盘catalog扫描、受保护引用、清理计划、可恢复隔离/恢复与显式purge。
3. [完成] 异步API与存储页：显式扫描、分页预览、操作结果与字节口径。
4. [完成] 竞态/文件完整性/真实HTTP与浏览器回归、文档和最终源码归档。

主数据只运行只读扫描，不实际隔离或永久删除。隔离目录仍占用磁盘，逻辑字节不冒充文件系统真正可用量增加。部署继续搁置，持续目标active。

本轮：后端217发现/216过/1按需skip，十万条另行通过；客户端65，parser8；storage mock12/真实临时HTTP浏览器7/实际应用只读6，管线只读8通过。独立审查修复同plan并发引用快照互删和扫描完成时CAS误报interrupted。主服务session62600保留5个campaign和2个旧job；主cache只扫描，2个whole/338697逻辑字节/19节点，无隔离或永久删除。持续目标active，部署搁置。

## 持续迭代：大规模结果发布与按需分析

上一轮是实际进展：代码、运行应用、文件/HTTP/浏览器证据均已改变。当前源码显示validation把完整candidate JSON载入并写result.json，详情直接载入整个结果；GC为保证读取有界，对>4MiB结果保守pin。新一轮围绕结果发布索引、PAE外置及按需可视化，避免大规模结果积累后的CPU/内存/网络瓶颈。

1. [完成] 核对PAE格式、发布事务、结果API和浏览器访问路径，固定兼容契约。
2. [完成] 原子发布紧凑结果/可信结构索引，旧结果安全兼容，GC不读取巨矩阵。
3. [完成] 大PAE外置与有界预览/精确区域读取、原始下载；详情和界面按需加载。
4. [完成] 最终全测253发现252过1skip，十万项另过；客户端65/前端parser8，PAE契约13/真实HTTP6/详情12、当前管线8/存储6过；应用、文档与归档更新。

现有数据不做破坏性迁移；性能测量标明合成数据或元数据。仍不部署GPU/公有云资源，持续目标active。

本轮实际进展完成：GPU worker不建立控制端会重建的矩阵，AF3先读summary排名；253后端发现252过1skip（59.46s），十万账本另过（导入7.677s/末页0.124165s）。合成2048²来源77,134,000B、峰值RSS29,982,720B，逐值精确验证；非GPU性能。实际服务51646运行于项目.venv，原5campaign/2job保留，无实际AF3/云部署。持续目标active；下一轮优先检查大批候选的科学评分筛选/排序，避免逐个打开详情找优选。

## 用户新重点：MSA慢阶段与GPU供料

用户指出MSA通常耗时很久。当前源码确认：CPU/GPU阶段已拆分、按链精确缓存已存在；但_assemble_af3严格按链顺序，多个准备worker会先等待同一固定链，并占住整项资源reservation。准备链质量也没有独立只读摘要。部署继续搁置，本轮优先检查此实际缺口，而非继续扩展结果排序。

1. [完成] 确认共享链等待/资源占位的可复现时序，确定不超配的非阻塞调度契约。
2. [完成] busy依赖让出准备slot、候选链先行、失败/取消/lease与缓存一致性验证。
3. [完成] MSA深度/覆盖等只读质量摘要与实际搜索/缓存/等待可视化。
4. [完成] 合成可控搜索与真实HTTP/浏览器验收、文档和当前应用更新；不冒充native搜索基准。

本轮完成：共享依赖让出准备槽位、搜索前pending引用、发布后硬退出恢复、逐链原始MSA QC和浏览器已接线。后端271发现270过1按需skip，十万项另过；客户端66/66；MSA浏览器契约11/真实HTTP6/实际只读4。主服务98824保留5campaign/2job及2个回环参考worker。部署保持搁置；当前无活动goal对象。


## 用户新重点：MMseqs2 CPU/GPU 搜索加速与 AF3 兼容

部署仍搁置。针对搜索本身提供可运行的管理员后端，继承独立链缓存、让出和 QC。按固定版本官方实现核对，不把CF复合物已配对A3M当AF3独立链paired缓存。

1. [完成] 官方MMseqs2/ColabFold/AF3版本与输入、数据库、配对、模板契约核查。
2. [完成] MMseqs CPU/GPU search → CPU native UniProt/template finalize 两段adapter；注册、身份隔离与精确输入验证。
3. [完成] 同节点跨进程GPU互斥、专用搜索GPU与等待让出；GPU仅搜索阶段占用。
4. [完成] 后端选择与AF3策略可视化、受控真实进程/HTTP/浏览器回归、专题与当前服务/归档更新。

本轮完成：实际CPU/directGPU适配器、保留native UniProt/模板、注册profile入口与cache身份、两phase释放设备、进程组/FD恢复、三处策略与四步可视化。后端298发现297过1按需skip；客户端67；新browser10/真实HTTP9；原MSA11/链复用8；主服务52791保留5campaign2job。gpuserver与远端MSA服务留作明确后续，不冒充已托管。

## 2026-10-23 前端功能与体验检查

1. [完成] 实际浏览与并行审核提交、批次、结果和运维操作。
2. [完成] 13组问题修复：草稿/路由/请求一致性/动作反馈/置信度/重连/队列/翻页/焦点与窄屏。
3. [完成] 新专项38项、既有66项回归；主应用只读，5批次2任务保留；部署继续搁置。
4. [完成] 检查报告与证据归档：docs/frontend-ux-review.md。

## 2026-10-23 腾讯云公开部署与个人网站整合

1. [完成] 定位portfolio-fe及现有腾讯云ubuntu@101.43.97.91、benluo.art HTTPS代理。
2. [完成] 可选/foldspace前缀、干净公开只读数据初始化、容器依赖和内存限制。
3. [完成] 腾讯云独立部署，健康/结构/PAE/只读门禁验收。
4. [完成] portfolio中英项目、预览与线上入口，构建/浏览验证后上线；保留已有未提交改动。

## 2026-10-24 持续迭代：批次置信度评分排序与筛选

上轮已明确"下一轮优先检查大批候选的科学评分筛选/排序，避免逐个打开详情找优选"。本轮先核对业界口径：AF3 官方 ranking_score（越高越好，ranked by 官方口径）、AF-Multimer 0.8·ipTM+0.2·pTM、EBI ipTM ≥0.8 高置信 / 0.6–0.8 灰区 / <0.6 可能失败、平均 pLDDT 0–100（官方 output 文档 + EBI 培训 + ColabFold issue 两个独立来源确认）。

1. [完成] 账本 score_* 列（ranking/iptm/ptm/plddt + ranking_metric + score_checked）与迁移；交付时在唯一完成路径提取分数写入。
2. [完成] 旧数据惰性一次性回填：有界 4MiB 读取 compact result，CAS 防重复；retry_failed 清除陈旧分数。
3. [完成] items API `sort=ranking/iptm/ptm/plddt` 降序（NULL 最后）与 `min_*` 阈值白名单校验；export 契约不变。
4. [完成] 批次页"置信度"列：ranking 加粗 + ipTM/pTM/pLDDT 详情 + EBI 分带徽标；排序下拉与 ipTM 阈值快捷筛选（仅有分数时出现）；浏览器契约验证。

本轮：新增后端 test_campaign_scores 8 项通过；全后端 320 发现 OK（1 按需 skip，73.1s）；根 tests 67/67；客户端 campaign_client 通过；分数浏览器契约 6/6、回归 browser_campaign 9/9（campaign f25c8d490322492d9d51e7c847904240，artifacts/campaign-scores-verification.json + 截图）。参考回放口径如实标注：无 ipTM，浏览器验证 pLDDT 排序与 API↔DOM 一致；ranking 与 pLDDT 顺序一致为回放口径巧合不作依据。截图模型级目检因工具超时跳过，DOM 断言已覆盖契约。无 GPU/云部署；演示服务用后即停。下一轮候选：export JSONL 带分数、分带统计汇总。

## 2026-10-24 持续迭代：筛选结果导出携带置信度分数

上轮遗留候选落地。先核对业界惯例（ColabFold batch 的 ranking_debug.txt/scores.json、Boltz 的 confidence.json、AF3 Server ranking_score，两个独立来源）：批量结果的机器可读导出必须携带置信度分数，筛选工作流要能导出选中的子集。当前 export JSONL 只有 metadata 与结构链接，不带分数。

1. [完成] campaigns.py 抽出 parse_score_query 共享校验，items 与 export 同一排序/阈值口径（含非法输入 400）。
2. [完成] backfill_scores 分页回填：seq keyset 前向扫描、干净账本单次计数即出、每页独立短事务；导出快照前驱动 legacy 行写一次补分。
3. [完成] export SELECT 增 score_* 列，行内带 resultScores；支持 sort/min_* 导出筛选后的子集，快照一致性契约不变。
4. [完成] 前端已交付导出链接跟随当前排序与阈值（所见即所得），提示文案标注分数与跟随行为；README 接口行更新。

本轮：后端 test_campaign_scores 增 2 项（分页回填、导出路由排序/筛选/legacy 回填/400）共 10 项通过；全后端 322 发现 OK（1 按需 skip，73.9s）；根 tests 67/67；客户端通过；分数浏览器契约 8/8（新增导出顺序一致 + resultScores + WYSIWYG 链接检查，campaign 5aeee499b0b94cf584b39294311d63a7）、browser_campaign 回归 9/9。导出仍为元数据级一致快照，不含坐标/PAE；参考回放口径同前。演示服务 8973 用后即停。下一轮候选：批次分带统计汇总（高置信/灰区/低置信计数）或目标详情分数趋势对比。

## 2026-10-24 持续迭代：批次置信度分带汇总

上轮候选落地。先核对分带口径（EBI AlphaFold 培训 + AlphaFold DB FAQ 独立两源）：pLDDT 四带 ≥90 很高 / 70–90 置信 / 50–70 低 / <50 很低（AF DB 默认着色带），ipTM 沿用 EBI 界面三带 ≥0.8 / 0.6–0.8 / <0.6。筛选场景需要批次级一眼可见的分布，而不是逐项打开。

1. [完成] get() 分带聚合并入既有 scored 计数单条查询（SUM 条件计数，NULL 自然排除），metrics 增 scoreBands（ipTM 三带 + pLDDT 四带）。
2. [完成] 前端批次面板交付进度条下新增"置信度分布"行：复用分带徽标配色，无分数段自动隐藏；tooltip 标注口径与 legacy 行"首次列出/导出后计入"的边界。
3. [完成] 后端分带计数测试 + 浏览器分布行一致性检查（含无 ipTM 时隐藏该段）。
4. [完成] README 批次管线能力段补分带汇总、排序筛选与跟随筛选导出描述。

本轮：test_campaign_scores 11 项通过（+1 分带）；全后端 323 发现 OK（1 按需 skip，72.1s）；根 tests 67/67；客户端通过；分数浏览器契约 9/9（+1 分布行，campaign 632f13fa4bbb44daa771a55c3df01741）、browser_campaign 回归 9/9。分带为账本聚合，不代表 GPU 吞吐；未评分交付项不属于任何带。演示服务用后即停。下一轮候选：目标详情页置信度分数与逐残基曲线联动摘要，或批次跨样本分数对比视图。

## 2026-10-24 持续迭代：目标详情置信度评分区

上轮候选落地。核对 AF3 Server 结果页惯例（EBI 培训"Interpreting results from AlphaFold Server"+ alphafoldserver.com 两源）：结果页以 ranking_score（源自 ipTM/pTM）排名，ipTM>0.8 高置信、0.6–0.8 灰区，配合 PAE 深查。详情 API 已带 resultScores（迭代 #1 的 _public_item），但检查器面板未渲染——列表有分、详情无分的断点。

1. [完成] renderCampaignInspectScores：排名分（加粗、rankingMetric 口径副注）+ ipTM/pTM/平均 pLDDT，EBI ipTM 三带与 AF DB pLDDT 四带徽标，术语 tooltip 标注口径与"预测置信元数据，不代表实验测定精度"。
2. [完成] 检查器动作区后插入评分区，无分数目标不渲染该段；CSS 复用 facts 布局与分带徽标配色。
3. [完成] 后端 detail 断言补强（store.item 含 resultScores）；浏览器契约新增打开检查器断言评分区存在；README 目标检查描述更新。

本轮：test_campaign_scores 11 项通过；全后端 323 发现 OK（1 按需 skip，73.9s）；根 tests 67/67；客户端通过；分数浏览器契约 10/10（+1 检查器评分区）、browser_campaign 回归 9/9。纯前端渲染 + 既有 API，无后端行为变更；参考回放口径同前。演示服务用后即停。下一轮候选：跨样本/跨候选分数对比视图，或批次交付物对比页。

## 2026-10-24 持续迭代：置信度分布条

上轮候选落地。依据：CASP Prediction Center 用动态图评估预测、模型集分数分布直方图是筛选惯例（Eramian 2006 等）、AF pLDDT 四带着色为公认口径。分布条让"从分布选阈值"一眼可做。按 dataviz skill 全流程执行：形式选择 → 配色按职能分配 → 验证器实测 → mark 规格 → 图例与可达性。

1. [完成] 原分带徽标配色跑 validate_palette 实测不合格（琥珀/橙 ΔE 6.6，正常视觉都难分）——改验证通过的有序色板绿/金/橙/深红（亮度带、彩度地板、CVD、正常地板全过；金色对比度 WARN 由图例文字计数 relief），产品徽标保持原配色不变。
2. [完成] ipTM / pLDDT 各一条 100% 堆叠分布条：段宽按带计数、2px 表面间隙、外端圆角、逐段 title tooltip、aria-label 汇总；带色板 swatch + 标签 + 计数图例行，identity 不靠色 alone。
3. [完成] 浏览器契约 +1：分布条段数与图例 swatch 数同步断言；README 分带描述补分布条。

本轮：分数浏览器契约 11/11（+1 分布条同步）、browser_campaign 回归 9/9、根 tests 67/67、客户端通过、全后端 323 发现 OK（1 按需 skip，72.4s；本轮纯前端）。分布为账本聚合非 GPU 吞吐；截图归档 artifacts/campaign-scores.png，模型级目检工具不可用（本地文件不支持），几何/同步由 DOM 断言覆盖。演示服务用后即停。下一轮候选：PAE 快照进入批次交付物、或跨批次（campaigns 列表）汇总。

## 2026-10-24 持续迭代：批次概览列表

上轮候选落地。依据 Seqera Platform/Tower 等 run 监控惯例（runs 列表显示状态、任务计数、进度，实时刷新）：多批次并存时选择器下拉不足以比较，需要概览列表。列表 API（list→get 全量）已含 counts 与 scoreBands，零后端改动。

1. [完成] ≥2 批次时显示批次概览表：名称（点击切换）· 状态 · 目标 · 已交付(%) · 失败 · ipTM/pLDDT 迷你分布条（tooltip 携带带名与计数）。
2. [完成] campaignBandEntries 单一构造器被面板分布与概览共用，分带口径同源不漂移；焦点恢复覆盖 data-campaign-pick。
3. [完成] 浏览器契约 +1：创建第二批次后表行数=2，点击概览行切换选择。
4. [完成] 修复迭代 #1 HTTP 测试竞态：CampaignScoreHTTPTests 曾 app.start() 启动真实管线 worker，与手工 store.claim 竞争领取（两次在负载下现形为 claim→None）；该类只走 GET 路由，去掉 start() 根治，套件重跑全绿。

本轮：分数浏览器契约 12/12（+1 概览）、browser_campaign 回归 9/9、根 tests 67/67、客户端通过、全后端 323 发现 OK（1 按需 skip，修复后复跑 74.0s；中途一轮 1 error 为上述测试竞态，已根治并复验）。概览为账本聚合轮询数据。演示服务用后即停。另：应用户要求，此前公开演示与置信度工作已按功能分两个英文 commit（ccef98c、bc9b49f）入库。

## 2026-10-24 持续迭代：排名模型切换（三维视图）

置信度筛查线（迭代 #1–#6）已闭环，本轮转向三维视图。依据两源：AlphaFold Server / EMBL 培训（每任务 5 个采样，结果页默认展示排名最高样本，全部采样与各自数据可查看、按 ranking_score 排名）；ColabFold 官方仓库与 AlphaFold2 notebook（ranked_0..4 按 pLDDT/pTM 排名，结构文件 B 因子列即逐残基 pLDDT，每个模型有独立置信度曲线）。检查发现后端已全程携带 rankedModels（rank/name/structureUrl/meanPlddt/ptm/iptm/rankingScore，artifact URL 已注册），但三维视图永远只加载主结构——采样"可下载不可查看"的断点。

1. [完成] viewer-tabline 排名选择器：≥2 采样时列出 排名·样本名·pLDDT·排名分，默认排名 1；切换经 renderMain 复用既有 viewer teardown 干净重建；下载链接跟随当前采样；scope 注明置信度曲线/PAE 仍对应排名 1 主结果。
2. [完成] 非主排名按结构文件 B 因子（即 pLDDT，AF2/ColabFold 约定）以共享四带色板逐残基着色；文件无可用 B 因子（全 0/缺失）回退按链着色，不虚构置信度。
3. [完成] 模型与结果制品表新增"排名分"列（AF3 Server 逐采样展示 ranking score 的惯例）与逐行"三维查看/当前视图"动作；modelIndex 按任务 id 重置，跨任务不残留。
4. [完成] 测试：新增 browser_ranked_models.cjs 契约（模拟多采样 AF3 任务 + 单模型参考任务，6 项：选择器标签与默认值、切换重载几何与下载链接、表格动作切回、单模型无选择器、跨任务重置）；置信度着色 vm 回归 +2（B 因子四带着色、全 0 回退按链）。后端零改动（结构 URL 与分数均已存在）。

本轮：语法检查 8 文件、排名模型契约 6/6、着色 vm 6/6、workspace-ux 离线 5 套件、隔离 8973 分数契约 12/12、批次回归 9/9、冒烟 12 检查（8974，需 CHROME_PATH 指向完整 Chrome，headless-shell 未安装）、根 tests OK、客户端通过、全后端 323 OK（1 按需 skip，72.9s）。workspace_ux_live 本轮未跑：其设计目标是常驻主服务（workers 模式 + 已有完成示例），隔离冷起服务不满足前置（自动种子在无 reference worker 时无法完成），历史绿记录对未改动面仍有效，改动面已由冒烟与离线套件覆盖。模拟 API 契约验证 UI 行为，非新预测或 GPU 性能声明。演示服务用后即停。下一轮候选：PAE 按采样快照、跨采样分数对比视图。

## 2026-10-24 持续迭代：结果制品打包下载

上轮候选落地（交付惯例）。依据两源：AlphaFold Server / EBI 结果页把全部建模结果（各采样 mmCIF + 置信度数据）打包成单个 zip 下载；ColabFold 任务完成自动下载 result.zip（ranked 结构/图/分数）。检查发现单任务制品已逐个可下（结构、PAE、制品表逐文件链接），但用户想离线带走时需逐个点击——补单档案交付。

1. [完成] GET /api/jobs/{id}/archive：复用制品注册表（_register_artifacts 的 registry，排除 __structure__ 路由标记），zipfile 打包为 data_dir 下 tempfile 一致快照后流式发送（沿用 campaign export 的 snapshot-then-stream 模式）；仅 completed（409）、未知任务 404、逐成员仍经 artifact() 做路径解析校验，响应带 attachment job-{id}.zip 与快照 scope 头。
2. [完成] 模型与结果制品面板标题新增"下载全部 zip"：已完成且有制品的任务显示，无制品任务（参考回放单结构）不出现；点击即单次下载整包。
3. [完成] 测试：后端 test_api +2（zip 字节级等于原制品、标记排除；进行中 409 / 未知 404）；浏览器契约 +1（下载事件指向 archive 端点；无制品任务无链接）。README API 表、交付段与测试计数同步。

本轮：语法检查、排名模型契约 7/7（+1 归档）、着色 vm 6/6、workspace-ux 离线 5 套件、隔离 8973 分数契约 12/12、批次回归 9/9、归档真实回放（参考回放任务 completed → 8 成员 zip：P0CG47.pdb、P0CG47_pae.json、.result-artifacts/ PAE 缓存 ×3、input/ ×3；testzip None；application/zip + attachment 头实测）、冒烟 12 检查（8974）、根 tests OK、客户端通过、全后端 325 OK（1 按需 skip，74.9s）。归档为已注册制品的文件快照打包，不代表 GPU 预测吞吐。演示服务用后即停。下一轮候选：PAE 按采样快照、跨采样分数对比视图、任务列表置信度列。

## 2026-10-24 持续迭代：任务列表置信度列

上轮候选落地。依据两源：SWISS-MODEL 结果表每模型带质量分列（GMQE / QMEANDisCo，帮助文档 + Waterhouse 2018 NAR 论文）；AF3 Server 历史列表与结果按置信 ranking_score 排列（EBI Training）。检查发现 /api/jobs 列表摘要在剔除大数组时本就保留 meanPlddt/iptm 标量——"逐个打开任务才能看分数"的扫描断点，零后端改动可补。

1. [完成] 任务表（全部实验 + 最近任务同构造器）新增"置信度"列：已完成任务显示平均 pLDDT（AF DB 四带徽标）与 ipTM（EBI 阈值徽标，复用 campaigns.js 的 campaignScoreBand 全局，同源不漂移）；未评分 / 未完成任务显示占位符，不虚构；tooltip 注明口径与"多采样任务为排名 1 主结果"边界；空行 colspan 同步 6→7。
2. [完成] 存储测试扩展：store.list 摘要保留置信度标量断言（数组仍剔除）。
3. [完成] 浏览器契约 +1：任务列表多采样任务 pLDDT+ipTM 双徽标、参考任务单 pLDDT 徽标、排队任务占位符且无徽标。
4. [完成] README 工作流第 6 项与 test:model-ui 计数（7→8 项）同步。

本轮：语法检查、排名模型契约 8/8（+1 列表置信度）、着色 vm 6/6、workspace-ux 离线 5 套件、隔离 8973 分数契约 12/12、批次回归 9/9、冒烟 12 检查（8974）、根 tests OK、客户端通过、全后端 325 OK（1 按需 skip，76.4s）。真实 HTTP 复验：参考回放完成后 /api/jobs 列表已完成任务均携带 meanPlddt 标量（实测 93.4）；链中该档首跑因自写脚本 heredoc 与管道抢占 stdin 未执行（harness 缺陷非产品缺陷），独立复跑通过并如实记录。徽标为账本置信元数据，不代表实验精度。演示服务用后即停。下一轮候选：PAE 按采样快照、跨采样分数对比视图、任务列表按置信度排序。

## 2026-10-24 持续迭代：按采样置信度曲线

上轮候选落地。依据两源：AF3 Server / EBI 结果页给选中采样各自的置信度图（选中哪个采样看哪个曲线）；AF2/ColabFold 每个排名模型的结构文件 B 因子列即逐残基 pLDDT（MindWalk 指南 + Carugo 2023）。检查发现引擎侧逐采样数组被有意丢弃（_collect_outputs 注释：仅保留 O(models) 标量、远端 worker 不得为丢弃而建 PAE），逐采样 PAE 架构上不取；但选中采样的结构文件本身已在前端加载——曲线可从其 B 因子导出，零后端改动。

1. [完成] bFactorResidueCurve()：按 3Dmol 实测属性（atom.atom 为原子名、b 为 B 因子，探针验证）取每残基一条 CA 记录，跳过侧链原子 / 重复残基 / 非法值 / 其他链污染；文件无可用 B 因子（全 0/缺失）返回 null，绝不虚构曲线。
2. [完成] loadStructure 完成后对非主排名采样做置信度面板原地替换（:not(.pae-panel) 消歧），标题注明"曲线由结构文件 B 因子（pLDDT 惯例）导出"；switchViewerModel 先清曲线防跨采样残留；scope 注更新为"曲线/着色按此采样，PAE 仍为主结果"。
3. [完成] 修复 #7 引入的测试夹具缺陷（本轮两次失败后按规程探针定位）：compact 模板 PDB 行列位错乱，3Dmol 列式解析把 B 因子与坐标读成 0——即 #7 浏览器夹具一直静默落入按链回退、B 因子路径从未在浏览器端真实生效。改为列位精确 ATOM 记录（B 因子列 61–66）并加注释防回归。
4. [完成] 测试：vm +2（曲线取值：侧链/重复/非法/跨链过滤；全 0 返回 null）；浏览器契约 +1（切采样后面板显示 B 因子导出曲线 57.2 并带标注，切回排名 1 恢复主曲线 81.0）；README 第 5 项与 test:model-ui 计数（9 项）同步。

本轮：语法检查、排名模型契约 9/9、着色 vm 10 检查（+2）、workspace-ux 离线 5 套件、隔离 8973 分数契约 12/12、批次回归 9/9、真实 B 因子可用性检查（参考回放结构 229 CA 全部 B 因子有效，列 61–66 实测）、冒烟 12 检查（8974）、根 tests OK、客户端通过、全后端 325 OK（1 按需 skip，71.0s）。曲线为结构文件携带的置信元数据，非 GPU 吞吐或实验精度声明。演示服务用后即停。下一轮候选：跨采样分数对比图、任务列表置信度排序、导入任务的置信度对齐提示。

## 2026-10-24 持续迭代：任务深链与 URL 路由

上轮候选落地。依据两源：SWISS-MODEL 交互工作区按 UniProt accession 参数化的稳定结果 URL；AF DB 每条结构一个公开稳定 URL（2.6 亿+ 条目可分享）。检查发现 selectJob 固定把 hash 写成 #studio——任务无法通过 URL 分享或收藏，公开演示也无法直链某个结构。

1. [完成] jobIdFromHash 解析 #job/{id}；selectJob 把 URL 写成 #job/{id}（地址栏始终对应当前任务）；hashchange 监听先处理 job 分支（selectJob 带 selectionRequest 竞态守卫，前进/后退切换任务安全），视图分支不变；navigate 写视图 hash 时 job 分支自然跳过。
2. [完成] 初始化：深链 id 优先于 localStorage 恢复；未知 id 回退最近任务并 toast 提示，不出现空白界面；页内 hash-only 改到未知 id 由 selectJob 404 toast 承接（不崩溃，当前视图保留）。
3. [完成] 测试：新增 tests/browser_deeplink.cjs 真实服务端到端（自建参考回放任务；4 项：深链直开含结构加载、表格选择跟随 URL、浏览器后退恢复、未知 id 回退）；排名模型契约改由 #job/job-m 启动并 +1 检查（hash 保留）→ 10 项。本轮两次失败均按规程定位：① 新服务无任务（先有鸡先有蛋）→ 测试自建目标；② goto 仅 hash 差异为同文档导航不重载 + 失败路径未关浏览器导致 node 挂起 → 加 query 强制整页导航、finally 关闭。
4. [完成] README 工作流第 2 项与验证段（test:model-ui 10 项、test:deeplink 用法）同步。

本轮：语法检查、排名模型契约 10/10、着色 vm 10 检查、workspace-ux 离线 5 套件、隔离 8973 分数契约 12/12、批次回归 9/9、深链端到端 4/4、冒烟 12 检查（8974）、根 tests OK、客户端通过、全后端 325 OK（1 按需 skip，68.1s）。深链为前端路由行为，无新增数据面或部署。演示服务用后即停。下一轮候选：任务列表置信度排序、跨采样分数对比图、导入任务置信度对齐提示强化。

## 2026-10-24 持续迭代：任务列表置信度排序

上轮候选落地。依据两源：NCBI BLAST 结果表点击列头按 bit score / E-value / percent identity 排序（Geneious 手册与 WU BLAST 教程均确认此惯例）；SWISS-MODEL 模型列表按 GMQE/QMEAN 质量分排序（迭代 #9 已引）。检查发现置信度列只能"看"不能"排"——大批次后想先看高分结构需手动翻页，补齐列的完整语义。

1. [完成] 全部实验表头置信度列可点击：已评分完成任务按平均 pLDDT 降序在前、未评分完成任务居中、非完成任务居后（时间序保留为稳定排序的 tie-break）；aria-sort 标注排序状态，tooltip 注明 BLAST 分值列排序惯例；最近任务紧凑表保持纯时间序不受影响。
2. [完成] sortedJobsForTable 纯函数（拷贝后排序不突变源数组），排序只作用于渲染层数据；事件用 document 级委托绑定，表格局部刷新（refreshJobsTable）后无需重绑。
3. [完成] 契约 +1：默认时间序 → 点击排序 → 顺序断言 [job-m, job-r, job-q] + aria-sort=descending → 再点还原。本轮一次失败按规程定位：为让默认序与置信度序不同重排了 mock 数组，但 detail 夹具按索引展开 jobs[0]/jobs[1] 导致记录串号——改为按 id 查找展开，与顺序解耦。
4. [完成] README 第 6 项与 test:model-ui 计数（11 项）同步。

本轮：语法检查、排名模型契约 11/11（+1 排序）、着色 vm 10 检查、workspace-ux 离线 5 套件、隔离 8973 分数契约 12/12、批次回归 9/9、深链端到端 4/4、冒烟 12 检查（8974）、根 tests OK、客户端通过、全后端 325 OK（1 按需 skip，75.5s）。排序为前端展示层行为，不改变存储或 API 顺序。演示服务用后即停。下一轮候选：跨采样分数对比图、导入任务置信度对齐提示、PAE 视图增强。

## 2026-10-24 持续迭代：采样排名分对比条

上轮候选落地。依据三源：ColabFold 原生 ranking plot（所有模型 ipTM/pTM 一图对比）、AF3 Server / EBI 结果解读（按 ranking_score 排名、逐采样置信度展示）、AF3 output.md（ranking_scores.csv 逐采样分数）。检查发现各采样分数只在下拉与表格里以数字形式散落，缺少 ColabFold 那种"一眼看出首名是否断层"的对比视图。

1. [完成] 多采样任务（≥2 个已评分采样）在"模型与结果制品"面板顶部渲染横向对比条：首选排名分（≥2 个采样有 rankingScore 时），否则回退平均 pLDDT（单链排名惯例）；标注"首名领先 X"，措辞中性不设硬阈值（分差越大择优越可靠，接近则需谨慎对比）；行 title 提示含样本名与 pLDDT。
2. [完成] 可视化遵循惯例：全条同色系（产品 --teal，单色表数量不表身份），当前查看采样以不透明度与加粗状态高亮（状态而非排名着色）；值标签右对齐等宽字体；单采样任务与参考回放任务不渲染。
3. [完成] 契约 +1（12 项）：对比条存在性、首名领先 0.232、当前高亮值 0.842、宽度递减、切换采样后高亮移至排名 2、单模型任务无对比条；截图人工核对布局无碰撞。
4. [完成] README 第 5 项与 test:model-ui 计数（12 项）同步。

本轮：语法检查、排名模型契约 12/12（+1 对比条）、着色 vm 10 检查、workspace-ux 离线 5 套件、隔离 8973 分数契约 12/12、批次回归 9/9、深链端到端 4/4、冒烟 12 检查（8974）、根 tests OK、客户端通过、全后端 325 OK（1 按需 skip，73.3s）。后端套件两次红均为验证脚本调用方式错误（-t backend 破坏可导入性 / 错误工作目录下 -s tests），按 README:150 规范调用复绿 325 OK，产品无回归；纯前端展示层，无存储或 API 变更。下一轮候选：多链界面（per-interface）置信度摘要、导入任务置信度对齐提示。

## 2026-10-24 持续迭代：链间界面置信度表

上轮候选落地。依据三源：EBI AF3 Server 结果解读页（chain_ptm / chain_iptm 为 [num_chains] 数组，chain_iptm 即该链与其余链界面的平均置信度）、alphafoldserver.com 官方指南（逐链 pTM / 逐链对 ipTM 展示）、Dunbrack 2025（链级均值可能掩盖非对称界面）。检查发现多链任务只显示整体 iptm，缺少 AF3 Server 的逐链界面口径。

1. [完成] 后端解析：stream_confidence（流式路径，重复字段拒绝、0–1 数值校验、64 链上限+警告，与 plddt 同级校验）与 parse_confidence（小文件/AFDB 回放路径）均提取 chain_ptm / chain_iptm → chainPtm / chainIptm；原始 JSON 仍为科学记录源。
2. [完成] 存储边界：store.list() 摘要剔除 chainPtm / chainIptm 数组（与 plddt 同策略），详情读取保留；契约测试同步固化（数组剔除 + 详情保留断言扩展到新字段）。
3. [完成] 前端：右栏新增"链间界面置信度"面板（≥2 链且有链级分数才渲染）：逐链 pTM 数值 + 平均界面 ipTM 徽标（复用 campaignScoreBand 的 EBI 阈值分带），提示注明 AF3 chain_iptm 口径且非对称界面建议结合 PAE 判断（Dunbrack 提示，不虚构阈值）。
4. [完成] 契约 +1（13 项）：多链参考任务渲染两行（0.82/0.71 灰带、0.55/0.48 低带）、口径提示存在、单链多采样任务不渲染；独立 mock 截图人工核对布局。

本轮：语法检查、排名模型契约 13/13（+1 链间置信）、着色 vm 10 检查、workspace-ux 离线 5 套件、隔离 8973 分数契约 12/12、批次回归 9/9、深链端到端 4/4、冒烟 12 检查（8974）、根 tests OK、客户端通过、全后端 326 OK（+1 AF3 链向量语义测试，1 按需 skip，77.2s）。链级数据仅随主结果（与 PAE 同范围），不进逐采样标量记录（保持 O(models) 纪律）。下一轮候选：低置信区域淡化切换（AF DB / PDBe-KB 口径）。

## 2026-10-24 持续迭代：低置信区域显示切换

上轮候选落地。依据三源：AF DB FAQ（pLDDT<50 区域常为无结构带状、不应解读，50–70 需谨慎）、PDBe-KB 对比工具（查看器默认 <70 低不透明度显示）、PyMOL 惯例（b<50 选择后隐藏）。检查发现查看器只能"看色"，无法按业界惯例淡化/隐藏低置信区域以聚焦可靠部分。

1. [完成] 工具栏新增"低置信显示"循环开关：关 → 淡化 pLDDT<70（不透明度 0.18，PDBe-KB 口径）→ 隐藏 <50（移除该子集表示，PyMOL 惯例）；仅 Cartoon / Ball & stick 生效，Surface 模式与无逐残基置信度时不切换并 toast 说明，开关文案始终如实反映当前状态。
2. [完成] 选择集构造：排名采样读 B 因子（AF 惯例），主排名按残基 pLDDT 以着色器同源身份规则映射；无法映射到原子、或子集覆盖全部原子（视图会被清空而非聚焦）时返回空并提示；未匹配置信度的原子不受影响，不虚构。
3. [完成] 实现前先探针验证 3Dmol 语义：函数选择器被忽略（setStyle/selectedAtoms 均按全体处理）、v.hide 不存在——改用对象 spec {chain,resi:[...]}（探针证实选择性生效）+ 空样式移除表示作为隐藏机制；两击截图探针字节相同触发诊断：插桩证实状态正确变化（0.18 恰好落在 b<70 原子、隐藏后恰 4 个无 cartoon），headless WebGL 画布导出读旧缓冲属捕获工具限制，非功能缺陷。
4. [完成] 契约 +1（14 项）：关→淡化→隐藏→关 三态循环的逐原子样式断言 + 无置信度任务 toast 不切换。

本轮：语法检查、排名模型契约 14/14（+1 低置信切换）、着色 vm 10 检查、workspace-ux 离线 5 套件、隔离 8973 分数契约 12/12、批次回归 9/9、深链端到端 4/4、冒烟 12 检查（8974）、根 tests OK、客户端通过、全后端 326 OK（1 按需 skip，77.3s）。纯前端展示层，无存储或 API 变更。下一轮候选：导入任务置信度对齐提示、PAE 界面子块均值。

## 2026-10-24 持续迭代：链对界面 PAE 均值

上轮候选落地。依据三源：Zhu et al. 2023 Bioinformatics（⟨PAE⟩_int = 链对间残基对的 PAE 均值，AF-Multimer 界面评估口径，https://doi.org/10.1093/bioinformatics/btad352）、ipSAE 预印本 Park et al. bioRxiv 2023（按链对从 PAE 矩阵导出界面分数，https://www.biorxiv.org/content/10.1101/2023.09.20.558044v1）、EBI Training「Interpreting AlphaFold results」PAE 页（PAE 为残基对相对位置的置信度，链间非对角块低值即界面相对排布可靠，https://www.ebi.ac.uk/training/online/courses/alphafold/inputs-and-outputs/evaluating-alphafolds-predicted-structures-using-confidence-scores/pae-a-measure-of-global-confidence-in-alphafold-predictions）。检查发现迭代 #14 的链间面板只有逐链 pTM / chain_iptm 标量，链对间的 PAE 界面块仍需逐格目测——补每对链的界面 PAE 均值。PAE(x,y)≠PAE(y,x)，均值对两个方向块同时累计（Zhu et al. 2023 口径）。

1. [完成] result_artifacts._interface_pae_means：索引期对已发布 float64 PAE 二进制单次流式逐行读取（内存保持 O(chains²)），按无序链对累计两个方向链间块的均值产出 result.interfacePae；非有限值跳过、无可用单元的链对省略。
2. [完成] 边界如实：仅 2..64 条不同链且矩阵 ≤4,000,000 单元时计算，超限跳过并附警告（单链任务不产出也不警告）；parsers.parse_confidence 内联 JSON 路径同口径镜像，token_chain_ids 与矩阵不对齐或链 ID 非字符串时不产出。
3. [完成] store.list() 摘要剔除 $.result.interfacePae（与 pae / chainPtm 同策略），详情保留。
4. [完成] 前端：链间界面置信度面板内逐链对标签（A↔B 界面 PAE 3.2 Å，.pae-interface-chip），tooltip 注明 ⟨PAE⟩_int 双向块均值口径，PAE 非对称时方向差异以矩阵为准。

改动文件：backend/result_artifacts.py、backend/parsers.py、backend/store.py、web/app.js、web/styles.css、backend/tests/test_result_artifacts.py、backend/tests/test_api.py、tests/browser_ranked_models.cjs、README.md（工作流第 6 项一句话 + test:model-ui 计数 14→15 项）。

验证计划：语法检查、排名模型契约 15/15（+1 界面 PAE 标签）、着色 vm 10 检查、workspace-ux 离线 5 套件、隔离 8973 分数契约 12/12、批次回归 9/9、深链端到端 4/4、冒烟 12 检查（8974）、根 tests OK、客户端通过、全后端 327 OK（1 按需 skip）。

验证结果：全链路重跑九层全绿（23:21–23:24）：语法 OK；排名模型契约 15/15；着色 vm 10；workspace-ux 离线 5 套件 OK；隔离 8973 分数契约 12/12、批次回归 9/9、深链端到端 4/4；冒烟 12（8974）；根 tests OK；campaign_client 通过；全后端 327 OK（1 按需 skip）。如实记录：初跑 T8 红——axis_data 守卫修复落盘（约 23:16）晚于该进程导入时刻（23:15:56），进程按旧字节码跑出修复前同款 3 errors；守卫确认在位（result_artifacts.py:329，AF3 confidence 含 pae 无 token axes 时 axis_data 为 None 的 #16 引入缺陷，be-tests 子代理套件复现 + 主线程定向复验 4/4 修复）后整链重跑取证通过。

## 2026-10-24 持续迭代：PAE 链边界分隔线

上上轮候选落地。依据三源：ChimeraX `alphafold pae` 官方文档（dividerLines 选项默认开启，多链预测在 PAE 图上画线界定链的起止，并如实注明线可能遮挡个别链末端残基、可关闭，https://www.cgl.ucsf.edu/chimerax/docs/user/commands/alphafold.html）、PAE Viewer webserver（Elfmann & Stülke 2023，按链标签分段 PAE 矩阵，https://pmc.ncbi.nlm.nih.gov/articles/PMC10320053/）、EBI Training PAE 解读页（对角块为各链域内置信、链间子块看相对排布，https://www.ebi.ac.uk/training/online/courses/alphafold/inputs-and-outputs/evaluating-alphafolds-predicted-structures-using-confidence-scores/pae-a-measure-of-global-confidence-in-alphafold-predictions）。检查发现 PAE 查看器有界预览/局部窗口已读矩阵原始值，但多链任务的链边界仍需逐格目测 token 索引——补链边界分隔线。

1. [完成] 前端 pae-viewer：spec() 校验描述符 axisMetadata.token_chain_ids（axisKind=token、长度与 rows=cols 对齐、非空字符串≤128）得 chains；无元数据不猜链序。
2. [完成] 绘制：采样/精确窗口相邻索引链 ID 变化处置 1px DOM 分隔线（绝对定位百分比，不碰画布像素、可断言可访问）；窗口内单一链或无可用边界不画；链数 2..128 之外不画。
3. [完成] 诚实口径：默认开启、可一键切换（aria-pressed），方法注注明线可能遮挡个别链末端残基（ChimeraX 文档同款提示）与采样下短链边界可能未被显示。
4. [完成] 契约 +3（13→16）：预览边界位置与测试侧独立重算逐像素对齐（±1e-3）、精确 64×64 瓦片跨界位置恰 50%、窗口内单链隐藏、无 token 元数据不渲染且隐藏开关。

改动文件：web/pae-viewer.js、web/styles.css、tests/browser_pae_lazy.cjs、README.md（工作流第 6 项一句）。

验证计划：语法检查、PAE 契约 16/16、排名模型契约 15/15（回归）、workspace-ux 离线 5 套件、隔离 8973 分数契约 12/12、批次回归 9/9、深链端到端 4/4、冒烟 12 检查（8974）、根 tests OK、客户端通过、全后端 327 OK（1 按需 skip）。

验证结果：九层链路全绿（23:36–23:41）：语法 OK；PAE 契约 16/16（13→16，+3 链边界检查）；排名模型契约 15/15 回归通过；workspace-ux 离线 5 套件 OK；隔离 8973 分数契约 12/12、批次回归 9/9、深链端到端 4/4；冒烟 12（8974）；根 tests OK；campaign_client 通过；全后端 327 OK（1 按需 skip，76.3s）。独立探针几何复核：DOM 分隔线 CSS 位置与画布颜色跃变像素对齐（109.5/220=49.78%，±0.5px），线宽恰 1px、白色 0.85 不透明度加 0.5px 深色描边，深/浅数据块上均可见，开关按钮与 aria-pressed 正常。开发期三修如实记：契约初跑暴露 CSSOM 百分比 6 位有效数字序列化（断言改数值容差）、paintOverlay 漏 return（链边界方法注未附，自查定位）、跨结果缓存 key 切换后无自动绘制（测试改显式点击加载）。
