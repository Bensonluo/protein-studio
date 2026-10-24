# 执行进度

2026-10-23：开始本轮常驻批量推理与本地/阿里云节点执行集成。已阅读现有规则与阶段实现，建立计划；正在分配独立模块。

- 完成原生AF3常驻API核查；agent已实现同一ModelRunner与device weights跨目标/批次复用，8项真实进程协议测试通过（受控native模块，未运行实际GPU）。
- 控制面workers模式已接入：CPU准备不要求控制节点有GPU预测命令；不再启动逐目标预测线程。2项模式测试通过。
- 新增显式16卡fleet启动规划与验证；4项fleet测试通过，组内设备无重叠。
- 混合云页面入口已接入，节点/批次/文件传输/故障恢复界面已落盘。
- 独立远程worker协议和实际数据传输正在集成；待真实HTTP双进程演示与故障验收。
- 实际双进程验收完成：24目标全部交付、8个多目标批次、48条hash传输记录、两个resident各初始化一次。证据 hybrid-http-verification.json。
- 真实SIGSTOP故障验收完成：一批中已经接受的1个目标保留原local结果，未交付的2个目标在租约过期后由另一实际HTTP节点接管；18/18最终完成，旧尝试不能覆盖。证据 hybrid-recovery-verification.json。
- 最后集成分发约束：campaign选择本地/阿里云/hybrid，管理员输入长度上限与resident bucket上限参与目标筛选。
- 发现并交付修复：恢复时间线按item关联全部历史batch产生重复node归属；B仅修该显示一致性问题，不影响已验证的任务恢复结果。

最终服务已切换8765 workers模式并保留旧数据。重新执行SIGSTOP故障测试18/18完成，事件归属修复后记录已更新。实际浏览器7组通过（无mock），桌面/手机截图已检查，恢复时间线限制高度可滚动。旧回归记录独立保留。

导出接口新增一致metadata快照：每次200行写临时磁盘文件，再单文件下载，避免坐标/PAE/序列入内存。首次HTTP测试因sandbox拒绝bind，改用本机端口授权执行。

新版验收：后端115发现/114通过/1按需skip；客户端23通过；parser8通过；真实主服务48目标完成及rich浏览器8项通过；隔离浏览器target inspector12项通过；mock cluster5项通过。修复空工作区深链被autoSeed跳转覆盖。最终中文界面与手机截图已检查；最后再次验证新路由/清理后的SIGSTOP恢复。

已完成：最终截图等待过渡后显示正确cluster导航，手机无横向溢出；源码包重建并校验ZIP完整性。主服务保持8765运行，未创建云资源。

用户要求部署先搁置；暂停远程联机/部署文档工作，保留已完成生成器与探针草稿。继续核心管线，选择官方AF3链级MSA/模板复用作为高价值优化。

链复用账本6项测试通过；隔离HTTP与pipeline/export组合22项发现、21过/1按需skip。首次默认sandbox拒绝bind，本机端口授权执行后通过。统计使用SQLite紧凑metadata，不逐条读取prepared/model文件；分页省略entries，详情保留。

完成：链缓存16+旧staged17回归，独立审查无阻断项；真实CPU准备→scheduler→ledger 2×3得5新搜索7命中。最终后端157发现156过1skip，十万项另行过，客户端44过，parser8过；mock链UI8过、真实架构5过、reference详情12过。修复架构图SVG选择器误作用于标题图标，重新截图与手机检查通过。服务8765已恢复（session38602），数据保留，真实部署仍搁置。

持续迭代本轮完成：固定靶标/JSONL流式CLI与UI，指纹续传，准备CPU/内存/磁盘预算准入与面板。172后端发现171过1skip；客户端65过（含两个十万输入内存测试）；新UI11+6、实际服务8、旧详情12过。组合CLI/HTTP/CPU/缓存/账本测试5搜索4命中、低磁盘零领取、重放3重复。服务32521保持运行并保留数据。持续目标active，不部署。

缓存生命周期迭代完成：发布/复制/ZIP维护锁与prepared_cache_key事务索引，受保护引用、异步有界清单、具体digest计划、隔离/恢复/显式purge、取消与中断对账、存储页。独立审查先复现再修同plan并发refs清理竞态及snapshot CAS返回值误报。全后端217发现216过1skip（Python3.12.14，34.046s），客户端65、parser8；十万账本另行7.648s导入/0.13082s末页/0.04585s汇总。新storage mock12/真实临时HTTP浏览器7/主服务只读6、管线只读8过。主缓存只做一次显式只读清单，2whole共338697逻辑字节；主数据无apply。实际隔离恢复逐字节一致和purge只在临时目录。修正live测试对option的isDisabled语义误判：实际DOM disabled=true，改检查DOM属性后通过，无产品改动。服务62600保持8765，保留5campaign/2job，两个回放worker在线；无真实GPU或云部署。

大结果迭代核心完成：source/float64/descriptor、按需PAE接口与UI、result_publication、旧结果下载。全后端250发现249过1skip（74.171s），客户端65（12.463s）、parser8，mockPAE13/真实临时HTTP6/详情12过。D单测初次array.clear不可用已改del buffer[:]；科学类型和TOCTOU独立失败回归均先复现后修。补native worker不生成重复PAE制品与AF3小summary排名快路，随后本轮验收归档。

大结果本轮最终：AF3 summary优先/仅top完整PAE，native worker build_pae=False，控制端独立生成；最终253发现252过1skip（59.46s）、客户端65（12.463s）、parser8；PAE14机制/发布12/独立审查5/API4/传输去重1纳入全测。PAE browser13+实际tempHTTP6、目标详情12、主应用只读8+6通过。十万元数据另过7.677s/0.124165s/0.045675s；2048²实际合成source77,134,000B、RSS29,982,720B、f64 33,554,432B、index3.392s/preview0.009s，保原文/逐值一致。新依赖ijson3.6.0装在项目.venv，source只附requirements。服务51646/8765保留5campaign和2job，未迁移旧数据，未部署GPU/cloud。界面fullPage screenshot取景已修并检查。

## 2026-10-23 MSA迭代交付

实现与独立审查完成；发布后硬退出、过期token、暂停/失败重试、引用保护及预算释放验证通过。后端271发现270过1skip（64.944s）；十万账本另行7.422s导入/0.128754s末页/0.064591s汇总；客户端66过（14.163s）。浏览器契约11/真实HTTP6/原应用只读4通过。主服务session98824，5campaign/2job完整保留，原生库仍未部署。总证据artifacts/msa-preparation-verification.json。

## 2026-10-23 MMseqs搜索后端完成

新增实际adapter+CPU/GPU profile注册与阶段参数/cache身份、AF3 nativepaired/templates、独立搜索设备与过程回收、UI三接点及四步架构。首轮full发现旧worker_mode测试替身未接可选profile参数（1 error），修正签名后完整重跑298发现297过1skip（68.767s）；clients67（14.679s）；新UI契约10/真实HTTP9，旧MSA11/chain8；主只读4/pipeline8。主session52791保留5campaign2job和2回环参考worker；无真实部署。证据artifacts/mmseqs-search-verification.json。

## 2026-10-23 前端体验迭代

完成13组前端问题修复，包含科学着色P1（独立pLDDT不再读B因子）、引擎参数草稿、常驻批次引导、筛选/详情响应乱序、成功提交与刷新失败分离、导出当前筛选、重连状态、缓存分页和键盘焦点。新专项38项与已有66项通过，语法检查通过。旧screening夹具未接profile参数、旧inspector在旧prepare入口注入失败，均修正后保持原断言通过。初次只读测试使用waitForSelector(hidden)默认visible，调整为attached后通过。详见docs/frontend-ux-review.md。

## 2026-10-23 腾讯云与个人网站交付

工作台部署到https://benluo.art/foldspace/，独立public-demo Docker volume，仅公开样例只读。补base path、ijson容器依赖、HTTPS origin门禁与worker路径兼容。原Nginx和portfolio镜像有回退副本。portfolio-fe新增中英featured项目、工程展示、两图切换与已核实public GitHub链接，静态构建发布。14项新增/专项后端、现有API/PAE27和worker10通过；公网工作台4组、portfolio5组通过，原RAG/stock仍200。portfolio原工作区修改未重置或批量提交。细节与回退在deploy/PUBLIC_DEMO.md。

## 2026-10-24 置信度评分排序迭代

业界口径核对后实现批次级置信度筛选：AF3 官方 ranking_score 排序、EBI ipTM 分带（≥0.8/0.6–0.8）与平均 pLDDT（0–100）。SQLite 账本新增 score_* 列与 score_checked 写一次标记，唯一完成路径交付时提取分数；旧数据惰性回填走有界 4MiB compact result 读取，CAS 防并发重复；retry_failed 清陈旧分数。items API 增排序（降序、NULL 最后）与阈值筛选，非法 sort/越界阈值 400。前端批次表格新增置信度列（ranking 加粗、ipTM/pTM/pLDDT 详情、EBI 分带徽标）、排序下拉与条件性 ipTM 阈值快捷筛选（iptmScoredTargets>0 才显示），焦点恢复覆盖新控件。

验收：新增后端 8 项（提取/校验/回填/HTTP 400/SQL 注入陈旧清除）通过；全后端 320 发现 OK（1 按需 skip）；根 tests 67/67；campaign_client 通过；分数浏览器契约 6/6 与既有 browser_campaign 9/9 回归通过。参考回放不含 ipTM：浏览器以 pLDDT 排序验证 UI 契约与 API↔DOM 一致，ipTM 徽标不出现的分支由 metrics 驱动断言覆盖。截图归档 artifacts/campaign-scores.png，模型级目检因工具超时跳过（DOM 断言已覆盖）。排序/阈值实现为元数据级，不代表 GPU 预测吞吐。演示服务（8973）验收后已停止；主服务 8765 未动；无部署。

## 2026-10-24 筛选导出携带分数迭代

按 ColabFold（ranking_debug.txt/scores.json）与 Boltz（confidence.json）等业界导出惯例，补齐 export JSONL 的置信度分数：items 与 export 共享 parse_score_query 校验（同一排序/阈值/400 口径）；新增 backfill_scores 分页回填（seq keyset、干净账本早退、短事务），导出快照前驱动 legacy 行一次性补分；export 行内 resultScores，支持导出筛选子集。前端已交付导出链接携带当前 sort/min_iptm，所见即所得；提示文案与 README 更新。

验收：test_campaign_scores 10 项通过（新增分页回填与导出路由含 legacy 补分/排序/阈值/400 共 2 项）；全后端 322 发现 OK（1 按需 skip，73.944s）；根 tests 67/67；campaign_client 通过；分数浏览器契约 8/8（新增 JSONL 导出顺序与 resultScores、WYSIWYG 链接两项）、browser_campaign 回归 9/9。导出依旧不聚合坐标/PAE，保持一致元数据快照；演示服务 8973 验收后停止；主服务未动，无部署。

## 2026-10-24 置信度分带汇总迭代

按 AF DB pLDDT 四带（≥90/70–90/50–70/<50）与 EBI ipTM 三带口径，批次面板新增已交付目标置信度分布：get() 的 scored 聚合单查询扩展条件计数输出 metrics.scoreBands（未评分交付项不属于任何带，NULL 自然排除）；前端分布行复用分带徽标，无数据段隐藏，tooltip 标注口径与 legacy 行在首次列出/导出后才计入的边界。README 批次管线段同步。

验收：test_campaign_scores 11 项通过（新增分带计数）；全后端 323 发现 OK（1 按需 skip，72.09s）；根 tests 67/67；campaign_client 通过；分数浏览器契约 9/9（新增分布行与账本一致 + 无 ipTM 隐藏段）、browser_campaign 回归 9/9。分带为 SQLite 账本聚合，不代表模型吞吐。演示服务 8973 验收后停止；主服务未动，无部署。

## 2026-10-24 目标详情置信度评分迭代

按 AF3 Server 结果页惯例（ranking_score 源自 ipTM/pTM、EBI 阈值分带）补齐目标检查器的置信度评分区：排名分加粗并副注排名口径，ipTM/pTM/平均 pLDDT 附 EBI 三带与 AF DB 四带徽标及术语 tooltip，明示分数为预测置信元数据而非实验精度。后端零改动（详情 API 已含 resultScores），补 store.item 断言；浏览器契约新增打开检查器验证评分区。

验收：test_campaign_scores 11 项通过；全后端 323 发现 OK（1 按需 skip，73.893s）；根 tests 67/67；campaign_client 通过；分数浏览器契约 10/10、browser_campaign 回归 9/9。演示服务 8973 验收后停止；主服务未动，无部署。

## 2026-10-24 置信度分布条迭代

按 dataviz skill 流程为批次面板新增 ipTM/pLDDT 100% 堆叠分布条：先用 validate_palette 实测发现原分带徽标配色不合格（琥珀/橙 ΔE 6.6），换验证通过的有序色板（绿 #2f7d4f/金 #cfaa2d/橙 #d96820/深红 #9c3226），徽标本身维持原配色；段宽按带计数、2px 表面间隙、外端圆角、逐段 tooltip 与 aria-label，图例行带 swatch+标签+计数（identity 不靠色 alone，金色对比度 WARN 由图例文字 relief）。后端零改动，复用迭代三 scoreBands。

验收：分数浏览器契约 11/11（新增分布条段与图例同步检查）、browser_campaign 回归 9/9、根 tests 67/67、campaign_client 通过、全后端 323 发现 OK（1 按需 skip，72.393s）。分布为 SQLite 账本聚合。截图归档；模型级目检工具本地文件不支持，几何与同步由 DOM 断言覆盖。演示服务 8973 验收后停止；主服务未动，无部署。

## 2026-10-24 批次概览列表迭代

按 run 监控 UI 惯例新增批次概览：≥2 批次时表格列出名称/状态/目标/已交付/失败与 ipTM、pLDDT 迷你分布条（与面板同构造器同口径），点击行切换批次；列表数据来自既有 /api/campaigns 轮询，后端零改动。浏览器契约新增第二批次创建与切换检查。

同时根治迭代 #1 遗留的测试竞态：CampaignScoreHTTPTests 的 app.start() 会启动真实管线 worker 与手工 claim 竞争（负载下两次现形 claim→None TypeError）；该测试类仅走 GET 路由，移除 start() 后确定性消除，全后端复跑 323 OK。

验收：分数浏览器契约 12/12、browser_campaign 回归 9/9、根 tests 67/67、campaign_client 通过、全后端 323 发现 OK（1 按需 skip，73.998s 复跑）。另应用户要求将公开演示（ccef98c）与置信度筛选（bc9b49f）分两个英文 commit 提交。演示服务 8973 验收后停止；主服务未动，无部署。

## 2026-10-24 排名模型切换迭代

按 AF3 Server（结果页默认展示最高排名采样、全部采样可查看、ranking_score 排名）与 ColabFold（ranked_0..4、B 因子列即 pLDDT）两源惯例为三维视图补齐采样切换：≥2 采样时 tabline 出现排名选择器（排名/样本名/pLDDT/排名分），默认排名 1；模型表新增排名分列与"三维查看"动作；非主排名按文件 B 因子四带着色、无可用 B 因子回退按链；下载链接与查看状态跟随当前采样；切换任务自动重置。后端零改动（rankedModels 与 artifact URL 均已存在）。

验收：语法 8 文件、排名模型契约 6/6、着色 vm 6/6（+2 B 因子用例）、workspace-ux 离线 5 套件、隔离 8973 分数契约 12/12、批次回归 9/9、冒烟 12 检查（8974 隔离服务）、根 tests OK、campaign_client 通过、全后端 323 OK（1 按需 skip）。两处环境坑已定位非产品缺陷：冒烟需 CHROME_PATH（Playwright headless-shell 二进制未安装）；workspace_ux_live 前置是常驻主服务，本轮主服务未运行故未复跑，历史绿记录对未改动面有效。模拟 API 契约验证 UI 行为，非新预测或 GPU 性能声明。演示服务用后即停；主服务未动，无部署。

## 2026-10-24 结果制品打包下载迭代

按 AF3 Server / EBI（结果页全部建模结果打包单 zip）与 ColabFold（完成自动下载 result.zip）两源惯例，补齐单任务交付：GET /api/jobs/{id}/archive 把已注册制品（结构、PAE 缓存与 JSON、输入记录；排除路由标记）打包为 zip 一致快照流式下发，completed-only（409）/未知 404，逐成员仍走 artifact() 路径校验；前端制品面板新增"下载全部 zip"，无制品任务不渲染。README API 表与交付段同步。

验收：test_api 19 项通过（+2 归档：字节一致与标记排除、409/404）；排名模型契约 7/7（+1 归档下载）；全后端 325 发现 OK（1 按需 skip，74.9s）；根 tests OK；campaign_client 通过；隔离 8973 分数契约 12/12、批次回归 9/9；冒烟 12 检查（8974）；另加一轮真实回放归档实测：参考回放任务完成后下载 zip 为 8 成员（主结构 + PAE JSON/缓存 + input/ 记录），testzip 完整性通过，Content-Type/Disposition 头正确。归档为制品文件快照，非 GPU 吞吐声明。演示服务 8973 验收后停止；主服务 8765 未动；无部署。

## 2026-10-24 任务列表置信度列迭代

按 SWISS-MODEL（结果表逐模型质量分列 GMQE/QMEANDisCo）与 AF3 Server（列表/结果按置信分数排列）两源惯例，任务表新增置信度列：已完成任务显示平均 pLDDT（AF DB 四带）与 ipTM（EBI 阈值）分带徽标，ipTM 分带复用 campaigns.js 的 campaignScoreBand 全局函数保持口径同源；未评分/未完成显示占位符不虚构，tooltip 注明口径与多采样任务对应排名 1 主结果的边界。后端零改动（列表摘要在剔除大数组时本就保留置信度标量，新增存储测试断言固化该契约）。README 工作流与 test:model-ui 计数同步。

验收：排名模型契约 8/8（+1 列表置信度：双徽标/单徽标/占位符三分支）；test_api StorageTests 4 项通过（含标量保留断言）；全后端 325 发现 OK（1 按需 skip，76.4s）；根 tests OK；campaign_client 通过；隔离 8973 分数契约 12/12、批次回归 9/9；冒烟 12 检查（8974）；真实 HTTP 复验 /api/jobs 列表已完成任务携带 meanPlddt（实测 93.4）。链中该真实检查档首跑因脚本 heredoc 抢占管道 stdin 未执行（测试 harness 缺陷），定位后独立复跑通过，如实记录。徽标为预测置信元数据而非实验精度。演示服务 8973/8974/8975 用后即停；主服务 8765 未动；无部署。

## 2026-10-24 按采样置信度曲线迭代

按 AF3 Server（结果页逐采样置信度图）与 AF2/ColabFold（结构文件 B 因子列即逐残基 pLDDT）两源惯例，补齐 #7 留下的"曲线仍对应排名 1"缺口：查看非主排名采样时，置信度面板曲线改由当前采样结构文件的 CA B 因子导出（bFactorResidueCurve，每残基一条，过滤侧链/重复/非法/跨链），标题注明导出口径；无可用 B 因子返回 null 回退主结果不虚构；切换采样先清曲线防残留。逐采样 PAE 因引擎侧 O(models) 标量纪律与远端 worker 契约明确不做。同步修复 #7 测试夹具 PDB 列位错乱缺陷（两次失败后探针定位：B 因子被列式解析读成 0、#7 浏览器夹具一直静默按链回退），改为列位精确 ATOM 记录。

验收：排名模型契约 9/9（+1 曲线跟随采样）；着色 vm 10 检查（+2 纯函数用例）；workspace-ux 5 套件；隔离 8973 分数契约 12/12、批次回归 9/9；冒烟 12 检查（8974）；根 tests OK；campaign_client 通过；全后端 325 发现 OK（1 按需 skip，71.0s）；真实文件检查：参考回放结构 229 条 CA 的 B 因子全部有效。曲线为结构文件置信元数据，非 GPU 吞吐声明。演示服务 8973/8974 用后即停；主服务 8765 未动；无部署。

## 2026-10-24 任务深链与 URL 路由迭代

按 SWISS-MODEL 工作区（accession 参数化稳定 URL）与 AF DB（每条目公开稳定 URL）两源惯例，补齐任务可分享路由：selectJob 将 URL 写为 #job/{id}，hashchange 先处理 job 分支（selectionRequest 守卫防竞态），前进/后退在任务与视图间安全切换；初始化深链优先于本地恢复，未知 id 回退最近任务并 toast，不出现空白；仅 hash 变更到未知 id 由 404 toast 承接保留当前视图。新增真实服务端到端 tests/browser_deeplink.cjs（4 项）与排名模型契约深链启动检查（10 项）。

验收：深链端到端 4/4（隔离 8973 链内复跑通过）；排名模型契约 10/10；着色 vm 10 检查；workspace-ux 5 套件；隔离 8973 分数契约 12/12、批次回归 9/9；冒烟 12 检查（8974）；根 tests OK；campaign_client 通过；全后端 325 发现 OK（1 按需 skip，68.1s）。两次失败均根因定位非盲试：测试自建参考回放目标（新服务无任务）；query 强制整页导航 + finally 关闭浏览器（同文档导航不触发初始化、未关浏览器挂起 node）。纯前端路由行为，无 GPU 声明。演示服务 8973/8974 用后即停；主服务 8765 未动；无部署。

## 2026-10-24 任务列表置信度排序迭代

按 NCBI BLAST（结果表点击列头按分值排序）与 SWISS-MODEL（模型按质量分排序）两源惯例，补齐任务列表置信度列的排序语义：全部实验表头可点击切换"平均 pLDDT 降序 ↔ 最近优先"，已评分任务在前、未评分完成居中、其余居后，稳定排序保留时间序 tie-break，aria-sort 标注无障碍状态；最近任务紧凑表不受影响。纯前端展示层（sortedJobsForTable 拷贝排序），事件委托绑定兼容表格局部刷新。契约 +1（排序顺序与 aria-sort 断言 + 还原）。

验收：排名模型契约 11/11；着色 vm 10 检查；workspace-ux 5 套件；隔离 8973 分数契约 12/12、批次回归 9/9、深链端到端 4/4；冒烟 12 检查（8974）；根 tests OK；campaign_client 通过；全后端 325 发现 OK（1 按需 skip，75.5s）。一次失败根因定位：mock 数组重排后 detail 夹具按索引展开串号，改按 id 查找。排序为展示层行为，非 GPU 或存储声明。演示服务 8973/8974 用后即停；主服务 8765 未动；无部署。

## 2026-10-24 采样排名分对比条迭代

按 ColabFold ranking plot 与 AF3 Server 结果页惯例，为多采样任务补齐逐采样分数对比视图：模型面板顶部横向对比条（排名分优先，回退平均 pLDDT），标注首名领先幅度与中性解读提示；当前查看采样以状态高亮（同色系单色表数量，不按排名变色）；单采样/参考任务不渲染。契约 +1（共 12 项）：对比条渲染、领先差值、当前高亮与切换联动、宽度单调、单模型无对比条。截图人工核对布局。

验收：排名模型契约 12/12；着色 vm 10 检查；workspace-ux 5 套件；隔离 8973 分数契约 12/12、批次回归 9/9、深链端到端 4/4；冒烟 12 检查（8974）；根 tests OK；campaign_client 通过；全后端 325 OK（1 按需 skip，73.3s）。两次后端套件红均为验证命令调用错误（非产品缺陷），按 README:150 规范调用复绿。展示层行为，非 GPU 或存储声明。演示服务 8973/8974 用后即停；主服务 8765 未动；无部署。
