# CPU 搜索准备 → 持久制品 → GPU 预测

本页说明 CPU 数据准备和默认 `legacy` 预测 helper，接通管理员已经安装的 AlphaFold 3 或 ColabFold。AF3 的新 `workers` 模式复用相同准备产物，改由原生常驻模型执行，见 [混合云管线](hybrid-resident-pipeline.md) 与 [resident 引擎](resident-engine.md)。本机没有因此安装模型、下载数据库，也没有声称已执行真实 GPU 预测。真实进程测试使用明确的 synthetic test executable，公开 reference 使用原有实验结构或 AFDB 历史结果。

## 1. 已实现的接口与阶段含义

`backend.staged.prepare_stage(job, work_dir, emit, cancelled)` 运行 CPU 准备并返回 JSON descriptor；`predict_stage(job, prepared, work_dir, emit, cancelled)` 消费 descriptor 并返回与单任务引擎相同的结果。`prepare_cache_key(job)` 可在计划时计算目标准备键；`staged_capabilities()` 报告阶段命令是否已配置。

| 引擎 | CPU 准备 | 预测阶段 | 当前状态 |
|---|---|---|---|
| AF3 | 官方 data pipeline 搜索 MSA/模板，写自包含 `*_data.json` | 读取 prepared JSON，跳过数据库搜索；进行 featurisation、模型计算和输出 | 两阶段可执行 |
| ColabFold | 本地 `colabfold_search`，写 target A3M，启用本地模板时还写 `.m8` | `colabfold_batch` 读 A3M；仍包含模型输入组装及可选本地模板处理 | 两阶段可执行 |
| AF2 | 使用原有 `af2_runner.py` 的完整流程 | 未拆分 | 单任务 monolithic；不在 staged campaign 中冒充两阶段 |
| reference | 校验并缓存已公开结构 | 读取公开结构 | 无数据库搜索、无新模型预测 |

AF3 的 `--run_inference=false` 和 `--run_data_pipeline=false` 是官方接口；准备 JSON 包含搜索结果，但**没有提前完成所有模型 featurisation**。官方代码在预测路径才进行相应转换。固定版本也提供目录输入功能。[AF3 v3.0.4 执行代码](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/run_alphafold.py)

ColabFold 的本地搜索与结构预测是两个独立命令；本实现不使用公共 MSA 服务。官方输入代码规定多链 A3M 的 unique-chain 长度和拷贝数元数据，本实现校验该元数据与查询序列。[ColabFold 官方说明](https://github.com/sokrypton/ColabFold/blob/main/README.md)、[A3M 输入实现](https://github.com/sokrypton/ColabFold/blob/main/colabfold/input.py)

## 2. 配置并实际导出环境

先将 `config/databases.example.json` 复制为 `config/databases.local.json`，填写已经安装的资产路径、数据库 snapshot、prepareVersion、searchVersion，移除所用 profile 的 `example:true`。不要把数据库版本占位字符串当真实版本。AF3 profile 与 ColabFold profile 的预检必须 `ready=true`。

再复制 `config/staged-engines.example.json` 为 `config/staged-engines.local.json`，修改实际 Python 环境、项目路径、模型权重路径与工具路径。JSON 本身不会被服务器自动加载：

```bash
cd /Users/luopeng/Documents/GitHub/protein-studio
python3 scripts/staged_env.py --config config/staged-engines.local.json --profile af3-local-staged
```

上面只打印可检查的 `export` 命令。检查后在将要启动服务器的同一个 shell 中应用：

```bash
eval "$(python3 scripts/staged_env.py --config config/staged-engines.local.json --profile af3-local-staged)"
python3 -m backend.server
```

ColabFold 改用 `--profile colabfold-local-staged`。只有 PREPARE/PREDICT 环境变量也能启用对应 campaign；不用另外伪配一个 monolithic `*_COMMAND`。单任务旧页面是否可用仍由旧接口自己的运行器设置决定。

legacy 默认配置为一个 GPU worker 和 `[["0"]]`。在真实十六卡节点，可以将 GPU workers 设置为 16，并将 GPU group 列表配置为十六个单卡组。CPU workers 根据搜索工具线程数、可用内存和数据库 I/O 能力设置，不能只按 GPU 数复制。组内分配多卡不自动让 AF3/ColabFold 成为多卡模型。

命令数组作为 argv 执行，不经过 shell。配置里的 `{profile_json}` 是 worker 写出的已验证数据库 profile；用户提交不能覆盖任意命令或本地路径。`staged_env.py` 对 shell 值使用 `shlex.quote`，支持项目路径中含空格。

## 3. 脚本如何映射真实参数

`af3_staged_runner.py`：

- `--stage prepare --input <input.json> --output-dir <dir> --profile-json <profile.json> --alphafold-script <installed/run_alphafold.py>`；设置 `run_data_pipeline=true`、`run_inference=false`，传入 profile 的数据库路径、搜索线程与模板日期。
- `--stage predict --input <prepared_data.json> --output-dir <dir> --profile-json <profile.json> --job-json <job.json> --alphafold-script <...>`；设置 `run_data_pipeline=false`、`run_inference=true`，正确传 `num_diffusion_samples` 与 `num_recycles`。
- `--` 后允许管理员增加模型路径、编译缓存、attention 实现等安装参数；不能覆盖脚本管理的阶段与采样参数。
- 示例按 v3.0.4 核查。升级应先更新 profile 的 prepareVersion，复核官方参数，再跑小规模真实 GPU 验收。

AF3 的模板序列路径是 `seqres_database_path`，不是 AF2 的 `pdb_seqres_database_path`。此外，v3.0.4 原生 CLI 初始化会检查 RNA 数据库路径，即使输入是蛋白；registry 必须覆盖它实际要求的文件。本项目没有用空文件或假的路径掩盖这一要求。

`colabfold_staged_runner.py`：

- prepare 调用已安装的 `colabfold_search <fasta> <dbroot> <output>`，将 `uniref30_db`、`environment_db` 映射为 `--db1`、`--db3`，明确固定 `--gpu 0 --gpu-server 0 --unpack 1`。
- predict 调用 `colabfold_batch <prepared-a3m-dir> <output>`，每个 seed 一次，映射 `--num-models`、`--num-seeds 1`、`--random-seed`、`--num-recycle`。这里 numSamples 是使用的模型数，不能当 AF3 扩散样本数解释。
- 默认不启用模板。若 `use_templates=true`，需登记 `template_db` 和 `local_pdb_path`，并产生对应 `.m8`；预测传本地 hit 文件和 PDB 镜像。`max_template_date`、`max_template_hits` 作为白名单参数映射，不允许静默忽略拼写错误或转向公网服务。
- ColabFold 模型权重路径通过 helper 的 `--model-dir` 显式传给官方 `--data`；示例使用 `/srv/protein-models/colabfold`。这是预先安装的模型目录，数据库 ready 检查不等于权重已经完整。搜索工具使用 `searchParameters.mmseqs` 中的绝对路径；只指定虚拟环境 Python 并不会自动激活它的 shell PATH。

模板参数与本地路径开关可在当前官方 parser 中核查。[ColabFold 搜索 CLI](https://github.com/sokrypton/ColabFold/blob/main/colabfold/mmseqs/search.py)、[ColabFold 预测 CLI](https://github.com/sokrypton/ColabFold/blob/main/colabfold/batch.py)

## 4. 缓存的准确粒度与故障语义

现有**整目标准备缓存**继续保留。键覆盖按顺序排列的全部 chain ID 与序列、引擎、准备实现版本、数据库指纹、搜索版本、MSA/配对与模板参数、管理员准备命令。seed、numSamples、numRecycles 不影响准备键；AF3 缓存 JSON 使用固定准备 seed，预测时只修改本任务副本的 modelSeeds。

两个相同目标同时准备时，一个持有 POSIX `flock`，另一个等待；等待可取消。产物先写同文件系统临时目录，校验结构化输入与 SHA256，最后写完整 manifest 并 rename 发布。失败和取消不发布完成标记。读取缓存时验证 manifest、文件集合、大小与 SHA256，不把存在一个目录视为已完成。进程崩溃可能留下 `.partial-*`，它们不会命中缓存；维护时可以在确认没有准备 worker 使用后清理。

这是本地 POSIX 文件系统的多进程 singleflight，不是跨云分布式锁服务。跨节点共享缓存需要明确验证共享文件系统锁语义，或改成具备条件写入与 fencing 的对象存储缓存协议。

数据库的 profileFingerprint 是声明的 release、参数和显式资产元数据指纹，不是整个 TB 级数据库的内容哈希。应使用不可变 release 目录，升级时更新登记信息；不能依赖修改目录内某个文件就一定被非递归元数据预检发现。

AF3 自动准备新增**独立链缓存**：A+B 与 A+C 保留不同整目标键，但共享 A 的精确搜索身份。单链使用规范 ID A 准备；组装时深拷贝原始两份 MSA、模板及映射，并还原完整目标链 ID/拷贝数。`pairedMsa` 保持原始物种信息，完整复合物由 native featurisation 重新配对。首次实现仅接收标准蛋白 id/sequence 与管理员自动搜索，其他模型保持原路径。[官方复用方案](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/docs/performance.md)、[本项目机制与计数](chain-reuse-design.md)

预测 work_dir 会保存完整 prepared 原始制品副本、执行账本与模型输出，可下载追溯。复制自包含制品也防止外部程序改坏共享缓存。代价是缓存命中仍有磁盘读取、哈希验证和制品复制，容量压测应计入这一部分。

准备线程现在可在领取目标前按显式CPU/内存/磁盘估计预算等待；它与ready水位共同生效。等待期间不启动搜索、不增加attempt。配置与口径见[准备资源准入](screening-and-preparation.md#准备并发为什么需要第二道准入)。

## 5. legacy 与原生常驻 workers

legacy scheduler 每个目标调用模型进程，因此仍会重复模型初始化；JAX 编译缓存仅复用可缓存的编译产物，不等于权重驻留 GPU。ColabFold 当前继续采用这一路径。

AF3 已新增 `workers` 模式：CPU 准备生成的自包含 JSON/模板产物传给认证节点，`scripts/resident_runner.py` 在一个长期进程中创建并复用官方 `ModelRunner`、加载一次权重，跨多个兼容目标与批次继续执行。`backend/resident.py` 保留进程生命周期、逐项完成事件、异常后的显式重启与部分完成记录。参见 [原生 resident 契约](resident-engine.md)。

这条路径已包含 batch 成员账本、带 SHA-256 的 HTTP 文件传输、逐项目标租约、结果校验发布与失联恢复。它是多个目标在同一 resident 中连续执行，不是未经模型支持的张量 minibatch。新 shape 仍可能首次编译；模型版本、采样与 recycle 兼容性必须匹配，不能为了复用改变科学设置。

官方 AF3 目录输入和 ColabFold A3M 目录输入仍可用于管理员的独立批处理，但当前常驻能力不是简单把目录交给原 CLI；ColabFold 的加载和 MSA 维度生命周期尚未按原生 resident 接入验证，不能说它已经共享这项能力。已有本地/远端 worker 协议同样不意味着已实现跨机模型并行或云实例自动创建。

启动、16 卡 fleet、失败 slot 的有界退避重启、systemd 托管与双节点真实协议证据见 [混合云常驻部署](hybrid-resident-pipeline.md)。

## 6. 验证

```bash
PYTHONPYCACHEPREFIX=/tmp/protein-studio-pycache python3 -m unittest backend.tests.test_staged -v
```

覆盖实际 subprocess、并发/跨进程 singleflight、重新启动后缓存命中、失败/取消不发布、DB 指纹变化失效、AF3 seed 重写与样本覆盖、ColabFold A3M 多链/同源拷贝/非配对块校验、公开结构完整 replay。正式接入数据库后仍需真实小样本验证实际安装版本、权重、设备、数据库内容和模型科学结果；这些没有用 fake executable 测试冒充。
