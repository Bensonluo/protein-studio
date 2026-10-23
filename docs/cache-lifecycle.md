# 准备缓存生命周期：扫描、隔离、恢复与删除

2026-10-23。此模块管理控制节点的整目标准备缓存、`af3-chain-v1` 链准备缓存、未完成残留和隔离区。它解决长期筛选积累制品后的空间管理问题。任务输入、已发布结果、传输 ZIP、节点暂存、权重、序列库和主账本均不属于回收范围。

当前实现包含并发保护、磁盘目录清单、异步 HTTP 操作、CLI 与“存储与缓存”页面。打开页面只读取已有目录清单；扫描、预览和执行分别由用户发起。主工作区验收仅运行扫描，隔离、恢复和永久删除测试使用独立临时数据。

## 为什么需要管线级保护

CPU 准备先原子发布缓存，再写任务 descriptor，最后在 SQLite 中提交 `ready`。若只按目录年龄清理，发布与登记之间的目录可能被误判为孤儿。预测复制、远端 ZIP 打包也会先验证清单再逐文件读取；验证后立即移动目录仍会破坏它们。

`backend/cache_access.py` 对同一 cache root 提供合作式共享锁 SH 和维护排他锁 EX：

```text
普通操作：intent SH → main SH → 释放 intent → 使用缓存 → 释放 main
维护操作：intent EX → main EX → 扫描或执行 → 释放 main → 释放 intent
```

进程内条件变量优先放行已等待的维护者。跨进程使用 POSIX `flock`，同线程 SH/SH、EX/EX、EX/SH 可重入；拒绝 SH→EX 升级。等待支持取消和超时，不会退化为无锁操作。

| 管线位置 | 持 SH 的区间 |
|---|---|
| CPU 准备 worker | 资源准入后、领取任务前，直到 descriptor 和 `ready` 引用完成登记 |
| 独立 `prepare_stage` 调用 | 整个准备调用；直接调用本身不建立持久任务引用 |
| legacy 预测 | 清单校验至完整复制到私有 attempt；后续模型计算释放 SH |
| 远端准备包 | descriptor 校验至独立 ZIP 和传输元数据完成；网络发送读取 ZIP |

锁顺序为维护锁→整目标 key 锁→链 key 锁→短 SQLite 事务。不能持有 SQLite 写事务等待维护锁。EX 要等待正在执行的准备退出；这可能超过扫描本身的时间预算，因此页面区分“等待锁”与“正在扫描”。

该锁适用于共享相同 POSIX 文件系统、全部采用此协议的进程。外部删除命令和旧版本写者不参加协议；本实现没有验证跨 NFS 或独立存储系统的锁语义。

## 哪些任务仍需要缓存

`prepared_cache_key` 与 `prepared_path`、`stage=ready` 在同一事务发布。扫描逐行读取 SQLite 引用，新 AF3 数据从紧凑 `preparation_reuse` 获取链来源；旧记录才回退到受限大小的合法 descriptor/manifest。

| 状态 | 处理 |
|---|---|
| preparing | 当前操作的 SH 保护尚未登记的 key、临时目录和发布窗口 |
| ready、predicting、validating、等待恢复 | 保留准备引用 |
| failed、paused | 保留引用，支持原地重试或继续执行 |
| completed | 只有合法且仍存在的已发布结果和结构文件，才允许释放历史准备引用 |
| cancelled campaign | 当前状态机禁止恢复；缓存满足保留期后可进入候选，新提交会重新准备 |
| 无法确认的引用 | 保护全部缓存，并给出诊断 |

共享 key 只要有一个需保留的 owner 就受保护。整目标输入已经内联 MSA 和模板，运行时不依赖外部链缓存；当前实现仍保守保留活动整目标所记录的链来源，历史完成任务不会永久固定所有链。

新结果优先核对同事务提交的紧凑发布索引与主结果/结构文件状态，不读取大型结果 JSON；旧记录才有界读取结果 JSON，巨大或无法确认的旧结果继续保留缓存。详见 [大结果发布与PAE](large-results.md)。清单扫描不读取完整 MSA/mmCIF，也不是全部科学制品的内容完整性审计。

## 扫描与空间口径

显式扫描将目录元数据写入独立 `data/cache-lifecycle.sqlite3`，不把全部 key 或文件树积累到 Python 列表中。目录 frontier、指纹排序和引用集合使用 SQLite；网页按 50 项分页。

扫描设置节点数和时间预算。节点数包括缓存内部文件和目录，并非只计算顶层 key。预算从获得维护 EX 后开始；引用确认也消耗时间预算。返回结果区分：

- `referencesComplete`：是否完整确认需保护的任务引用。
- `inventoryComplete`：是否完成此次目录清单；不可读和预算耗尽会造成不完整。
- `truncated`、`unreadableEntries`、`failClosed` 与有上限的诊断。
- `logicalBytes`：已经完整测量条目的常规文件长度总和。

未知名称、坏 manifest、symlink 或特殊文件保留为异常条目；不跟随外部路径，不自动移走它们。页面不允许从不完整或保护状态不确定的扫描生成清理计划。执行时还会重新检查实时引用和全部计划条目的指纹。

逻辑文件长度不等于分配磁盘块，也不等于真正增加的可用空间。隔离是同文件系统 rename，仍占磁盘；永久删除收据只报告删除文件的逻辑长度，`reclaimedBytes=null`、`spaceReclaimedMeasured=false`。页面磁盘可用量来自文件系统，其他进程也能改变该值。

## 具体计划与恢复

预览固定一份候选清单，包含条目、指纹、保留规则和 SHA256 digest。生成预览不移动文件。执行必须提交对应 `planId`、相同 digest 和明确确认；不能只传一个目录或一条“删除所有过期文件”的规则。

1. **隔离**：重新核查后把具体目录移动到 `.quarantine/<id>/payload`。操作日志先记录意图，再 rename，再提交完成收据；不跨设备复制后删除。
2. **恢复**：针对一个明确隔离条目生成独立计划；原位置已存在时拒绝覆盖。
3. **永久删除**：只处理已隔离、已满足隔离保留期、当前未被引用的具体条目，独立确认。

日志记录移动、恢复和删除中间状态。下一次显式维护根据原路径和 payload 的存在情况对账；冲突保留为异常，部分删除条目不能冒称完整可恢复，需重新扫描并生成计划。取消在安全边界停止，已经处理的条目保留在收据中；不做隐式反向操作。

同一计划的并发执行需串行，并在维护 EX 内重新读取状态；已完成计划重放返回原收据。每次执行使用独立引用快照，避免先前尝试的清理误删另一次尝试的保护记录。操作锁用于区分仍活跃的 CLI 与崩溃遗留记录；服务重启不能仅凭旧 `running` 文本永远阻塞页面。

## 页面与 HTTP 接口

入口：应用导航“存储与缓存”，或 `http://127.0.0.1:8765/?ui=rich#storage`。

| 接口 | 行为 |
|---|---|
| `GET /api/storage` | 已有扫描、操作状态、磁盘可用量和能力开关；不启动目录遍历 |
| `POST /api/storage/scan` | 异步有界扫描，接受 `maxEntries`、`maxSeconds` |
| `GET /api/storage/entries?scanId=…&limit=50&offset=0&kind=…` | 读取目录清单分页 |
| `POST /api/storage/plan` | 异步生成 quarantine / restore / purge 预览 |
| `GET /api/storage/plans/{id}?limit=50&offset=0` | 明确计划与候选分页 |
| `POST /api/storage/apply` | 执行 `planId`、`digest`、`confirm:true` 对应的计划 |
| `POST /api/storage/cancel` | 请求停止当前 `jobId` |

异步接口返回 202 和 job，随后 GET 查询。一次服务只运行一个维护 job。`failed` 或 `partial` 收据映射为失败状态，同时保留已处理条目和诊断，不能把部分完成展示成全部完成。

HTTP 永久删除默认禁用。管理员显式设置 `PROTEIN_STUDIO_CACHE_PURGE_ENABLED=1` 才开放；生成计划和实际执行时各检查一次能力。隔离与恢复仍需查看具体清单并确认，永久删除还要求输入该清单的条目数。

## CLI

以下命令应从项目目录使用 Python 3.12+ 执行；示例 ID 和 digest 替换为命令实际返回值。首次使用先执行扫描和预览。

```bash
python3.12 scripts/manage_cache.py --data-dir /srv/foldspace/data status
python3.12 scripts/manage_cache.py --data-dir /srv/foldspace/data scan --max-entries 100000 --max-seconds 30
python3.12 scripts/manage_cache.py --data-dir /srv/foldspace/data plan --action quarantine --min-age-seconds 604800
python3.12 scripts/manage_cache.py --data-dir /srv/foldspace/data show-plan --plan-id PLAN_ID
python3.12 scripts/manage_cache.py --data-dir /srv/foldspace/data apply --plan-id PLAN_ID --digest SHA256
```

最后一条命令默认仅预览。实际移动必须再加 `--apply`。永久删除还必须是 purge 计划并同时加 `--purge`。CLI 可配置不同扫描预算；它不会因磁盘紧张自动跳过保留期。

## 验证范围

真实线程/子进程锁测试覆盖并行读者、排他等待、写者优先、嵌套访问、取消、超时、异常退出和进程终止释放。发布/复制/ZIP 测试用 barrier 固定时序，验证维护不能进入未登记窗口，私有复制完成后的实际 synthetic 子进程计算不会继续占 SH。

管理器、HTTP 和浏览器验收分别验证引用保护、计划零移动、指纹变化拒绝执行、隔离/恢复字节一致、管理员开关、故障恢复与并发收据。所有破坏性验证均使用临时目录。具体执行结果记录于 `artifacts/cache-lifecycle-verification.json`；浏览器模拟契约与真实 HTTP 验收分开标注。

这些测试使用公开结构回放和小型合成进程，不产生真实 AF3、序列库搜索或 GPU 吞吐证据。此模块也没有新增真实服务器部署；部署继续按用户要求搁置。
