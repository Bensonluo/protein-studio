# AF3 链级 MSA 与模板复用设计

核查日期：2026-10-23。原始实现固定为 AlphaFold 3 `v3.0.4`，源提交 `85c4d20505fd5cef05eac22b534d4e793971ae69`。本文记录当前 `backend/staged.py` 已实现的链缓存、官方机制及后续原生验收。链复用与原有 staged 回归共 33 项在 Python 3.12.13 下通过，使用真实 CPU 子进程执行合成搜索替身；没有执行真实数据库搜索或 GPU 推理。

## 结论与收益口径

固定靶标与大量候选组合时，应缓存每条蛋白链的原始搜索结果，再为每个完整复合物组装输入。AF3 官方明确给出了从 `n + m` 次单体准备生成 `n × m` 个组合的做法，允许复制的三个字段是 `unpairedMsa`、`pairedMsa`、`templates`。CPU data pipeline 完成这些搜索，不完成模型的 featurisation。[官方 performance 指南](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/docs/performance.md)

以下计数是工程推导，不是速度基准：一个固定链 T 与 N 条互不相同、也均不同于 T 的候选链组合，未经跨任务缓存的朴素实现最多重复执行 2N 次链搜索；精确链缓存只需要 N+1 次。实际节约的 CPU 时间取决于 T 与候选各自的搜索成本。仍然有 N 次完整结构推理，不能由搜索次数直接推出 GPU 时间降低的百分比。候选之间只有相似性、存在一个残基突变，仍视为不同搜索输入。

更通用地，搜索次数由同一准备配置下的“唯一搜索身份数 U”决定。复合物可以有两条以上的链，例如固定双链靶标加候选；相同链重复拷贝也只搜索一次，但每个拷贝仍必须出现在最终复合物输入中。

## 为什么原始 pairedMsa 可以复用

官方 `data/pipeline.py` 的蛋白搜索函数只依赖本链序列和搜索配置，没有搭档链参数。unpaired 输入合并多个数据库结果；paired 输入来自 UniProt，保留重复行；模板搜索使用本链的 unpaired 输入。三者一起形成可持久化的原始搜索制品。[官方数据准备实现](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/src/alphafold3/data/pipeline.py)

名称 `pairedMsa` 容易误导：这里保存的是本链“用于随后配对的原料”，并不是已经针对某个搭档完成配对的数组。官方 `msa_pairing.py` 在多个链的 feature 中按物种组织匹配；同物种候选还受原始行位置、跨链出现情况和数量限制影响，缺少对应链时用 gap。不能把另一组复合物已完成配对的数组直接移植。[官方配对实现](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/src/alphafold3/model/msa_pairing.py)

工程约束如下：

- 将原始 A3M 字符串作为不可变内容保存，连同 header、序列顺序、重复行、小写插入字符一起保留。
- 组装器只分配原始字段，不按行拼接两个字符串，不自行选择“最佳同物种 hit”。
- 更换搭档之后，完整复合物仍进入 AF3 原生 featurisation。配对、去重、裁剪、token 映射由该次完整输入重新生成。
- 同一靶标第一次与候选 A 组合、第二次与候选 B 组合，可以共享靶标搜索结果，但不得共享前一次复合物的 `msa_all_seq`、pair representation、结构坐标或置信度。

原始 MSA 到 feature 的转换会从 description/header 提取物种信息。因此“清理文件名时顺手删除 header”会改变后续配对含义；把插入的小写字母直接改成大写也会改变对齐含义。[官方 MSA 容器及 featurize](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/src/alphafold3/data/msa.py)

官方 `features.MSA.compute_features` 依据完整输入中的蛋白链决定是否配对；只有一个独特蛋白序列的体系走不同条件分支。因而不能为了节省单体文件大小，删除“单体推理暂时不用”的原始 pairedMsa：它之后加入异源复合物可能需要使用。[官方 MSA feature 计算](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/src/alphafold3/model/features.py)

## 字段处理规则

| 内容 | 链缓存处理 | 组装处理 |
|---|---|---|
| 标准蛋白 `sequence` | 精确身份的一部分，记录 SHA256 | 与当前目标链逐字符匹配 |
| `unpairedMsa` | 原始文本、内容散列 | 深拷贝，不改行或 header |
| `pairedMsa` | 原始文本、内容散列，不能去重 | 深拷贝，交由 native 重新配对 |
| `templates` | 完整列表、mmCIF 文本及 residue mapping | 深拷贝完整条目 |
| 目标 `id` | 首版规范成单链 A；不进入搜索 key | 替换成当前目标真实 ID |
| `modelSeeds`、`name` | 规范单链准备使用 `[1]` / `protein` | 组装制品仍使用规范值；预测阶段写入当前目标 seeds |
| 采样数、recycles、权重 | 不决定本版原始搜索缓存身份 | 继续由完整预测任务及 resident signature 管理 |
| 已成对 features、bucket、预测结果 | 不存入此类链缓存 | 每个完整目标重新计算 |

模板的 `queryIndices` 指向本链序列，而 `templateIndices` 指向模板自己的残基。模板 mmCIF 是一个独立结构；不能因为目标链从 A 改成 D，就全局替换 mmCIF 内的 A。对目标链 ID 的修改只发生在外层 `protein.id`。相同序列上沿用同一个模板映射是允许的；截短、插入或突变后不能仅修改 query header 假装仍是同一输入。

## 重复链 ID 展开必须精确

官方 `Input.to_json` 会按 `hash_without_id()` 合并内容一致的链。蛋白的这个身份包含序列、修饰、description、两份 MSA 和 templates；并非仅比较序列。合并项的 `id` 可以是数组。[官方输入对象、hash_without_id 与 to_json](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/src/alphafold3/common/folding_input.py)

例如用户顺序是 A、B、C，A/C 的内容一致，准备结果可能把 A/C 放在一个 `id:["A","C"]` 项，后面才是 B。验证应展开为 ID→内容映射，检查无重复 ID、无缺链、无额外链；不要将准备结果的第 2 个 JSON 项强行对应输入的 B 或 C。

当前 `_assemble_af3` 按用户目标顺序输出一项一个 ID，相同搜索制品使用独立深拷贝。它保留单链准备 JSON 的版本，并拒绝不同版本的单链结果混装。随后 native 可以自己规范化。链数、化学计量、真实 ID 都保留；不会把两个相同链“去重”为一个分子。

## 首版支持范围与必须保持的输入语义

当前产品输入为标准二十种氨基酸、每链 `id + sequence`、全部 protein 实体。`engine.validate_submission` 规范化这些字段；`staged._prepare_af3_chain` 调用管理员配置的单链准备命令。官方准备器执行自动 MSA/模板搜索，输出中的显式空 MSA 会原样保留。这一范围解决固定蛋白靶标 × 大量蛋白候选的重复搜索。

以下情况不自动套用首版链缓存：

1. **人工预配对 MSA。** 官方自定义模式可将人工对齐的块放进 unpairedMsa，并置 pairedMsa 为空，配合 `resolve_msa_overlaps=false`；其跨链行关系必须整体保留。[官方自定义 MSA 与 pairing 说明](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/docs/input.md) 当前 `scripts/resident_runner.py` 固定 `resolve_msa_overlaps=True`，因此不能宣称已经支持该模式，也不能把它当独立链制品拆开。
2. **用户提供自定义模板/MSA 或禁用搜索。** `null`/缺省、空字符串、空列表表达不同意图，不能统一成“没有数据，自动搜索”。将来支持时，应把规范化意图与内容 digest 纳入 key；不应只按 sequence 命中自动搜索结果。
3. **PTM、自定义 CCD、配体、核酸、共价键。** 这些不是“AF3 一律不能复用”，而是本项目当前输入空间尚未表达全部语义。`_validate_af3_leaf` 会拒绝准备器在标准蛋白任务中注入非空 modifications、bondedAtomPairs 或 userCCD；非 protein 实体也不能进入。后续支持时要保留顶层完整输入，并核查修饰后的实际搜索序列、键的链 ID 引用和所有化学身份。
4. **其他模型和预处理工具。** AF2、ColabFold、Boltz、Protenix 的原始制品格式、配对策略和模板处理不能因为也叫 MSA 就共享此缓存 namespace。ColabFold 的复合物 A3M 包含自身的配对/化学计量约定，不能在本轮直接拆成 AF3 的三字段。
5. **近似序列复用。** 用野生型 MSA 加速大量突变体属于另一项科学方法，需要重新构造 query、正确处理 alignment/mapping，并单独评估质量。本轮精确缓存不做近似匹配。

当前链输入契约只处理 id/sequence；API 已在规范化之前拒绝额外链字段和 native 化学/自定义顶层字段，防止用户提交的 MSA、模板或修饰被静默丢弃。完整 native AF3 高级输入需要独立适配。

## 当前准备阶段的执行路径

`backend/staged.py` 保留了整目标 key、singleflight、SHA256 清单、取消检查、临时目录与原子发布；AF3 整目标缓存 miss 后已调用 `_assemble_af3`，再由 `_prepare_af3_chain` 管理链层。

```text
prepare_stage
  ├─ 完整目标缓存命中 → 原路径返回
  └─ 未命中
      ├─ 校验首版链复用适用范围
      ├─ 为每个唯一 chain key：
      │   ├─ 链制品命中 → 校验 manifest + 字段
      │   └─ 未命中 → 规范单链输入 → 官方 CPU prepare → 校验 → 原子发布
      ├─ 按当前目标链 ID 深拷贝三字段，组装完整 *_data.json
      ├─ _validate_af3(assembled, requested_chains)
      └─ 原有整目标 COMPLETE.json + 原子发布
```

`prepare_stage` 的 AF3 分支现在直接调用 `_assemble_af3`。reference、ColabFold、GPU 租约和 resident 模型路径保持各自实现。每个链 miss 使用当前管理员准备命令、规范单链 A 输入和同一数据库 profile；输出要求恰好一个完整、内联、无外部路径、只有一个规范 protein 实体的准备 JSON。

单链缓存目录为 `cache/af3-chain-v1/<key>/`。不同目标工作线程竞争同一条靶标时使用同一个 per-chain 文件锁；命中前检查完整标志与各文件 SHA。一次只持有一个链锁，完成后释放，不同时持有多条链锁。调用方向是完整目标锁→单链锁，单链函数不递归调用 `prepare_stage`。现有 flock 是同一可见文件系统上的进程协调，不代表已经提供跨多个独立 CPU 节点的分布式锁。

取消时，未完成单链临时目录不发布；已经完成的单链制品可以保留，供其他目标使用。一个目标取消不能删除他人已命中的不可变链缓存。整目标组装失败也不需要把成功的单链结果全部回滚。

## 当前缓存 key 与可追溯性

当前单链 key 的算法为：先将任务规范化成唯一链 `A`、相同标准序列，求已有 `prepare_cache_key(canonical_job)`，再加入独立 namespace：

```text
chain_key = SHA256("af3-chain-v1" + NUL + prepare_cache_key(canonical_job))
```

内层身份包含 schema、engine、规范链序列、prepareVersion、profileFingerprint、searchVersion、searchParameters、templateParameters、executionProfile.preparation、prepareCommand，以及存在时的 executables；采用排序键 JSON 的 SHA256。源代码提交号不是一个单独强制字段，应通过 prepareVersion/搜索实现版本及管理员 profile 声明准确绑定。替换同路径程序而不更新版本声明，不会自动产生新身份。

原目标 ID、搭档、campaign ID、请求 seed/sample/recycle 不影响单链 key。模板数据库 release 和 `max_template_date`、MSA 数据库 release、搜索工具版本、搜索参数的身份变化会失效。执行权重变化本身不需要重跑相同原始搜索；原始准备实现变化则应更新版本声明。

整目标 key 保持原算法，以兼容已有完整准备制品。旧缓存已自包含时无需为了获取链统计重新运行搜索；没有 `chainReuse` 的旧 manifest 原样复用，不伪造历史命中情况。

当前数据库 profileFingerprint 包含快速元数据与声明版本；目录不会递归全量散列 TB 数据。运营上必须将 release 视为不可变快照；在原路径悄悄替换内部数据库、却不更新 release/manifest，不应期待缓存自动检测所有变化。可将发布时生成的数据库 manifest digest 纳入 profile，避免每次任务重新读取全库。

新组装的整目标 manifest 和 descriptor 含 `chainReuse`，当前字段如下：

| 字段 | 准确含义 |
|---|---|
| `strategy` | 固定为 `af3-chain-reuse-v1` |
| `totalChains` | 当前完整目标的链拷贝数 |
| `uniqueChains` | 当前目标中不同标准序列数 |
| `newChainSearches` | 本次准备调用新执行的唯一链准备数 |
| `chainCacheHits` | 本次准备调用实际访问并命中的唯一链缓存数 |
| `duplicateChainCopies` | `totalChains - uniqueChains`，目标内部省去的重复拷贝搜索 |
| `wholeTargetCacheHit` | 此次是否直接命中完整目标缓存 |
| `entries[]` | 每个唯一序列的 `chainIds`、`sequenceSha256`、`cacheKey`、`cacheHit`、`residueCount` |

`entries[].cacheHit` 记录该完整制品最初组装时的来源，属于不可变 provenance。之后直接命中整目标时，返回 descriptor 将操作计数 `newChainSearches=0`、`chainCacheHits=0`、`wholeTargetCacheHit=true`，不访问链层、不改变原 manifest 或 entries。零个链命中表示这次根本不必查链缓存，并不表示搜索重新执行。

因此 2×3 个不同二链组合共有 12 次逐目标去重链访问，首次构建需 5 次搜索、7 次链命中；同一批再准备一遍得到 6 次整目标命中、0 次新搜索、0 次链查找命中。这个区分应保留在看板和 ledger 中。完整制品仍通过 artifacts 中的 SHA256 清单校验，不依赖 entries 找到原来的链目录。

账本 `preparationReuse` 汇总各目标最近一次成功发布的准备记录，`uniqueChainOccurrences` 是逐目标去重链数之和，不能作为全批次唯一序列数。失败或取消的尝试没有成功 descriptor，不计入这些计数；其已执行搜索记录留在尝试日志。

链缓存减少搜索，并不自动消除所有磁盘重复。若固定链三字段合计为 S 字节，自包含的 N 份组合 JSON 仍可重复保存约 N×S 字节，再叠加任务输出中的准备文件。当前每次链命中也会读取并校验内容 SHA、解析 JSON；大量命中的性能仍受这些字节处理影响，不能将 CPU 搜索消失等同于准备阶段零成本。百万目标应分别统计“唯一链制品占用”和“待发送/保留的完整目标占用”，并在任务完成后按现有确认机制回收工作暂存。当前整目标准备包不包含整个链缓存目录，移除原控制端缓存后仍可在远端读取。后续按内容块传输或共享只读制品需要另行设计引用解析与生命周期。

## 验证与完成标准

### 已有回归与后续原生验收

`backend/tests/test_chain_reuse.py` 的 16 项测试与 `backend/tests/test_staged.py` 的 17 项回归已通过。前者使用可控制失败、取消和计数的实际 Python 子进程，覆盖两个目标共享靶标、2×3 组合、重复链和 ID 映射、采样/配置身份、线程与跨进程 singleflight、失败重试、取消、缓存损坏、旧整目标缓存、迁移包、异常准备输出和显式空 MSA。运行命令为：

```bash
/Users/luopeng/.pyenv/versions/3.12.13/bin/python3.12 -m unittest backend.tests.test_chain_reuse backend.tests.test_staged
```

下表是完整验收要求；其中需要真实 native 数据语义的项目仍不能由合成搜索替身代替：

| 测试 | 必须观察到的结果 |
|---|---|
| T+A、T+B 两个目标 | 首次各搜索 T/A/B 一次；T 第二次命中 |
| 同序列不同目标 ID | 复用搜索，组装后的 ID 分别正确 |
| 同一目标 A/B/A 的三个拷贝 | 一个 A 的搜索身份，三个完整链 ID 均存在 |
| 官方分组输出 `id:[A,C]` | 展开后识别两个链，不按数组位置误映射 |
| 仅改 seed/sample/recycle | 不重搜，预测任务设置仍更新 |
| 改序列、DB release、模板日期或工具版本 | 单链缓存 miss |
| 两线程同时请求同一靶标 | 同一个链身份只执行一次准备命令 |
| 搜索途中取消/失败 | 无 COMPLETE；后续重试可正常计算 |
| 缓存内容损坏或残留临时目录 | 不作为命中；有明确错误/恢复路径 |
| MSA 有相同序列不同物种 header | 原始文本逐字保留，不能自行 dedup |
| 模板有非 A 的内部链名 | 组装只改外层 ID，mmCIF digest 不变 |
| 准备器注入不支持的化学字段 | 拒绝发布链缓存 |

测试替身可以验证调用次数、缓存原子性和 JSON 组装，但不能证明生物特征正确。原生 AF3 环境还应做下一层测试：把固定的已准备原始制品分别走“原输入”与“缓存组装输入”，使用相同完整链顺序、seeds、CCD、bucket 和配置调用 native featurisation，比较 MSA/deletion/template/token 等关键 feature。该层无需加载模型权重或执行 GPU 推理，仍需安装 AF3 原生依赖。

原生 featurisation 本身要求准备字段已经齐全，再依据完整折叠输入产生特征；因此应将 native 输入解析与 feature 等价检查作为组装机制的科学契约测试。[官方 featurisation 入口](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/src/alphafold3/data/featurisation.py)

最后再在真实环境抽取异源二聚体、同源多聚体、多链复合物，比较直连准备与缓存组装的输入、完成样本数、质量字段和结果。相同条件下出现差异时先定位字段/feature 差异，不能用“GPU 非确定性”解释所有偏差。本次审查没有执行这一原生验收，也没有生成任何推理加速百分比。

## 当前审查结果与尚未宣称的能力

- `staged._validate_af3` 按 chain ID→sequence 校验；`_validate_af3_leaf` 进一步要求唯一规范实体、受支持版本和无额外化学字段，避免错误制品进入共享链缓存。
- `_validate_chain_msa` 已验证 query 与标准序列一致、去除小写插入后的行宽以及基础 A3M 格式，原始文本不被重写，显式空字符串保留。它不等价于 native 模板解析、物种 ID 识别或 feature 等价验收，这些仍需原生 AF3 环境验证。
- 模型特征仍在 resident 对每个完整目标生成。链缓存降低重复搜索；模型热驻降低权重加载与可复用编译开销。这两项是不同优化，记录耗时和命中率时分别呈现。
