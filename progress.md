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
