# MMseqs2 → AF3 两阶段搜索适配器

本项目现在有可执行的本地搜索适配器 `scripts/mmseqs_af3_runner.py`。它以 ColabFold 的本地 MMseqs2 搜索替换蛋白的 **unpaired MSA 来源**，再用 CPU 补齐 AF3 UniProt 原始配对候选与模板。输出仍是自包含的 AF3 单链 `*_data.json`，能进入现有链缓存、固定靶标组合和常驻推理管线。

本轮未安装搜索工具、序列数据库或 AF3，未执行真实 CPU/GPU 数据库搜索；已验证的是实际进程、参数、数据转换、缓存接口和失败行为。下面的版本是适配目标与管理员声明；`ready` 的文件检查不等于工具或 GPU 已通过实机验收。

## 1. 实际执行路径

```text
独立蛋白链 / 现有链缓存 miss
    ↓
search phase：ColabFold local search → 原始 unpaired A3M
    ├─ CPU：CUDA_VISIBLE_DEVICES=""
    └─ GPU：显式申请配置的搜索设备组，沿用节点共享设备锁
    ↓ 搜索子进程与后代退出，释放搜索 GPU 组
finalize phase：CPU
    ├─ native-uniprot：AF3 Jackhmmer → 原始 paired 候选
    ├─ native-af3：以新 unpaired MSA 构建模板搜索 → inline mmCIF
    └─ none：显式写 pairedMsa="" 或 templates=[]
    ↓
QC / SHA 与输入身份检查 → 原子写 protein_data.json
    ↓
链缓存发布 → 按原链 ID 组合完整目标 → 已有 AF3 推理阶段
```

一次适配调用只处理一个标准蛋白链。同一候选复合物的各链由管线独立准备并复用，组合时保留链身份；适配器不会把多个候选拼成一个复合物。它不接受已预配对的复杂 A3M、化学修饰或用户已有 MSA/模板后又覆盖这些字段。

AF3 原生实现把 UniProt 搜索结果作为配对候选，而 unpaired 来源是另一组数据库；部分 MSA 字段已填、另一部分仍未填时，直接运行完整原生 pipeline 并不能自动补缺。因此本适配器调用固定版本的公开 `msa.get_msa` 和模板 API，仅补所需残余阶段。[AF3 v3.0.4 pipeline](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/src/alphafold3/data/pipeline.py)、[MSA API](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/src/alphafold3/data/msa.py)

## 2. 搜索策略与科学含义

| 配置 | 本轮执行方式 | 对输入的实际影响 |
|---|---|---|
| `device=cpu` | ColabFold 本地 MMseqs2 CPU 搜索 | 以 UniRef30，可选环境库，生成 unpaired MSA |
| `device=gpu` | 同一搜索接口，启用 MMseqs2 GPU gapless 路径 | 需要 GPU 兼容的 ColabFold 展开数据库集；结果须独立质控 |
| `pairingStrategy=native-uniprot` | CPU AF3 Jackhmmer 查询 UniProt | 保留候选重复行与原生物种信息，供完整目标后续配对 |
| `pairingStrategy=none` | `pairedMsa=""` | 明确不提供同源配对候选，是输入消融选项 |
| `templateStrategy=native-af3` | CPU AF3 模板 API，输入为新 unpaired MSA | 模板搜索配置沿用适配目标；命中结果可能改变 |
| `templateStrategy=none` | `templates=[]` | 明确不使用模板，是输入消融选项 |

默认组合是 `native-uniprot` + `native-af3`。这些策略不保证与原生 AF3 搜索科学等价：搜索算法、数据库以及模板查询 MSA 都可能变化。判断是否可替代生产配置，需要在相同结构评估集上检查结果质量；A3M 行数、覆盖率或搜索速度本身不能代替结构评估。

`pairedMsa` 此时是每链的**原始配对候选**。最终配对发生在完整目标的 AF3 特征处理中；本适配器没有独立计算最终有效配对数。因此 QC 的 `finalPairedRows` 为 `null`。ColabFold 复合物模式已拼接/配对的 A3M 不会被直接拷贝到这里。ColabFold 官方本身提供 AF3 JSON 导出，但本适配器采用单链输出并自行保留提交的 ID、seed 与策略来源。[ColabFold v1.6.3 搜索实现](https://github.com/sokrypton/ColabFold/blob/v1.6.3/colabfold/mmseqs/search.py)、[AF3 输入格式](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/docs/input.md)

## 3. Profile 契约

数据库 registry 中使用 `validationProfile="af3-mmseqs-v1"`。以下为字段约定，路径由管理员指向已经存在的本地文件，示例不执行任何安装或下载。

```json
{
  "searchBackend": {
    "id": "mmseqs2-colabfold",
    "version": "18-8cc5c",
    "colabfoldVersion": "1.6.3",
    "adapterVersion": "mmseqs-af3-v1",
    "device": "gpu",
    "databaseFormat": "colabfold-gpu",
    "gpuServer": false,
    "pairingStrategy": "native-uniprot",
    "templateStrategy": "native-af3"
  },
  "searchParameters": {
    "threads": 8,
    "use_env": 1,
    "db_load_mode": 2,
    "jackhmmer_n_cpu": 8
  },
  "templateParameters": {
    "max_template_date": "2026-01-01"
  }
}
```

`max_template_date` 是评估/业务设定，示例日期不是推荐日期。运行时不替用户补成当天日期。

| 映射 | 必需项 |
|---|---|
| `executables`，始终 | `colabfold_search_binary`、`mmseqs_binary` |
| `paths`，始终 | `uniref30_db`；`use_env=1` 还需 `environment_db` |
| 任一 native 策略 | `executables.af3_python`，指向已安装 AF3 v3.0.4 及其数据组件的 Python |
| `native-uniprot` | `executables.jackhmmer_binary_path`、`paths.uniprot_cluster_annot_database_path` |
| `native-af3` | `executables.hmmsearch_binary_path`、`hmmbuild_binary_path`；`paths.seqres_database_path`、`pdb_database_path` |

路径均为绝对路径。当前 registry 的原生 UniProt 资产预检支持显式的单个 FASTA 文件。适配器 API 能处理原生 `path@N` shard 语法，并要求全库 `uniprot_cluster_annot_z_value`；这不代表 registry 已完成分片文件清单的完整预检，分片注册能力不在本轮交付范围。[AF3 Jackhmmer 分片语义](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/src/alphafold3/data/tools/jackhmmer.py)

可配置的 ColabFold 搜索参数为 `threads/use_env/db_load_mode/prefilter_mode/s/filter/expand_eval/align_eval/diff/qsc/max_accept`；CPU native 参数为 `jackhmmer_n_cpu/jackhmmer_max_parallel_shards/uniprot_cluster_annot_z_value`。其它参数直接拒绝，避免看似已设置、实际被忽略。GPU 配置拒绝 `s`，且只允许 gapless 的 `prefilter_mode=1`。ColabFold 自身的 `filter` 预设会覆盖部分底层对齐参数，选择参数时应依照固定版本实现；命令日志保存实际 argv，不能把声明值当成每层执行值。

Profile 的工具版本、数据库 release/指纹、参数、准备/收尾命令与策略都进入准备身份。修改搜索来源后不复用旧策略的缓存。

## 4. 两条真实命令与中间制品

Profile 的命令数组使用已有模板变量：

```json
{
  "prepareCommand": [
    "/absolute/project/.venv/bin/python",
    "/absolute/project/scripts/mmseqs_af3_runner.py",
    "--phase", "search",
    "--input", "{input_json}",
    "--profile-json", "{profile_json}",
    "--output-dir", "{output_dir}"
  ],
  "finalizeCommand": [
    "/absolute/project/.venv/bin/python",
    "/absolute/project/scripts/mmseqs_af3_runner.py",
    "--phase", "finalize",
    "--input", "{input_json}",
    "--profile-json", "{profile_json}",
    "--output-dir", "{output_dir}"
  ]
}
```

主脚本使用标准库和项目 MSA QC；只有 native 子阶段切换到 `executables.af3_python`。内部 `--phase native` 是 CPU helper，不是独立管线阶段。所有命令使用 argv 数组与 `shell=False`。

| 文件 | 含义 |
|---|---|
| `mmseqs-query.fasta` | 单链查询，固定内部 header `protein` |
| `mmseqs-search/protein.a3m` | ColabFold 真实输出，保留 UTF-8 内容及原换行 |
| `mmseqs-search.log` | 本地搜索进程日志 |
| `mmseqs-search-state.json` | 完成的搜索阶段身份、A3M SHA、QC、argv 与墙钟 |
| `native-enrichment.json` | native helper 的真实配对候选与 inline 模板结果 |
| `af3-native-enrichment.log` | CPU 原生残余阶段日志 |
| `msa-search-provenance.json` | 输入来源、策略、QC、分阶段时间和目标 API 版本 |
| `protein_data.json` | 最后原子发布的自包含 AF3 单链输入 |

Finalization 校验输入、profile 以及 A3M SHA。修改中间 A3M、换 query/profile、出现错误 query 或 alignment width 时拒绝发布。搜索必须在新的 attempt 输出目录开始，避免 MMseqs2 依据旧中间文件跳过实际搜索。已有最终文件也不会被静默覆盖。每个源码/JSON 文件限 128 MiB，超限明确失败。

`msa-search-provenance.json` 中的 `colabfoldSearchWallSeconds` 包含启动、数据库加载、搜索、展开及 A3M 输出；`finalizeWallSeconds` 包括 native 子进程和组装开销。它们是某条缓存首次生成的墙钟，不是纯 GPU kernel 时间，也不是后续缓存命中的本次搜索时间。`af3ApiTargetVersion` 明确表示代码适配目标，不冒充实机版本探测结果。

## 5. GPU 数据库与设备生命周期

普通 FASTA 或仅生成的通用 padded sequence DB，不能替代 ColabFold 所需的 expandable 数据库集；其搜索后还要展开簇内序列、重新对齐并生成 MSA。ColabFold 官方准备脚本区分 GPU 构建路径与兼容 CPU/GPU 的预构建数据库，且提醒数据库排序变化可能影响结果。本项目的后缀存在检查只验证最低资产完整性，不通过文件名声称已经验证 GPU padded 格式。[ColabFold v1.6.3 数据库脚本](https://github.com/sokrypton/ColabFold/blob/v1.6.3/setup_databases.sh)

GPU 搜索要求调度器显式提供 `CUDA_VISIBLE_DEVICES`，为空时直接拒绝，不能默认占用 GPU 0。托管运行时，CF 与真正 MMseqs 子进程留在 engine 的进程组；engine 在包装进程退出、超时或取消后清理整组，再释放设备租约。直接 CLI 调用会建立独立子进程组并回收其后代。UniProt 与模板阶段始终设置 `CUDA_VISIBLE_DEVICES=""`、`JAX_PLATFORMS=cpu`。

**本轮拒绝 `gpuServer=true`。** 官方 `gpuserver` 能令数据库常驻，但服务会在一个查询完成后继续占显存；仅锁定查询的本项目租约不能覆盖这个寿命。后续若加入托管服务，必须把整个 daemon 生命周期纳入独占设备组及故障回收。不能在当前管线偷偷启动一个不受管的常驻进程。[MMseqs2 GPU 文档](https://github.com/soedinglab/MMseqs2/wiki#gpu-accelerated-search)、[Release 18 的 gpuserver 接口变更](https://github.com/soedinglab/MMseqs2/releases/tag/18-8cc5c)

## 6. 本轮验证与仍需真实测量的内容

`backend/tests/test_mmseqs_af3_runner.py` 使用实际 Python 子进程和明确标识的假搜索/假原生 API，覆盖：两阶段执行、CRLF A3M 原样保留、ID/seed 保留、CPU/GPU 参数、native 候选重复行、模板索引映射、中间文件身份、错误输出、超时/取消后代回收、托管进程组与锁 FD 继承、显式空策略、非法科学字段拒绝。这些不是结构预测精度测试，也没有执行真实同源搜索。

真实启用后需要分别记录：工具与数据库版本、GPU 型号/设备数、query 长度、冷热缓存、搜索与 CPU 残余墙钟、搜索 GPU 占用，以及对应的 MSA QC 和结构评估结果。固定靶标筛选中还应同时记录独立链数、实际新搜索数和链缓存命中数；完整复合物数量不能直接当成搜索调用次数。业界 GPU 搜索论文支持探索这个方向，但论文中其他模型/数据库/硬件的倍数不能直接作为这条 AF3 混合路径的收益。[GPU-accelerated homology search with MMseqs2，2025-09-18](https://www.nature.com/articles/s41592-025-02819-8)
