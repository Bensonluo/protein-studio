# 本地 16 卡与阿里云：常驻模型、带校验传输、逐项目标恢复

本文对应本仓库的混合云执行实现，核对日期 **2026-10-23**。当前原生常驻模型适配器支持 **AlphaFold 3 v3.0.4 接口**；`reference` 用来验证真实 HTTP、文件传输、租约与公开结构回放。ColabFold 保留已有分阶段 runner，本轮没有宣称它已具备原生模型常驻能力。

关键变化是 GPU 节点启动一次模型进程，连续接收多批兼容目标。CPU 搜索准备仍在本地数据库一侧完成，结果通过带 SHA-256 的准备包发送给节点；节点逐项回传结果，控制面校验后进入验证发布阶段。整批百万输入不等于一个百万条的模型张量，也不等于启动百万个模型进程。

## 1. 先把“规划资源”“执行节点”“模型进程”分清

“本地 16 卡”是部署计划。只有 agent 注册并持续发送心跳，页面才列出在线节点；配置文件里的 16 不会自动变成 16 个在线设备。`GET /api/cluster` 分别返回 `planned`、`nodes`、`batches`、`transfers`、`events`。

| 页面字段 | 实际含义 |
|---|---|
| 本地规划资源 | 控制面配置声明的卡数，不是探测到的设备 |
| 本地 / 阿里云位置在线节点 | 注册表中的位置，加最近心跳状态；一个节点不必等于一张卡 |
| devices / gpuGroups | 节点上报的可见设备与独占分组 |
| maxResidues / capacityEvidence | 管理员与 runner 共同限制的目标长度资格，不是通用显存保证 |
| PID | 常驻模型进程标识；重启后应变化 |
| modelInitCount | 模型后端初始化次数；回放也可初始化一次运行时 |
| modelLoads | 实际模型权重加载计数；reference 必须为 0 |
| loaded / processedTargets | 节点上报的驻留状态及进程累计处理数 |
| batch.completed | 已被接收的逐项目标回传数；不等于全部经过最终验证 |
| batch.members[].stage | 每个目标在控制面中的实际阶段，可判断 completed / validating |
| transfer.bytes / sha256 | 本次真实传输包的大小与摘要 |

这些运行信息由 worker 上报，不是硬件远程证明。`mode: fixture` 的节点固定显示为演示，不计作真实 GPU 推理能力。两个演示进程即使分别标记 `local` 和 `aliyun`，也可能都在同一台 Mac 的 loopback 上运行；这只证明跨进程协议与真实文件流转，不证明已经连上阿里云 GPU。

页面每 4 秒读取状态，刷新数据块时保留位置筛选、搜索文字、展开的哈希详情与表格滚动位置。节点、批次与传输列表有返回数量上限，页面会注明近期窗口，不将窗口统计误称为全历史产能。

### 一键复现本机双节点演示

```bash
python3 scripts/start_hybrid_demo.py \
  --port 8766 --data-dir ./data-hybrid-demo \
  --seed-demo --targets 24 --batch-targets 3
```

打开 `http://127.0.0.1:8766/#cluster`。脚本创建本次演示配置和临时环境凭据，启动一个控制面、两个实际 HTTP agent 与两个常驻 reference 子进程，并导入小批公开结构回放。两个位置标签都在本机回环运行，未使用真实阿里云 GPU；日志与结果保存在指定目录。Ctrl+C 停止本次启动的进程；已占用端口不会被脚本强行接管。

## 2. 一条真实目标的完整路径

1. 候选流式导入 campaign，输入清单和规范参数写入 SQLite。CPU preparation worker 领取目标；完整目标缓存未命中时，AF3 自动准备复用独立链的原始 MSA/模板，只搜索缺少的链，再组装自包含完整输入。其他引擎保持原有准备路径。
2. 准备完成后目标进入 ready。`workers` 模式关闭旧的本地 campaign prediction 线程，由注册节点通过认证接口拉取工作；CPU preparation 和 CPU validation 池继续运行。
3. 控制面从有界候选窗口选择兼容目标，分配 node、batch、GPU group，并为每个目标生成独立 lease token。批次签名包含引擎、数据库 profile 指纹、采样/循环设置、执行身份、运行版本与粗粒度长度桶。
4. worker 下载准备包，检查整体 SHA-256，按清单安全解包，再验证准备产物。预测机器不必为每个目标远程读取整套本地序列库。
5. worker 把一批目标路径写给已启动的常驻模型进程。模型逐项完成并发出结构及结果路径，agent 将每项结果打包上传。
6. 控制面先验证上传大小、摘要、路径与产物，再以当前目标租约执行条件更新。旧进程、已取消任务、过期租约都不能覆盖新 attempt 的有效结果。
7. 目标进入 validating，独立 CPU 验证池继续检查序列、结构/文件路径和发布完整性。通过后，结果指针发布为 completed，可查看结构、置信度及下载原始文件。

预测阶段仍会产生并解析部分原生模型结果；独立 CPU 验证池并不代表所有置信度解析已从预测进程移出。上述分工的目的，是让昂贵 GPU 阶段不再等待全库搜索，并让目标级交付、重试与追踪可独立进行。

## 3. 常驻模型是怎样实现的

`backend/resident.py` 管理一个长期存活的 JSONL 子进程，参数通过 argv 传入，不经 shell。模型日志写 stderr；stdout 只承载 ready、逐项目标事件与批次完成消息。一次批次结束后进程保留，下一批继续使用它；进程死亡、取消或超时会进入 broken 状态，明确重启后才能接收新任务。

`scripts/resident_runner.py` 的 AF3 后端加载已安装的官方运行模块，创建一个 `ModelRunner`，读取一次缓存的 `model_params`，将权重放到选定 GPU 并等待设备完成；后续目标复用相同 runner 和模型函数。它依赖已核对的官方 v3.0.4 `ModelRunner` / cached-property 接口，所以升级 AF3 必须重新验证该适配，而不是仅改一个版本字符串。[AF3 v3.0.4 官方运行实现](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/run_alphafold.py)

原生进程协议与兼容性细节见 [resident engine](resident-engine.md)。下面是模型进程本身的示例命令，通常由 worker agent 启动，不作为浏览器提交命令：

```bash
/opt/alphafold3/venv/bin/python /srv/foldspace/scripts/resident_runner.py \
  --engine alphafold3 \
  --af3-script /opt/alphafold3/run_alphafold.py \
  --model-dir /srv/protein-models/alphafold3 \
  --model-version 实际代码与权重标识 \
  --num-samples 5 --num-recycles 10 \
  --device 0 \
  --compilation-cache /srv/protein-cache/jax-af3 \
  --allowed-root /srv/foldspace-workers/local-gpu-00
```

参数加载一次不等于所有 shape 都已编译：新长度桶首次运行仍可能触发 JAX 编译。页面加载状态不能当成预热完成证据，实际运行元数据会保留 `compiledShapesWarmed: false`。应在代表性长度上记录冷启动、首个 shape 和稳定运行时间，之后再判断是否需要调整桶、准备缓存或 worker 数。

一个 resident 的采样数、recycle、设备、模型版本等初始化条件固定。兼容任务才交给该进程；参数不同的实验通过另一组进程或明确重启处理。不能为了复用模型悄悄改用户的科学参数。

**这里的 batch 表示连续处理多个目标，不是模型张量 minibatch。** 当前 AF3 resident 对同一批目标顺序执行；收益来自复用初始化、权重和可复用编译，以及减少每条目标的进程启动。没有实测就不写固定加速百分比。

## 4. 本地 16 卡如何运行

包含的原生 AF3 adapter 使用一个可见设备，所以直接可部署的路线是：每张卡一个常驻 worker，16 张卡并行处理不同目标。`config/fleet.local16.example.json` 列出 16 个独立 GPU worker；它是计划，不是在线节点证明。

```bash
cp config/fleet.local16.example.json config/fleet.local16.local.json
# 修改 controllerURL、实际主机设备、Python/模型/权重路径和运行版本。
python3 scripts/launch_worker_fleet.py \
  --config config/fleet.local16.local.json --dry-run
```

dry-run 只展示启动计划，不读取 token 值，不启动模型。完成路径和凭据配置后去掉 `--dry-run` 才启动。每个 worker 的 token 由对应 `tokenEnv` 环境变量继承，配置文件不写明文密钥。launcher 在模型启动前设置该 worker 的 `CUDA_VISIBLE_DEVICES`，同一主机的设备组不得重叠。

fleet launcher 默认只重启退出的 slot，其他健康 worker 继续执行。`--restart-limit` 默认 3，`--restart-backoff` 默认 5 秒，按失败重启次数递增等待；达到上限的 slot 停止自动重试，不形成无限重启。`--shutdown-grace` 默认 300 秒，为收到终止信号后的 draining 留出时间。持续 OOM 或配置错误应先修复，不靠不停重启掩盖。

Linux 长期部署可使用 [deploy/foldspace-worker@.service](../deploy/foldspace-worker@.service) 和 [deploy/worker.env.example](../deploy/worker.env.example)：修改实际用户、Python、项目路径，按 worker ID 设置对应环境文件，由每个 service instance 管理一个 slot。服务模板使用失败重启与 SIGTERM 停止；对同一设备选择 fleet 或 systemd 一种管理方式，避免两套 supervisor 重复占卡。controller 注册文件与主机 fleet 的 worker ID/tokenEnv 必须一致。

如果 16 张卡分布在四台机器，分别给每台生成四个 worker 的配置，设备编号使用所在主机的本地编号，worker ID 全集群唯一；不能在一台机器上写 0–15 就宣称它能访问其他服务器的卡。

对于确实需要原生多卡模型的长序列，可在模型支持的实现中配置合法的设备组，再由与该实现适配的 resident runner 执行。当前 native AF3 fleet 明确拒绝一进程多设备，不会把调度器分配多卡当作模型自动实现分布式。其他模型多卡支持需要独立验证，不能复用单卡性能乘法推断。

### 任务分发还要通过两个资格条件

在 `workers` 拉取模式下，campaign 的 `placement` 可取 `local`、`aliyun` 或 `hybrid`，默认 `hybrid`。`local` 只允许管理员注册位置为 local 的节点领取；`aliyun` 只允许 aliyun；`hybrid` 允许两侧的兼容节点领取。它限制执行位置，不代表自动租云、平均分配比例或优先级 SLA。网页批次创建可选择该字段，直接 API 则在创建 campaign 的顶层传入，例如 `"placement": "local"`。

管理员在 worker 注册配置中设置 `maxResidues`，节点还报告 runner 能接受的上限；控制面使用二者较小值。内置 AF3 runner 的声明范围与其编译 buckets 配置相关。领取时还受 `batchResidues` 的整体限制，因此超过资格范围的目标不会被这台节点领取，应检查是否有符合条件的节点或经过实测后调整配置。

这里的残基上限是**分发门槛**：相同总长度，不同链数、MSA 深度、模板、采样、精度和硬件也可能有不同显存需求。应以实际 GPU/模型/参数组合的成功率与峰值显存设置上限，并保留余量；默认门槛或最大编译桶都不是“该长度必定装得下”的保证。需要运行更长目标时，先验证对应硬件/算法执行方案，不能只把数字调大。

## 5. 控制面启动与节点凭据

控制面保留数据库搜索和验证，启用 authenticated pull：

```bash
cp config/workers.local16-aliyun.example.json config/workers.local.json
# 按实际节点修改注册表、maxResidues，并向控制面和对应节点注入相同 tokenEnv 凭据。
export PROTEIN_STUDIO_DISTRIBUTED=1
export PROTEIN_STUDIO_PREDICTION_MODE=workers
export PROTEIN_STUDIO_WORKER_CONFIG="$PWD/config/workers.local.json"
export PROTEIN_STUDIO_DATABASE_CONFIG="$PWD/config/databases.local.json"
export PROTEIN_STUDIO_CPU_PREP_WORKERS=2
export PROTEIN_STUDIO_CPU_VALIDATE_WORKERS=1
export PROTEIN_STUDIO_READY_HIGH_WATER=64
python3 -m backend.server
```

准备命令与数据库设置沿用 [数据库管线](database-and-batch-pipeline.md) 和 [staged runner 配置](staged-runners.md)。默认不设置 `workers` 模式时保持 legacy staged 行为；真实 resident 集群运行必须显式切换，避免旧预测线程和远端 worker 同时竞争同一 ready 队列。

`config/workers.example.json` 目前是两个明确的 reference fixture 凭据样例。生产注册文件应列出实际节点，示例如下；该条目只是允许节点注册，尚不表示它在线：

```json
{
  "id": "local-gpu-00",
  "name": "Local AF3 GPU 00",
  "location": "local",
  "mode": "real",
  "engines": ["alphafold3"],
  "maxResidues": 2048,
  "tokenEnv": "PS_LOCAL_GPU_00_TOKEN"
}
```

这里的 `2048` 只是配置语法示例，须替换为该节点实际验证过的分发上限。阿里云节点把 `location` 设为 `aliyun`。节点身份和 location/mode 来自管理员注册表，而不是让普通请求自行声称生产位置。为每个节点分配不同的高熵 token，将值通过已有 Secret 管理或受保护环境注入控制面和对应 agent。worker 接口使用 Bearer 认证，浏览器 API 不返回 token。

远程控制面地址使用 HTTPS，由管理员配置的反向代理终止 TLS，保留证书与主机名校验；本地 loopback 演示才使用 HTTP。哈希负责发现包内容变化，TLS 和凭据负责连接及身份，二者不能相互代替。Python 默认 TLS 客户端证书验证机制见 [Python 3.12 ssl 官方文档](https://docs.python.org/3.12/library/ssl.html)。本项目未实现公网用户账号系统；浏览器工作台通过受控内网或已有认证代理访问。

## 6. 现有阿里云 GPU 怎么加入

有现成 ECS GPU 服务器时，在该服务器安装与本地兼容的模型环境与已授权权重，放置项目代码、worker 本地可写目录和编译缓存，再运行对应 worker。agent 主动向控制面拉任务，无需把本地 SQLite 文件共享给云端，也不需要逐目标 SSH 启动一个新模型进程。

若已有 ACK GPU 节点池，可以把常驻 worker 作为长期运行的工作负载，由 ACK 分配真实 GPU；权重只读挂载、临时准备包与输出在本地工作目录，凭据通过 Secret 注入。节点池可用于区分硬件和维护策略，并支持纳入已有 ECS 实例；实际平台配置遵循 [阿里云 ACK 节点池官方说明](https://www.alibabacloud.com/help/en/ack/ack-managed-and-ack-dedicated/user-guide/node-pool-overview)（页面更新 2026-08-29）。本轮不创建 ECS、不扩大节点池、不购买云资源。

单个真实云节点的启动方式如下；节点 ID、token 环境变量和版本必须与控制面注册表一致。这里的 GPU 0 是当前节点已分配的实际设备：

```bash
python3 scripts/worker_node.py \
  --url https://foldspace-controller.example.org \
  --worker-id aliyun-gpu-00 --token-env PS_ALI_GPU_00_TOKEN \
  --engine alphafold3 --runtime-version 实际代码与权重标识 \
  --gpu-devices 0 --samples 5 --recycles 10 \
  --work-dir /srv/foldspace-workers/aliyun-gpu-00 \
  --resident-command '["/opt/alphafold3/venv/bin/python","/srv/foldspace/scripts/resident_runner.py","--engine","alphafold3","--af3-script","/opt/alphafold3/run_alphafold.py","--model-dir","/srv/protein-models/alphafold3","--model-version","实际代码与权重标识","--num-samples","5","--num-recycles","10","--allowed-root","/srv/foldspace-workers/aliyun-gpu-00"]'
```

worker 客户端验证 TLS 证书，不跟随重定向，不继承环境代理；远程明文 HTTP 默认拒绝。控制面若直接绑定非 loopback 地址，还需显式设置 `PROTEIN_STUDIO_ALLOW_REMOTE_BIND=1`，并由实际网络访问控制和 TLS 代理保护。不要为了接通而关闭证书校验。收到 SIGTERM/SIGINT 后 agent 进入 draining，完成当前批次逐项回传后退出；云回收是否留有足够时间仍取决于剩余目标耗时。

本地与云端都按独立目标批次执行，避免跨广域网进行模型同步。准备包包含目标实际需要的 MSA/模板产物，不把整个数据库重复上传。云节点仍需足够 CPU/RAM 来做预测侧特征处理与结果处理；不能把“搜索已经拆开”误解成 GPU 进程完全不需要 CPU。

`config/hybrid-topology.example.json` 汇总控制面、库、缓存、fleet 与云节点的部署关系，**它是说明性拓扑清单，不是自动 provisioner，也不是实时节点注册表**。实际启动分别读取 worker registry 和各主机 fleet 配置。

## 7. 传输完成不等于结果已提交

准备包与结果包均以实际字节计算 SHA-256。接收方先检查摘要和限制，再解包；安全解包拒绝路径穿越、绝对路径、重复条目、符号链接及超出数量/解压大小限制的内容。当前配置默认单包上限 256 MiB、解压后上限 1 GiB，可按实测产物调整，不能只放开 HTTP 请求上限而忽略解压后占用。

上传的最后一步是带租约的条件提交：目标仍归属该节点、token 未变、租约未过期、campaign 未取消，才允许从 predicting 进入 validating。这是对账本当前状态的比较并更新，不是仅把文件复制到共享目录后宣称成功。传输记录保留方向、字节数、hash、item 与 batch 关联；浏览器能展开查看完整摘要。

下载记录依次区分准备完成、发送完成、worker 验证；上传记录的 accepted 表示控制面已接收并通过提交条件。相同摘要的结果包重复上传可以取得幂等回执；网络失败有限重试，未确认的本地结果包保留，避免把丢失 ACK 误当作必须重新推理。

批次里某项已经上传并被接受，即使后续节点丢失，这项也不应跟着整个批次重新计算。最终交付以目标 completed 为准；面板的“回传 x/y”用于表示接收进度，验证与交付在每项目标阶段里继续追踪。

## 8. 阿里云抢占、断网或模型进程死亡怎么办

控制面给每个目标独立租约，心跳续租。节点断网或进程退出后，未完成目标在租约恢复流程中重新进入可领取状态，新 attempt 获得新 token；旧节点晚到的上传不能覆盖新结果。节点重新注册也形成新 session，旧 session 不再有权续租或提交。常驻进程异常后的重启重新加载权重是合理恢复成本，不能为了显示“一次初始化”隐瞒重启。

对于抢占实例，阿里云系统事件文档列出 `Instance:PreemptibleInstanceInterruption` 回收通知，通常在回收前五分钟发出。将已有云事件处理接到 worker supervisor：停止领取新批次，争取上传已完成目标，未完成部分让租约机制恢复。不要假定五分钟一定够跑完长目标，也不要把目标级恢复说成可以从模型计算中间位置继续。[阿里云 ECS 系统事件](https://help.aliyun.com/en/ecs/user-guide/summary)

云事件监听与实例生命周期管理由部署侧接入；当前 worker 协议提供 draining/租约/逐项提交基础，尚不代替完整云资源管理服务。共享的持久化输入、准备缓存与控制面账本应位于不会随着抢占节点消失的位置。

## 9. 已完成的真实协议验收与下一步 GPU 验收

**双节点实际 HTTP 验收已完成**：控制面、两个真实 worker client、两个长期存活的 reference 子进程通过 loopback HTTP 传输文件，24 / 24 个目标最终完成，组成 8 个执行批次，记录 48 次带哈希的数据传输。两个 resident 各处理 12 个目标，各自 `modelInitCount=1`、`modelLoads=0`，同一进程 PID 跨多个目标与批次保持。原始记录见 [hybrid-http-verification.json](../artifacts/hybrid-http-verification.json)。这证明常驻进程、批次派发和真实文件传输已运行；所有目标均为公开结构回放，没有执行 AF3 GPU 权重，也没有实际连接阿里云机器。

**真实进程暂停与接管验收也已完成**：对 local-demo worker 执行 POSIX SIGSTOP，使其停止心跳及提交；控制面租约恢复后，未交付目标被 aliyun-demo 领取。18 / 18 个目标最终完成，其中 1 个已经接收的目标保留原 local worker / batch，2 个未交付目标由另一个实际 loopback 节点接管，新 attempt 的结果未被旧尝试覆盖。对应的 item、worker、batch 与 attempt 标识保存在 [hybrid-recovery-verification.json](../artifacts/hybrid-recovery-verification.json) 的 `preservedAccepted`、`recoveredUnsubmitted` 和 `lateAttemptCouldOverwrite` 字段。这是控制面与回放故障注入验证，不是云抢占时间或蛋白模型吞吐基准。

两组证据中的 `aliyun` 是管理员注册的位置标签，节点实际都在本机回环网络。它们证明协议支持位置分开的节点身份与恢复流程，不能据此宣称公网带宽、云端 GPU、驱动环境或真实模型计算已经测过。

浏览器回归的 [cluster-ui-verification.json](../artifacts/cluster-ui-verification.json) 与 `cluster-*-mock.png` 使用明确模拟的接口数据，只证明空状态、真实/演示标签、筛选刷新、哈希展开和移动端布局。它与上述真实 HTTP 验收是两组不同证据，模拟截图不替代集群执行记录。

部署到真实 AF3 GPU 后，下一步验收是：同一 PID 连续处理多个真实目标，modelLoads 为 1，首次新 shape 编译后后续兼容目标复用；输出序列与输入一致，采样/recycle 和模型版本没有漂移。记录每项 wall time、加载/编译/计算范围、GPU 型号与数量、峰值显存、传输字节和结果有效率，再与之前每目标启动进程的基线比较。实际 GPU 和阿里云环境的这些测量尚未执行。

Linux 服务使用 `KillMode=mixed`：先向 worker 主进程发送 SIGTERM 以停止领取并排空当前工作，超出 `TimeoutStopSec` 后再终止整个进程组。该时间应覆盖目标实际计算与上传所需时间；超时仍由逐目标租约恢复未提交工作。[systemd 官方说明](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html)

## 全局态势与目标排查（本轮更新）

`/api/cluster.pipeline` 是全账本的一致读快照，`counts` 和 `totalItems` 不受近期50批/100传输窗口限制。`queue.used` 只计算active preparing与可路由ready，暂停批次不占这个额度。`locations` 的资格检查针对active、未领取的ready，并使用与实际领取共用的条件；hybrid目标可能同时出现在两侧，不相加。busy组允许CPU预取，但不能同时再领取一个批次。`recent` 为最近300秒的持久事件，分别统计事件数和分类内去重目标数；回收仅计租约恢复事件，排队改回waiting不算故障恢复。

workers模式准备领取会跳过没有在线兼容节点的任务。旧的不可路由ready按最多500行迁回waiting_input，保留prepared descriptor和缓存，资源回来时重用缓存。这样先来的cloud-only任务不会占满本地可运行队列的准备水位；此策略也意味着没有在线兼容模型节点时不会继续新CPU准备。需要预先独立跑离线准备时，可使用已有分阶段准备脚本，之后由准备缓存复用。

界面增加可点击执行池、批次成员检查与跨页目标详情。目标详情按保留事件展示轨迹，不补写未记录时间；attempt按阶段领取累计，不是模型失败次数。全量/已完成/失败清单走`/api/campaigns/{id}/export`，每200行生成磁盘临时JSONL快照，结束读事务后单文件下载；不含完整输入序列、PAE、坐标或resultPath。

节点在收到匹配itemId和SHA-256的accepted回执后删除该target暂存树，同批其他目标不受影响；丢ACK继续以相同ZIP重传，不确定或失败件保留。未加入失败数据TTL或磁盘配额。实际OOM/MemoryError/RESOURCE_EXHAUSTED目标停止自动重试，提示改变输入/设置/硬件再手动重试；resident仍退休重启，批中尚未开始的目标可自动恢复，临时device lost等设备错误保留既有恢复策略。
