# 大规模预测结果：轻量发布、流式 PAE 与按需查看

2026-10-23。百万级筛选不仅需要 GPU 调度，也需要避免结果处理随矩阵体积重复占用内存、磁盘和网络。当前 campaign 列表和 JSONL 导出已经分页读取元数据，本轮保留这条路径，重点改结果解析、发布、下载和分析界面。

## 当前数据流

```text
多个模型采样输出
  → 逐个校验结构、保留排名标量与路径
  → 采用同一排名指标选出最佳样本
  → 流式解析最佳样本的置信度
  → 原始 JSON + float64 PAE 制品 + 小描述信息
  → 控制端验证、登记制品
  → result.json 与结构文件发布索引，同事务提交 completed
  → 详情元数据 / 按需预览 / 局部原始窗口 / 原文件下载
```

多个样本的全部 PAE、坐标和残基数组不再同时留在 `models[]`。排名仍使用原有共同指标：报告的 ranking score，或全部可用时的 ipTM/pTM 组合，否则使用 pLDDT；不会把不同量纲混在一起排序。非最佳样本的原始输出仍可下载。

置信度使用 [ijson 的低层事件流接口](https://pypi.org/project/ijson/)；本地测试为 ijson 3.6.0、`yajl2_c`、Python 3.12.14。它避免构造完整 JSON 树。矩阵按行缓冲写入 little-endian、row-major 的 float64 文件，并记录 shape、来源 SHA256、派生文件 SHA256 和文件状态。数值沿用原来 `float(value)` 的双精度语义；不使用 float32、整数化或颜色量化。原始 JSON 原文继续保留。

## 发布与缓存回收

`campaign_items.result_publication` 是不超过 4 KiB 的发布索引，保存 schema、item/attempt/input 身份及结果 JSON、主结构的相对路径与文件状态。它与 `completed`、`result_path` 在同一 fenced SQLite 更新中提交。过期 token 无权发布，失败重试会清除旧索引。

GC 对新索引只核对受管路径、身份和文件状态，不再为了知道结果是否存在而加载整个 PAE JSON。索引损坏、文件缺失或被替换时继续保留准备缓存；NULL 的旧记录才使用原有有界检查。这个索引证明主结果的发布状态，不是全部科学制品的内容完整性审计。PAE 读取另外检查来源和派生文件状态。

旧结果不自动迁移。超过旧详情读取上限的历史 JSON 返回 `resultAvailability` 说明与原 JSON 下载入口，不在打开抽屉时全量加载。旧小型结果仍兼容内联 PAE；新大矩阵使用 `paeArtifact`。

## PAE 访问契约

| 接口 | 行为 |
|---|---|
| `GET /api/campaigns/{campaign}/items/{item}` | 轻量结果、制品描述与事件，不自动返回大型矩阵 |
| `GET /api/campaigns/{campaign}/items/{item}/pae?maxSize=256` | 最多 256×256 个实际采样值 |
| `GET …/pae?rowStart=0&colStart=0&rows=128&cols=128` | 最多 128×128 的连续原始窗口，参数从 0 开始 |
| `GET /api/jobs/{job}/pae?...` | 单任务工作流的同类接口 |
| 已登记的 artifact 下载 URL | 流式下载来源 JSON 或派生二进制，不先加载矩阵 |

未知参数、重复参数、混用 preview/tile、负数和越界窗口返回错误。预览返回 `sourceShape`、`shape`、`rowIndices`、`colIndices`、`values` 与 `method='nearest-center'`。采样位置为 `floor((i+0.5)×N/H)`，保留该位置的实际数值；它不是整块均值，也不保证展示每一个局部细节。需要局部细节时读取 `method='exact'` 的连续窗口。

PAE 可以不对称，行列顺序按源文件保留。AF3 的轴可能包含模型 token；有合法 token metadata 时记录它，否则显示模型矩阵索引，不强行解释成 Cα 残基。缺失和非法数值不补成 0。超出可视化的大小限制时保留结构、可用排名指标和来源下载，明确显示不可预览原因；不会只因图太大而丢掉已经有效的结构结果。

## 混合云传输

GPU worker 只保留轻量排名与原生输出，不提前建立随后会由控制端重建的 PAE 文件。AF3 第一遍排名优先读取可用的小 summary，缺失或指标不足时才回退读取 full confidence。worker 上传保留原生结构、confidence JSON、summary 和执行记录，跳过内部 `.result-artifacts` 派生目录以及重复的 result.json。控制端根据原始输出独立选样、生成自己的 PAE 制品；不接受节点提供的绝对路径或派生矩阵作为可信科学结果。这样避免把来源矩阵和可重建矩阵同时传一遍。

控制端准备/验证阶段和本地预测解析支持取消。resident 是独立进程，停止仍由既有 resident 生命周期处理；本轮没有改变真实模型 GPU 执行的取消语义。

## 界面

批量列表及目标抽屉不请求 PAE 数据。工作台显式加载预览，根据画布像素设置上限；可输入局部窗口或用键盘/前后窗口移动。悬停显示对应来源索引和数值。缓存仅保留当前结果的有界预览和一个窗口，切换目标会取消旧请求，旧响应不能覆盖新结果。

“矩阵来源 JSON”可能是原生 confidence，也可能是旧内联数据外置时保存的矩阵片段；界面不把后者称作完整原始 confidence。原生完整文件另保留在制品列表。

当前安全上限为来源 JSON 1 GiB、矩阵轴5000、2500万个单元；上限是制品处理配置，不是模型支持能力。超限时保留有效结构、指标和来源下载。

## 可复现验证

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s backend/tests -v
.venv/bin/python scripts/benchmark_pae.py --size 2048 --report artifacts/pae-synthetic-scale.json
```

合成 2048² 实验：来源 JSON 77,134,000 字节，派生 float64 33,554,432 字节，描述信息约 1.5 KiB；索引约 3.392 秒，256² 预览读取约 0.009 秒，进程峰值 RSS 29,982,720 字节（macOS）。预览和精确窗口全部与生成公式逐值比对。来源超过旧 64 MiB 解析上限，但没有以完整矩阵构造 Python 对象。临时源文件和矩阵已删除。

该次测量在开发机并行回归期间进行，软件源码 SHA 记录在 artifacts/pae-synthetic-scale.json。它是本机合成 JSON/文件系统测量，不是蛋白预测、GPU 推理或阿里云性能。真实 AF3 与序列库部署仍按用户要求搁置。HTTP、浏览器、发布/恢复及数值类型回归结果见本轮验收文件。
