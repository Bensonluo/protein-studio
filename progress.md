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

## 2026-10-24 链间界面置信度表迭代

按 EBI AF3 Server 结果解读页与 alphafoldserver.com 官方指南惯例，为多链任务补齐逐链置信度：后端流式与小文件两条解析路径均提取 AF3 chain_ptm / chain_iptm（0–1 校验、64 链上限、重复字段拒绝，原始 JSON 不动），任务列表摘要剔除链级数组、详情保留；前端右栏新增"链间界面置信度"面板，逐链 pTM + 平均界面 ipTM（EBI 阈值分带），面板提示链级均值为 AF3 口径、非对称界面建议结合 PAE 判断（Dunbrack 2025 提醒，中性表述）。

验收：语法、排名模型契约 13/13（+1）；着色 vm 10；workspace-ux 5 套件；隔离 8973 分数契约 12/12、批次回归 9/9、深链 4/4；冒烟 12（8974）；根 tests OK；campaign_client 通过；全后端 326 OK（+1 链向量语义测试：越界/非数值/重复字段/超限警告，1 按需 skip，77.2s）。存储摘要契约测试扩展到 chainPtm/chainIptm。mock 截图人工核对。演示服务 8973/8974 用后即停；主服务 8765 未动；无部署。

## 2026-10-24 低置信区域显示切换迭代

按 AF DB FAQ、PDBe-KB 查看器与 PyMOL 三方惯例，为三维查看器补齐低置信区域显示控制：工具栏循环开关 关 → 淡化 pLDDT<70（0.18 不透明度）→ 隐藏 <50（空样式移除表示）；Surface 模式或无逐残基置信度时如实提示且不切换。实现依据探针实证的 3Dmol 对象 spec 语义（函数选择器被忽略、v.hide 不存在）；两次截图字节相同后按规程插桩诊断，确认状态变化正确、差异属 headless WebGL 画布导出读旧缓冲的捕获限制。

验收：排名模型契约 14/14（+1 三态循环逐原子断言）；着色 vm 10；workspace-ux 5 套件；隔离 8973 分数契约 12/12、批次回归 9/9、深链 4/4；冒烟 12（8974）；根 tests OK；campaign_client 通过；全后端 326 OK（1 按需 skip，77.3s）。演示服务 8973/8974 用后即停；主服务 8765 未动；无部署。

## 2026-10-24 链对界面 PAE 均值迭代

按 Zhu et al. 2023 Bioinformatics（⟨PAE⟩_int 界面残基对 PAE 均值，AF-Multimer 评估口径，https://doi.org/10.1093/bioinformatics/btad352）、ipSAE 预印本（Park et al. bioRxiv 2023，按链对从 PAE 矩阵导出界面分数，https://www.biorxiv.org/content/10.1101/2023.09.20.558044v1）与 EBI Training PAE 解读（PAE 为残基对相对位置置信度，链间非对角块低值即界面排布可靠）三源口径，为多链任务补齐链对界面置信度：索引期对已发布 float64 PAE 二进制单次流式逐行读取，按无序链对同时累计两个方向链间块（PAE(x,y)≠PAE(y,x)）的均值，产出 O(chains²) 标量 result.interfacePae（2..64 条不同链、矩阵 ≤4M 单元，超限如实跳过并警告，非有限值跳过）；parsers 内联 JSON 路径同口径镜像（链 ID 与矩阵对齐才产出）；store 列表摘要剔除、详情保留（与 chainPtm 同策略）；前端链间界面置信度面板内逐链对标签展示（A↔B 界面 PAE 3.2 Å），tooltip 注明 ⟨PAE⟩_int 双向块均值口径，PAE 非对称时方向差异以矩阵为准。后端新增 test_interface_pae_means_follow_zhu_2023_convention（全测 327 项），浏览器契约 14→15 项。改动文件：backend/result_artifacts.py、backend/parsers.py、backend/store.py、web/app.js、web/styles.css、backend/tests/test_result_artifacts.py、backend/tests/test_api.py、tests/browser_ranked_models.cjs、README.md。

验证计划：语法检查、排名模型契约 15/15（+1）、着色 vm 10、workspace-ux 5 套件、隔离 8973 分数契约 12/12、批次回归 9/9、深链 4/4、冒烟 12（8974）、根 tests、campaign_client、全后端 327 OK（1 按需 skip）。演示服务用后即停；主服务 8765 未动；无部署。

验证结果：九层链路全绿——语法 OK、排名模型契约 15/15、着色 vm 10、workspace-ux 5 套件、隔离 8973 分数 12/12、批次回归 9/9、深链 4/4、冒烟 12（8974）、根 tests OK、campaign_client、全后端 327 OK（1 按需 skip）。初跑 T8 因 axis_data 守卫落盘晚于进程导入按旧字节码失败（#16 引入的 None 崩溃，be-tests 套件发现、主线程修复并定向复验 4/4），守卫在位后全链重跑取证通过。安全 grep 全 diff 干净（新增插值均 escapeHTML）。演示服务用后即停；主服务 8765 未动；无部署。

## 2026-10-24 PAE 链边界分隔线迭代

按 ChimeraX `alphafold pae` dividerLines 默认行为（多链 PAE 图画链起止线，文档如实注明可能遮挡链末端残基、可关闭）、PAE Viewer webserver 链标签分段与 EBI Training 链间子块解读三源口径，为 PAE 查看器补链边界分隔线：描述符携带 axisMetadata.token_chain_ids（axisKind=token 且与矩阵两轴对齐、非空字符串≤128）时，采样预览与精确窗口在相邻索引链 ID 变化处画 1px DOM 分隔线（百分比定位，不碰画布像素，可断言可访问）；窗口内单一链、链数超 2..128 或无 token 元数据均不画、不猜链序；默认开启可一键切换（aria-pressed），方法注照录 ChimeraX 遮挡提示与采样短链可能未显示。契约 13→16 项：预览边界与测试侧独立重算逐像素对齐、64×64 瓦片跨界恰 50%、单链窗口隐藏、无元数据不渲染。改动文件：web/pae-viewer.js、web/styles.css、tests/browser_pae_lazy.cjs、README.md。

验证计划：语法、PAE 契约 16/16、排名模型契约 15/15、workspace-ux 5 套件、隔离 8973 分数 12/12、批次回归 9/9、深链 4/4、冒烟 12（8974）、根 tests、campaign_client、全后端 327 OK（1 按需 skip）。演示服务用后即停；主服务 8765 未动；无部署。

验证结果：九层链路全绿——语法、PAE 契约 16/16（+3）、排名模型契约 15/15、workspace-ux 5 套件、隔离 8973 分数 12/12、批次回归 9/9、深链 4/4、冒烟 12（8974）、根 tests、campaign_client、全后端 327 OK（1 按需 skip）。探针几何复核分隔线与画布颜色跃变像素精确对齐（±0.5px、1px 线宽、深浅块均可见）。安全 grep 干净。演示服务 8973/8974 用后即停；主服务 8765 未动；无部署。

## 2026-10-24 导入置信度对齐守卫迭代

按 ChimeraX `alphafold pae`（PAE 轴按索引对应关联模型残基、维度不匹配拒绝关联）、AF3 output.md token_res_ids 对应语义与 PAE Viewer 链标签一致性要求三源口径，为导入路径补 PAE 对齐守卫：parse_confidence 产出 N×N 矩阵与结构解析残基数不等时不挂接（弹 pae 与派生 interfacePae），警告如实说明轴无法对齐、原始 JSON 保留下载；pLDDT 长度守卫既有；对齐矩阵走既有内联/外部化路径。测试扩展对齐与错配双场景，全后端 327。守卫上线即揪出 test_proxy_paths 既有夹具缺陷（1 残基 PDB 配 129×129 PAE 静默挂接），夹具改为 129 残基对齐结构后挂载路径与数值断言全保留。改动文件：backend/server.py、backend/tests/test_api.py、backend/tests/test_proxy_paths.py、README.md。

验证计划：py_compile、定向导入测试、后端 327、根 tests、campaign_client、PAE 契约 16/16、排名模型 15/15、workspace-ux 5 套件、隔离 8973 分数 12/12、批次 9/9、深链 4/4、冒烟 12（8974）。演示服务用后即停；主服务 8765 未动；无部署。

验证结果：十层链路全绿——py_compile、导入定向 2/2、PAE 契约 16/16、排名模型 15/15、workspace-ux 5 套件、隔离 8973 分数 12/12、批次 9/9、深链 4/4、冒烟 12（8974）、根 tests、campaign_client、全后端 327 OK（1 按需 skip）。两次红如实记：夹具缺陷由守卫揪出后对齐修复；mmseqs 心跳测试判负载抖动（隔离 12/12 + 整层 327 复跑绿）。安全 grep 干净。演示服务 8973/8974 用后即停；主服务 8765 未动；无部署。

### 迭代 #19（2026-10-25）：每对链 ipSAE 界面置信度

- 研究：核源发现 ipSAE 定义已从 2023 Cβ≤5Å 演进为 Dunbrack 2025 纯 PAE 公式（PMC11844409 Eq. 14-16），AFDB 已采用同口径展示（PAE<10 Å 且 Cα–Cα<15 Å，分带 ≥0.8/0.7/0.6）；独立佐证 DunbrackLab/IPSAE 与 ProteinIQ。纯 PAE 口径恰与既有 PAE 二进制 + token_chain_ids 管线同构，解析器 CA-only 约束无影响；采用论文公式并在 README 明示与 AFDB 距离限制变体的口径差异。ChimeraX binder 三源（官方文档/prospecting 教程/Trac #17749）留作后续候选。
- 实现：parsers.ipsae_row_score 内核 + parse_confidence 小矩阵路径；result_artifacts._interface_ipsae 流式路径（同 interfacePae 守卫与超限诚实跳过）；导入失配 pop；store 剥离；前端 chips 追加 ipSAE + AFDB 四分带着色。
- 验证：10 层链 t1–t10 全绿（全量后端 328 OK/1 skip；browser_ranked_models 16 项）；t7/t8 首跑红系链脚本漏设 STUDIO_URL（连接拒绝、未触达应用代码），隔离服务器补跑 12/12、9/9 绿；SEC-GREP CLEAN。English commit: feat: per-chain-pair ipSAE interface scores。

### 迭代 #20（2026-10-25）：每对链高置信界面接触残基清单

- 研究：AFDB ipSAE 对口径（PAE<10 Å 且 Cα–Cα<15 Å、两方向较低 PAE）+ ChimeraX alphafold interfaces 参与残基输出形态（四源：AFDB FAQ、Dunbrack 2025、ProteinIQ、ChimeraX 文档+教程页）；Cα 口径恰与解析器单 CA 存储匹配，无需扩解析器；token 轴不猜对齐。
- 实现：parsers.interface_contacts 纯内核（网格 O(n)、区间压缩、512 段截断）；result_artifacts._iter_pae_rows + attach_interface_contacts（engine 顶层 join）；server 导入路径同口径；store 剥离；前端 .pae-contact-row 渲染；自捕并修复 pairs 双计 bug。
- 验证：十层链 t1–t10 全绿（全量后端 330 OK/1 skip；browser_ranked_models 17 项；campaign 12/12、9/9）；SEC-GREP CLEAN。English commit: feat: confident interface contact residues per chain pair。

### 迭代 #21（2026-10-25）：3D 视图界面接触残基高亮

- 研究：PyMOL show sticks, byres … within N of 惯例（四源：Medium 教程、Reddit 实践、pymol.org 官方 Selection Operator 表、InterfaceResidues 脚本）；3Dmol addStyle 官方核心 API 叠加式表示；仅主模型（采样是另一结构）。
- 实现：#iface-toggle + interfaceContactSelection（区间钳制、整数 residueNumber 过滤、链分组）+ applyViewerStyle addStyle stick 强调色叠加 + surface 双守卫；测试 fixture 补 residueNumber（fallback 无残基号选不中原子），18 项断言含选中原子 style 断言与采样视图按钮缺席。
- 验证：十层链 t1–t10 全绿（全量后端 330 OK/1 skip/74.0s；browser_ranked_models 18 项首跑即绿；campaign 12/12、9/9）；链内 SEC-GREP 假阴性已手动复扫 CLEAN。English commit: feat: interface contact residue highlight in 3D viewer。

### 迭代 #22（2026-10-25）：PAE 热图悬停残基身份与联动高亮

- 研究：Jalview（hover→tooltip+linked views）、PAE Viewer PMC10320053（青/橙双色打分/对齐约定）、ChimeraX #17112（双色惯例）、EBI AFDB（联动标配）；hover 联动不动 click 既有窗口契约。
- 实现：pae-viewer 残基身份守卫（token 不猜、unknown 轴仅 inline 放行）+ tooltip 身份 + setPAEHover；app.js 序列条双类 + applyViewerStyle 双色球棍（主模型/非 surface 守卫、key 去重、移出即清）。
- 验证：pae_lazy 18/18 ×2 首跑即绿（含 3D 双色原子断言与 token 负例）；十层链 10/10（全量后端 330 OK/1 skip/74.8s）；SEC-GREP 手动复扫 CLEAN。English commit: feat: PAE hover residue identity and linked highlighting。

### 迭代 #23（2026-10-25）：按 PAE 着色的界面接触线

- 研究：ChimeraX alphafold contacts（<8 Å 画线按 PAE 着色、无 PAE 过滤）+ CASP RR 8 Å + LIVIA cLIR + AF3 contact_probs 工具（四源）；距离口径 Cα（单 CA 存储）。
- 实现：内核 lines=True（8 Å 对 + min-PAE + 2048 截断，顺带根治 #20 None-PAE TypeError）；attach/server 落 interfaceLines；store 剥离；前端 toggle + addCylinder 五档蓝→黄 + removeAllShapes 重建。
- 验证：首链 t9 红（vm stub 缺新 API，测试基建问题）→ 修 stub 重跑 10/10 全绿（后端 331 OK/1 skip；ranked 19 项首跑即绿）；SEC-GREP CLEAN。English commit: feat: PAE-colored interface contact lines。

### 迭代 #24（2026-10-25）：PAE 拖框矩形选区与均值读数

- 研究：PAE Viewer NAR 全文（拖框选区：打分 x 段青色、对齐 y 段橙色、重叠洋红；框下均值读数）+ ChimeraX Recipes Mean PAE（「Mean PAE 5.97 in dragged box /A:139-176 aligned to /A:2-19」句式）+ Jalview/EBI AFDB 联动生态（六源）；纯点击保留 #16「读局部窗口」契约。
- 实现：pae-viewer 拖拽机件（mousedown/>4px 阈值/mouseup 二分：成框→finalizeSelection、纯点击→原窗口加载）；finalizeSelection 原始索引区间 + 非空格均值 + （采样格）标注 + 清除选区按钮 + setPAESelection 仅对齐时联动；Escape 全局键 + stop() 清选区；app.js 三色序列条类 + applyViewerStyle 三色球体（仅行橙/仅列青/重叠洋红，r=1.2）。自审纠错：#22 悬停双色映射与 PAE Viewer 原文相反（打分列应青、对齐行应橙），本轮一并修正（app.js + pae_lazy 断言 + README 三处）。
- 验证：pae_lazy 19/19 首跑即绿（含 mouse.down→move→up 拖拽模拟、均值 4.50 Å 精确断言、Escape 清除、3D 三色原子）；十层链 t1–t10 全绿（全量后端 331 OK/1 skip/72.4s，#24 未动后端）；SEC-GREP 手动 diff-scoped CLEAN。English commit: feat: PAE drag-box selection with mean readout and linked ranges。

### 迭代 #25（2026-10-25）：每对链 AFM-LIS 本地界面度量（iLIS 家族）

- 研究：LIVIA About 度量定义表 + flyark/AFM-LIS 源码（transform/mean 双向实现）+ Kim & Perrimon 2026 bioRxiv + af-analysis 独立实现 + Kim 2024 原始 LIS 论文 + LIVIA Prediction Analysis 阈值页（六源）；actifpTM 候选被排除——正牌需 AF2 distogram（AF3 不产出，Dall'Armellina 2025 明示），LIVIA 亦仅 PAE 近似且自标偏高，诚实优先不实现。
- 实现：interface_contacts 单遍历新增 LIS 家族（conf=1−PAE/12 ROC 优化 cutoff、两方向各自均值再平均、cLIS 限 Cα≤8Å 接触格、iLIS=√(LIS×cLIS) 无接触即 0、LIA/cLIA/iLIA/iLISA）；attach/server 落 interfaceScores；store 剥离；前端 chips 追加 iLIS 按 LIVIA Y2H FPR 分带（0.551/0.339/0.223）着色 + tooltip 全口径说明（Cα 代 Cβ 如实标注）。
- 验证：自捕真 bug——close_pairs 只存 小→大 有序对，反向接触格漏查致 B→A 方向 cLIS 漏计，冒烟对拍手算发现并修复；后端 40/40（手算 fixture 精确断言 LIS 0.6806/cLIS 0.75/iLIS 0.7144）；browser_ranked_models 20/20 首跑即绿；十层链 10/10（后端 332 OK/1 skip/70.8s）；SEC-GREP 手动 diff-scoped CLEAN。English commit: feat: per-chain-pair iLIS local interface scores。

### 迭代 #26（2026-10-25）：3D 界面着色模式（非界面灰化）

- 研究：LIVIA About/Prediction Analysis（Gray non-LIR 选项 + Mol* LIR cartoon 默认视图 + Gradient 链色预设）+ ChimeraX interfaces 官方文档（伙伴链浅灰剪影 Show Contact Residues）+ flyark/LIVIA GitHub README（LIR 链色/cLIR 深色结构渲染）+ PyMOL InterfaceResidues 工作流（Oppenheimer 博客：非界面残基单独灰化）（四源）；actifpTM 候选研究后排除——正牌需 AF2 distogram（AF3 不产出，Dall'Armellina 2025 明示），LIVIA 亦仅 PAE 近似且自标偏高，诚实优先不实现近似口径。
- 实现：#color-mode 新增「界面着色」选项（仅 ifaceData 门控）；applyViewerStyle interface 分支——非界面残基灰底 #8e9ca6（setStyle 字面色）+ 界面残基 per-链色 addStyle 字面色（#21 可观测惯例）；surface 无逐原子样式走渲染期 colorfunc；采样视图/无接触回退按链着色（select 联动回退选中显示）。
- 验证：调试弧线如实记录——首版 colorfunc 断言超时 → 探针证实 3Dmol 将 colorfunc 解析进 WebGL 顶点色、不落 atom.style（字面色才可观测）→ 改字面色方案 → 二次探针证实产品行为正确且切回链色无字面色残留 → 修测试第二断言预期 → browser_ranked_models 21/21 绿；十层链 10/10（后端 332 OK/1 skip/72.7s，#26 未动后端）；SEC-GREP 手动 diff-scoped CLEAN。English commit: feat: interface focus color mode。

### 2026-10-25 迭代 #27：链间 2D 接触图（feat: inter-chain 2D contact map）

- 源：COcaDA 蛋白接触数据库文档（接触图两轴各一条链、每点一对残基、悬停明细、跨链即标准形态）+ ConPlot（Bioinformatics 2021, PMC8428603，接触图/distogram 标准视图）+ LIVIA universal 页（linear contact map 连接物理接触 cLIR 对；PMC13174586）（三源）。
- 实现：renderChainConfidence 每对链一个小 SVG 接触图——每轴一链（链内序列位置、端点结构残基号刻度、链色 Chain 标签）、每点一对 Cα≤8Å 接触残基（悬停 title=残基身份+PAE）、点色=3D 接触线同款五档 PAE 分带（两视图同读）、五档图例一条；复用 #23 interfaceLines 制品零后端改动；PAE 缺失不画点、索引越界/残基数组缺席整组跳过。
- 验证：首跑红在测试侧——fixture 原两条 line 落链 A 内部，被产品跨链守卫正确跳过（真实后端只产跨链对），修 fixture 补两条跨链 line（3D 侧因 B 链无 CA 原子自动跳过，#23 断言原样通过）；复跑 browser_ranked_models 22/22 绿；十层链 10/10（后端 332 OK/1 skip/72.8s，零后端改动）；SEC-GREP 手动 diff-scoped CLEAN。English commit: feat: inter-chain 2D contact map。

### 2026-10-25 迭代 #28：环形接触总览（feat: circular contact map overview）

- 源：LIVIA universal+About（Circular Contact Map chord diagram：弧=链∝长度、cLIR 线跨全部链对）+ AlphaBridge ribbon_plot（PDB-REDO，独立同式）+ COcaDA/ConPlot（#27 家族参照）（三源）。
- 实现：renderChainConfidence 新增 contactChord——弧角∝链残基数、链色带、链标签外置；弦=跨链 Cα≤8Å 接触对、过圆心贝塞尔、五档 PAE 色带与 2D 图/3D 线同语言、悬停 title 同款；与 2D 图同门控（mapGroups 空不渲染）。零后端改动。
- 验证：browser_ranked_models 23/23 首跑即绿；十层链 10/10（后端 332 OK/1 skip/74.2s）；SEC-GREP 手动 diff-scoped CLEAN。English commit: feat: circular contact map overview。

### 2026-10-25 迭代 #29：序列条界面接触残基标记（feat: sequence interface residue marking）

- 源：LIVIA sequence viewer（LIR/cLIR 序列级高亮惯例）+ ProSAT+（H-ITS，序列特征↔结构高亮同型）+ RCSB Sequence Annotations Viewer / Jalview（序列特征轨道通行惯例）（三源）。
- 实现：renderSequence 逐残基判属 interfaceContactSelection()（与 #21/#26 同一后端高置信接触集，仅主模型）→ 链色下划线（同 2D 图/chord 调色板）+ title 标注「界面接触残基」；单一诚实层、不造假 LIR 层；residueNumber 非整/链不匹配跳过。
- 验证：产品侧首红——新代码把返回数组当 Map 用（.get 抛 TypeError 断渲染管线），改 Map-of-Sets 包装后修复；browser_ranked_models 24/24 绿；十层链 10/10（后端 332 OK/1 skip/73.8s，零后端改动）；SEC-GREP 手动 diff-scoped CLEAN。English commit: feat: sequence interface residue marking。

### 2026-10-25 迭代 #30：逐链对 pDockQ/pDockQ2 界面置信度（feat: per-chain-pair pdockq and pdockq2 interface scores）

- 源：Bryant et al. 2022 Nat Comms（pDockQ 原文公式与 sigmoid 常数）+ Zhu et al. 2023 Bioinformatics btad424/PMC10348836（pDockQ2 原文、AFM 偏高警示、其参考实现 pdockq2.py 本用 Cα）+ foldmetrics 变体表（Cα-for-Cβ 文档化变体）+ ProteinIQ DockQ 指南（≥0.23/0.5 阈值）+ LIVIA pDockQ 家族表（五源）。
- 实现：backend interface_contacts 在 close_pairs 单遍累积逐链对 n/界面残基并集/逐方向接触格 plddt 与 T=1/(1+(PAE/10)²) 和；score_rows 追加 pdockq（⟨pLDDT⟩_int×log₁₀n 过 sigmoid）与 pdockq2（逐方向乘积过 sigmoid 取 max，foldmetrics 惯例）；不可算键缺席（无 plddt / 接触格 PAE 缺失）；纯物理接触无 PAE 支持的链对不因 pDockQ 进表（Zhu 指出的假阳性类，不放大）。前端 chips 分带 + tooltip 公式/Cα 变体/AFM 警示；chips 块整体重写为 parts-array 结构。
- 验证：六残基 fixture 手算 pdockq=0.019/pdockq2=0.395 与实现精确一致；三条诚实路径（无 plddt 双键缺席、接触格 PAE 缺失 pdockq2 缺席 pdockq 保留、无 PAE 合格格整行缺席）；legacy 精确断言零改动（fixture 几何 9Å>8 → 键自然缺席）；browser_ranked_models 25/25；十层链 10/10（后端 333 OK/1 skip/72.2s）；SEC-GREP 手动 diff-scoped CLEAN。产品侧两红记录：嵌套模板编辑致 SyntaxError（重写 parts-array 根治）、断言算术错（pDockQ2 0.61 实为 mid 非我期待的 high，产品分带逻辑正确）。English commit: feat: per-chain-pair pdockq and pdockq2 interface scores。

### 2026-10-25 迭代 #31：界面聚焦相机 + 委托监听器幂等修复（feat: interface focus camera with idempotent event binding）

- 源：3Dmol.js 官方文档（zoomTo(sel) 选择居中+slab、空选择全场景、AtomSelectionSpec.predicate）+ ChimeraX view/Focus 官方文档与邮件列表（focus=平移+缩放+裁剪面包裹选中项）+ Mol* Maneuvering 官方文档（选中即聚焦）（三源）。
- 实现：接触行「聚焦界面」按钮 → interfacePairSelection 单链对 {chain,resi} 组 → predicate 选择 → zoomTo 即时聚焦；同键再点回全貌；selectedAtoms 空不聚焦；仅主模型。附带两个真实修复：① t5 harness 静态服务器无缓存头→Chromium 启发式缓存旧 app.js（会话中改文件即踩），补 Cache-Control:no-store；② bindViewEvents 每次 renderMain 重复注册 document 级委托监听器——单击 N 连击（数据排序按钮同病已久），加 bindViewEvents.bound 幂等守卫。
- 验证（长调试弧，诚实全记）：首红貌似点击无效→11 轮探针逐一排除（直调成功/合成点击成功/事件冒泡正常/监听器存在且调用/无异常/无 DOM 重建）→全量插桩终判：单次 click 处理器执行 3 次（focus-path,toggle-off 交替）＝监听器堆叠；初判 Playwright 重试双击/缓存陈旧均被后续证据修正为表象。修复后 browser_ranked_models 26/26 绿；十层链见下；SEC-GREP 手动 diff-scoped CLEAN。English commit: feat: interface focus camera with idempotent event binding。
