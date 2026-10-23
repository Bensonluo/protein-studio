# AF3 v3.0.4：真实数据库与 CPU 准备验收

这里固定的是官方 AF3 **v3.0.4，提交 `85c4d20505fd5cef05eac22b534d4e793971ae69`**，与本项目生成器使用的原生 AF3 路径配套。提交来自对官方仓库执行的只读 `git ls-remote` 查询；探针也会核对完整提交。其他版本和数据库分片方案不能直接沿用这份验收结论。下列安装、下载和真实搜索命令应在 Linux CPU 准备服务器执行；本开发机没有安装这些数据库，也没有完成真实 AF3 搜索或 GPU 推理。

## 1. 本地数据库服务实际提供什么

CPU 准备服务从目标序列生成 MSA 和结构模板。生成的 `*_data.json` 内嵌每条链的 `unpairedMsa`、`pairedMsa` 与 `templates[].mmcif`，随后作为准备包的一部分传给 GPU worker。GPU 端做特征化和模型推理，不需要读取 CPU 主机的几百 GB 序列库，也不需要访问控制器路径。官方支持用两个 stage flag 分开执行这两个阶段。[官方分阶段说明](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/docs/performance.md#running-the-pipeline-in-stages)

这里的当前输入范围是普通蛋白链。完整 AF3 软件支持更广的分子类型，但本平台没有因此自动获得 RNA、DNA、配体输入的前端和任务校验能力。即使本次只提交蛋白，**原版 CLI 创建 `DataPipelineConfig` 时也会解析 RNA 数据库路径**，所以九项资产必须完整，不能用空文件或几条序列占位。[官方 CLI 路径初始化](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/run_alphafold.py)

## 2. 固定运行环境和搜索工具

以下是面向 Ubuntu 24.04 / Python 3.12 的原生安装步骤；路径统一为源码 `/opt/af3`、Python `/opt/af3/venv/bin/python`、工具 `/hmmer/bin`、本项目 `/srv/foldspace`。已有经过验证的同版本容器也可用，但探针必须在**实际运行搜索的同一个容器/用户/挂载视图**中执行，不能在宿主机检查一个路径，再让另一个容器读取不同文件。

```bash
sudo apt-get update
sudo apt-get install -y git wget curl ca-certificates gcc g++ make patch zlib1g-dev zstd python3.12 python3.12-dev python3.12-venv
sudo install -d -o "$(id -un)" -g "$(id -gn)" /opt/af3 /hmmer /srv/af3-evidence /srv/protein-databases/af3-v3.0-snapshot
git clone --branch v3.0.4 --depth 1 https://github.com/google-deepmind/alphafold3.git /opt/af3
test "$(git -C /opt/af3 rev-parse HEAD)" = 85c4d20505fd5cef05eac22b534d4e793971ae69
```

官方 Dockerfile 使用 HMMER 3.4 并给 Jackhmmer 打 `seq_limit` 补丁。按同一源码和校验和安装，避免 `PATH` 意外选到另一套工具。补丁影响冗余命中输出与峰值内存：v3.0.4 会检测这个能力，未打补丁时可退回不使用此参数；探针把这种情况报告为警告，而非错误声称完全不能执行。[官方构建文件](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/docker/Dockerfile)、[Jackhmmer 能力检测](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/src/alphafold3/data/tools/jackhmmer.py)

```bash
af3_hmmer_build="$(mktemp -d)"
curl -fL http://eddylab.org/software/hmmer/hmmer-3.4.tar.gz -o "$af3_hmmer_build/hmmer-3.4.tar.gz"
(
  cd "$af3_hmmer_build"
  echo 'ca70d94fd0cf271bd7063423aabb116d42de533117343a9b27a65c17ff06fbf3  hmmer-3.4.tar.gz' | sha256sum -c -
  tar -xzf hmmer-3.4.tar.gz
  patch -p0 < /opt/af3/docker/jackhmmer_seq_limit.patch
  cd hmmer-3.4
  ./configure --prefix=/hmmer
  make -j8
  make install
  make -C easel install
)
/hmmer/bin/jackhmmer -h
/hmmer/bin/nhmmer -h
```

下载地址使用官方构建配方中的 HTTP 上游，因此上面的已知 SHA-256 校验不能省略。安装 AF3 的 Python 依赖和随软件分发的化学组分资源：

```bash
python3.12 -m venv /opt/af3-bootstrap
/opt/af3-bootstrap/bin/pip install uv==0.9.24
cd /opt/af3
UV_PROJECT_ENVIRONMENT=/opt/af3/venv UV_LINK_MODE=copy /opt/af3-bootstrap/bin/uv sync --python python3.12 --frozen --all-groups --no-editable
PATH="/hmmer/bin:$PATH" /opt/af3/venv/bin/build_data
```

这里使用官方锁文件和相同版本的 uv；`build_data` 生成运行时需要的 CCD 资源，不能只安装五个 HMMER 程序就认为 AF3 环境已经完整。实际依赖安装成功与否由后面的 CLI 导入和 CPU 搜索验证。[官方安装配方](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/docker/Dockerfile)

## 3. 下载官方快照，保持原始目录布局

官方安装说明列出的完整数据库网络下载约 **252 GB**，解压后约 **630 GB**；这些是该说明的参考值，不是本机实测，也不是实际文件的完整性校验值。磁盘还要留出工作目录、准备缓存和并发临时文件空间；数据库应放在 AF3 源码目录之外。[官方存储说明与目录清单](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/docs/installation.md#obtaining-genetic-databases)

```bash
df -h /srv/protein-databases
bash /opt/af3/fetch_databases.sh /srv/protein-databases/af3-v3.0-snapshot > /srv/af3-evidence/fetch-v3.0.4.log 2>&1
```

这个命令是真实大体量下载，应在选定服务器和持久化磁盘上人工运行。程序默认不会替用户执行。官方脚本会并行下载和解压多个文件；**看到末尾 `Complete` 不能代替资产检查**。该版本使用后台任务及无参数 `wait`，因此也不能仅凭脚本最后的退出状态证明每个后台下载都成功。中断后应先查日志和资产，不要让零字节或半解压文件进入 ready。[固定版本下载脚本](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/fetch_databases.sh)

设数据库根目录为 `/srv/protein-databases/af3-v3.0-snapshot`，生成器中的资产相对路径应为：

| Profile 资产名 / AF3 flag | 根目录下的实际资产 |
|---|---|
| `uniref90_database_path` | `uniref90_2022_05.fa` |
| `mgnify_database_path` | `mgy_clusters_2022_05.fa` |
| `small_bfd_database_path` | `bfd-first_non_consensus_sequences.fasta` |
| `uniprot_cluster_annot_database_path` | `uniprot_all_2021_04.fa` |
| `seqres_database_path` | `pdb_seqres_2022_09_28.fasta` |
| `pdb_database_path` | `mmcif_files/` |
| `ntrna_database_path` | `nt_rna_2023_02_23_clust_seq_id_90_cov_80_rep_seq.fasta` |
| `rfam_database_path` | `rfam_14_9_clust_seq_id_90_cov_80_rep_seq.fasta` |
| `rna_central_database_path` | `rnacentral_active_seq_id_90_cov_80_linclust.fasta` |

八个序列文件必须是解压后的 FASTA；`mmcif_files` 是解压后直接含大量 PDB ID `.cif` 文件的目录，不是 tar 包路径，也不是额外嵌套一级的 AF2 目录布局。`seqres` 与 mmCIF 应来自匹配的快照，不能只更新其中一项并沿用旧 release。以上文件名来自固定版本下载脚本；flag 对应关系来自同版本 CLI。[下载资产](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/fetch_databases.sh)、[路径 flag](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/run_alphafold.py)

## 4. Profile 和准备命令

生成的数据库 profile 必须包含：

- `engine: "alphafold3"`、`validationProfile: "af3-v3.0.4"`。
- `root` 指向真实解压目录；上表九项 `assets` 全部 `required: true`。其中 `pdb_database_path` 为 `type: "directory"`，其他为 `type: "file"`。
- `release` 表示实际数据快照，例如 `official-v3.0-databases-pdb-2022-09-28-installed-YYYYMMDD`；日期由安装者填写，不能假称当前最新数据库。
- `prepareVersion` 记录 AF3 v3.0.4 提交及本项目准备适配器版本；`searchVersion` 记录实际 HMMER 版本和补丁状态。
- `executables` 显式指定 `jackhmmer_binary_path`、`nhmmer_binary_path`、`hmmalign_binary_path`、`hmmsearch_binary_path`、`hmmbuild_binary_path`，分别指向 `/hmmer/bin/` 中对应程序。
- `searchParameters` 如 `{"jackhmmer_n_cpu":8,"nhmmer_n_cpu":8}`；`templateParameters` 如 `{"max_template_date":"2021-09-30"}`。模板日期是实验条件，不能为追求更多命中而在同一 campaign 中悄悄修改。

控制器准备 argv 配置如下，JSON 数组由应用直接传给子进程，不经 shell：

```json
[
  "/opt/af3/venv/bin/python",
  "/srv/foldspace/scripts/af3_staged_runner.py",
  "--stage", "prepare",
  "--input", "{input_json}",
  "--output-dir", "{output_dir}",
  "--profile-json", "{profile_json}",
  "--job-json", "{job_json}",
  "--alphafold-script", "/opt/af3/run_alphafold.py",
  "--python", "/opt/af3/venv/bin/python"
]
```

准备适配器强制 `run_data_pipeline=true`、`run_inference=false`、关闭大输出压缩，并明确设置 CPU JAX 平台。配置内五个 executable 路径现在会传给官方 CLI；其文件元数据与声明版本参与缓存指纹。相同序列但数据库、搜索参数、模板日期或工具版本不同，会重新准备。

## 5. 预检和真正的 CPU 搜索验收

先运行无搜索预检，路径替换为生成器实际产出的配置：

```bash
cd /srv/foldspace
python3 scripts/af3_database_probe.py \
  --config /srv/foldspace/deploy/generated/databases.json \
  --profile af3-production \
  --alphafold-script /opt/af3/run_alphafold.py \
  --python /opt/af3/venv/bin/python \
  --output /srv/af3-evidence/database-preflight.json
```

配置文件和 profile ID 是调用参数；若生成器输出名不同，以生成结果为准。此步骤检查真实文件可读、非空、FASTA 前后缀，平铺 mmCIF 目录的记录数量，五个工具可执行及其 HMMER 3.4 帮助信息，AF3 固定 Git 提交、受跟踪文件未修改，以及该 Python 环境能导入官方 CLI。stdout 输出 JSON，未就绪退出码为 2。

严格 profile 的 1 MiB 序列文件下限、探针的 100,000 个平铺 mmCIF 下限都是**保守的明显占位/严重不完整检测门槛**，不是官方精确资产大小或数量。短序列假文件、空库、错误 HTML 下载、少量演示模板都不能通过生产探针；门槛通过仍不能证明所有数据库字节完整。报告同时写明内容校验范围，不把快速探针包装为全库校验和认证。

再用一个不包含预计算 MSA/模板的真实蛋白输入执行搜索。例如 Crambin：

```json
{
  "name": "database-smoke-crambin",
  "dialect": "alphafold3",
  "version": 1,
  "modelSeeds": [1],
  "sequences": [{"protein": {
    "id": "A",
    "sequence": "TTCCPSIVARSNFNVCRLPGTPEAICATYTGCIIIPGATCPGDYAN"
  }}]
}
```

将上面内容保存为 `/srv/af3-evidence/smoke-input.json` 后：

```bash
python3 /srv/foldspace/scripts/af3_database_probe.py \
  --config /srv/foldspace/deploy/generated/databases.json \
  --profile af3-production \
  --alphafold-script /opt/af3/run_alphafold.py \
  --python /opt/af3/venv/bin/python \
  --sample-input /srv/af3-evidence/smoke-input.json \
  --sample-output /srv/af3-evidence/smoke-first \
  --sample-timeout 86400 \
  --output /srv/af3-evidence/database-smoke.json
```

`sample-output` 必须是新目录，避免旧结果冒充本次成功。探针拒绝把预填的 MSA、空字符串 MSA 或空模板作为数据库冒烟输入，因为这些字段会令官方流程跳过搜索。成功后，`readinessLevel` 为 `cpu-preparation-smoke-passed`，报告包含运行时间、MSA 行数、模板数和 prepared JSON 的 SHA-256；原始日志留在输出目录。没有模板命中不必然失败，需要结合序列和模板截止日期解释。这个验收不运行 GPU，不证明模型预测准确性。

## 6. 准备数据如何离开数据库服务器

官方 `Input.to_json()` 把 MSA 和模板内容内嵌到 JSON，还可能把相同序列的 A、C 链组合成 `id:["A","C"]`。本项目已按唯一 chain ID 和序列核对，不再误把序列分组导致的输出顺序变化判为输入错误。模板必须包含内嵌 mmCIF 文本与匹配的残基索引；`mmcifPath`、`pairedMsaPath` 等依赖控制器路径的准备结果会在发布缓存前被拒绝。[官方序列化实现](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/src/alphafold3/common/folding_input.py)

CPU 输出通过文件摘要清单原子发布；worker 下载 ZIP、检查 SHA、解包并核对清单。真正供推理消费的是自包含数据 JSON。包里附带的数据库 profile 只是出处元数据，不能让 GPU worker反向访问原 CPU 路径。当前回归测试把带 MSA 和内嵌模板的缓存打包，移到独立目录，再删除原缓存后验证可读；这是运输与校验测试，不是对真实 AF3 特征化的替代。上线仍需把上一节真实准备产物交给已安装权重的 worker，完成一次实际推理验收。

## 7. CPU、内存与版本记录

一项蛋白准备会并行搜索四个蛋白数据库。若每个 Jackhmmer 配置 8 线程，两项准备并发，对应最高约 `2 × 4 × 8 = 64` 个搜索线程的配置量，还未计 Python、模板搜索及调度开销。这是并发配置推算，不是实测 CPU 利用率或吞吐。CPU 不足、内存不足或存储带宽有限时先减少准备 worker 数或单工具线程；增加 GPU 数量不能消除这个瓶颈。[官方并发实现](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/src/alphafold3/data/pipeline.py)

每个正式快照保存：下载日志、源提交、安装依赖、工具版本和 patch 状态、九资产路径、模板日期、两份探针报告，以及正式投产前的 GPU 验收报告。举例：

```bash
git -C /opt/af3 rev-parse HEAD > /srv/af3-evidence/af3-commit.txt
/opt/af3/venv/bin/python -m pip freeze > /srv/af3-evidence/python-packages.txt
sha256sum /opt/af3/uv.lock /opt/af3/fetch_databases.sh /opt/af3/docker/jackhmmer_seq_limit.patch > /srv/af3-evidence/installation-files.sha256
```

若 uv 创建的环境没有 pip，可改用 `/opt/af3-bootstrap/bin/uv pip freeze --python /opt/af3/venv/bin/python`。探针的文件头尾指纹和元数据适合识别常见换库错误；要求完整校验时，应另行对实际大文件流式计算 SHA-256 并保存 manifest，这会完整读取几百 GB 数据，应安排专门时间和 I/O 预算。更新库时使用新快照目录和新 profile，不在运行 campaign 底下原地覆盖。准备缓存包含整目标与 AF3 独立链两层；共享链在相同搜索身份下跨候选组合复用，机制与计数见[链级复用专题](chain-reuse-design.md)。
