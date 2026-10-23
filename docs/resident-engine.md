# 真正跨目标、跨批次复用的 AF3 模型进程

当前常驻后端支持 **AlphaFold 3 v3.0.4 原生 Python API** 和明确标示的公开结果 reference 回放。AF3 每个进程只构造一次 `ModelRunner`，加载一次权重，创建一次持久 JIT callable；后续目标和后续批次直接调用这个对象。执行目标时不再启动 `run_alphafold.py` 子进程。ColabFold 继续使用现有 staged adapter，本轮不宣称已实现它的常驻模型。

代码入口：`backend/resident.py` 是持久进程客户端；`scripts/resident_runner.py` 是 JSONL server；`backend/tests/test_resident.py` 验证生命周期和实际原生适配调用路径。真正模型运行需要管理员已安装且有权使用的 AF3 环境与权重。这里没有下载模型，也没有完成真实 GPU 速度或科学质量验收。

## 1. 常驻的具体对象

AF3 原实现将 `model_params` 和 `_model` 定义为 `cached_property`；`process_fold_input` 接收已有 `model_runner`。本适配器保留同一个实例，并在启动阶段显式取出权重，使用 `jax.device_put` 放到选定 GPU，等待拷贝完成，然后保留 `_model` callable。小型 `__meta__` 元数据留在主机上，兼容官方调用 identifier 的 `.tobytes()`。后续通过 `data_pipeline_config=None` 禁用数据库搜索。[AF3 v3.0.4 原生代码](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/run_alphafold.py)

“已创建 JIT callable”不等于所有长度都完成编译。首次遇到新的兼容 shape/bucket 仍可能编译，启动事件因此明确返回 `compiledShapesWarmed=false`。可配置磁盘 compilation cache，但磁盘编译缓存和内存模型常驻是两个不同机制。

所有目标都使用相同模型签名：引擎、管理员声明的模型版本、numSamples、numRecycles、attention、bucket 列表、模型目录、可见设备索引、准备模板日期及安装脚本 SHA256。不兼容的请求会产生该目标的失败事件，不会悄悄加载第二套模型。种子与目标序列可以改变；一次请求内多个目标顺序执行，**不是张量维度上的多目标 minibatch**。

prepared AF3 输入必须自包含并与提交蛋白链一致；本地副本会改为该目标的 seeds，随后调用官方 `folding_input.Input.from_json`。传入 descriptor 时先验证本地缓存 manifest 与文件 SHA256；传入直接主文件时只校验其格式/序列，制品完整性应已由 worker 解包层验证。[AF3 输入解析](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/src/alphafold3/common/folding_input.py)

## 2. 实际启动命令

公开 reference（无 GPU、无新预测、无模型加载）：

```bash
python3 /Users/luopeng/Documents/GitHub/protein-studio/scripts/resident_runner.py \
  --engine reference
```

AF3 worker（替换为该 GPU 节点上的真实路径）：

```bash
CUDA_VISIBLE_DEVICES=0 \
/opt/alphafold3/venv/bin/python /opt/protein-studio/scripts/resident_runner.py \
  --engine alphafold3 \
  --af3-script /opt/alphafold3/run_alphafold.py \
  --model-dir /srv/protein-models/alphafold3 \
  --model-version '实际安装提交与权重版本' \
  --num-samples 5 --num-recycles 10 \
  --attention triton --device 0 \
  --buckets 256,512,768,1024,1280,1536,2048,2560,3072,3584,4096,4608,5120 \
  --max-template-date 2021-09-30 \
  --compilation-cache /srv/protein-cache/jax-af3 \
  --allowed-root /srv/protein-worker
```

GPU 节点不需要连接主控上的 CPU 序列数据库；它只需要已打包的 prepared 输入、AF3 安装和模型权重。它不会调用主控 `get_profile()`，也不要求配置 `PREPARE_COMMAND`。

`CUDA_VISIBLE_DEVICES` 必须在进程创建前固定。一个常驻进程在其整个生命周期占用对应模型资源；不能在每个目标结束后把同一张卡分配给另一个不受控模型进程。十六张独立工作卡通常配置十六个独立 resident slot，各自持有一张物理卡。此原生实现只选一个可见 JAX device；把 `CUDA_VISIBLE_DEVICES=0,1,2,3` 交给它不会自动启用模型并行。

`--allowed-root` 是可选的本机输入/输出目录约束，应覆盖 worker 解包目录与输出目录；不覆盖它们时会拒绝目标。`--model-version` 是管理员声明，实际安装脚本 SHA256 也写入签名；这没有代替对整个权重包的外部完整性校验。

## 3. Python 客户端与文件结果

```python
from backend.resident import ResidentProcess, ResidentProcessError

command = [
    '/opt/alphafold3/venv/bin/python',
    '/opt/protein-studio/scripts/resident_runner.py',
    '--engine', 'alphafold3',
    '--af3-script', '/opt/alphafold3/run_alphafold.py',
    '--model-dir', '/srv/protein-models/alphafold3',
    '--model-version', '实际安装提交与权重版本',
    '--num-samples', '5', '--num-recycles', '10',
]
client = ResidentProcess(
    command, '/srv/protein-worker/control',
    env={'CUDA_VISIBLE_DEVICES': '0'},
    startup_timeout=600, request_timeout=7200,
)
ready = client.start()

# target 必须提供 targetId、job、preparedPath、outputDir。
# preparedPath 可以是主输入文件，或已重写为本地路径的 prepared.json descriptor。
# outputDir 每次尝试使用新目录；job 包含 engine/chains/seeds/numSamples/numRecycles。
try:
    files = client.predict_batch(targets, emit=on_target_event, cancelled=should_cancel)
finally:
    client.close()
```

`predict_batch()` 默认 `load_results=False`，返回每目标的小型元数据：`targetId`、`status`、`resultJson`、`resultBytes`、`pid`、`generation`；失败项返回 `error` 和 `retryable`。**不把多个目标的 PAE 全部读入并保留在 results 列表中。** `load_results=True` 仅用于小型交互与测试，会额外包含完整 `result`。

每个完成目标立即同步调用：

```python
{
    'event': 'target_complete',
    'type': 'target_result',
    'targetId': 'candidate-001',
    'status': 'completed',
    'resultJson': '/srv/protein-worker/attempt-001/result.json',
    'resultBytes': 12345,
    'pid': 1234,
    'generation': 1,
}
```

worker 可以在回调中逐项打包/上传，避免等待整批结束。回调返回后客户端继续读取事件；模型进程可能已开始下个目标。回调异常会终止本次 resident，以避免继续执行而没有可靠结果接收方。网络上传时间计入该次 request 的总超时，应据批大小和上传策略配置。

## 4. JSONL 协议

stdout 只允许单行 JSON 事件。runner 保留一个独立协议 fd，将模型 Python 与 C 层 stdout 都转到 stderr，日志写入客户端 `resident-<generation>.stderr.log`。完整结果写磁盘，不通过 stdout 输出 PAE 或原子坐标数组。

启动 `ready` 包含：

- `protocolVersion=1`、pid、sessionId、固定 signature。
- `initializationCount=1`：后端对象初始化一次。
- AF3 的 `modelLoads=1`、`weightsLoaded=true`；已实际请求原生权重并完成 device_put。
- reference 的 `modelLoads=0`、`weightsLoaded=false`、`isDemo=true`；不是“已加载一个模型”。
- `compiledShapesWarmed=false`；没有虚报目标 shape 已预热。

请求：

```json
{
  "id": "batch-request-001",
  "operation": "predict",
  "targets": [
    {
      "targetId": "candidate-001",
      "job": {
        "engine": "alphafold3",
        "chains": [{"id": "A", "sequence": "AG"}],
        "seeds": [1, 2],
        "numSamples": 5,
        "numRecycles": 10
      },
      "preparedPath": "/srv/protein-worker/prepared/protein_data.json",
      "outputDir": "/srv/protein-worker/results/attempt-001"
    }
  ]
}
```

这里 `AG` 只是协议形状示例，不能代替真实完整蛋白输入。响应依次有 `target_started`、`target_complete{resultJson}` 或 `target_failed{error,retryable}`，最后 `batch_complete`。目标出错通常不阻止后续目标；设备/资源错误触发 `worker_retiring`，尚未开始的剩余目标标为可重试，然后退出进程。实际执行发生OOM/MemoryError/RESOURCE_EXHAUSTED的目标标为不可自动重试；需调整输入、设置或硬件后手动重试，避免反复加载同一模型并确定性OOM。临时device lost等设备故障仍允许恢复。

## 5. 失败、取消与恢复

客户端不会自动重试计算。超时、取消、非法协议或 EOF 会终止对应进程组并标记 `broken`。再次使用时必须显式 `client.restart()`；重启会改变 PID/sessionId 并重新加载权重，这次加载不能被称为前一个常驻进程的复用。

`ResidentProcessError` 和 `ResidentCancelled` 附带 `completed`（已经收到的逐目标终态元信息）和 `pending_target_ids`。调用方应保留并上传已经完成的目标，仅重新排队未完成目标。`completed` 中也可能含已明确失败的目标，需按各项 status/retryable 判断，而不是把整个列表视为成功。

对于已完成写盘、但进程在发出成功事件前死亡的目标，客户端不能证明发布完成，应按未知尝试处理；新的尝试使用新 outputDir。controller 的 lease/fencing 决定哪个尝试有权提交结果，resident 不自行作跨节点幂等判断。

OOM/CUDA 错误的退休分类由明确异常类别和常见运行时错误文本触发；不是对所有驱动故障的完备诊断。节点 supervisor 仍需检查进程和设备健康，必要时重启整个 slot。部分请求的科学输入错误只是 target_failed，不会导致隐式模型重载。

## 6. 为什么本轮不把 ColabFold 包装成“常驻成功”

当前 1.6.3 `batch.run` 会在每次调用内部取得 `load_models_and_params`，并对单查询根据 MSA 深度调整 `max_seq/max_extra_seq`。简单反复调用 CLI 或对外层函数加缓存，不能保证同一模型配置、权重与 JIT runner 跨批次保留。[ColabFold batch 原生实现](https://github.com/sokrypton/ColabFold/blob/main/colabfold/batch.py)

真正接入需固定模型种类、MSA 深度配置与其他计算参数，持有原生加载结果，并接通 `generate_input_feature/predict_structure` 的相容调用路径、模板处理和输出校验。原加载接口确实返回 runner 与参数组合，但本轮没有将这条路线生产化，因此不在 resident CLI 宣布支持。[ColabFold 模型加载接口](https://github.com/sokrypton/ColabFold/blob/main/colabfold/alphafold/models.py)

## 7. 已执行的验证

```bash
PYTHONPYCACHEPREFIX=/tmp/protein-studio-pycache \
python3 -m unittest backend.tests.test_resident -v
```

测试以真实 subprocess 启动本 resident runner，在 **实际 AF3 adapter 代码下** 注入受控安装包替身：三目标跨两个 batch 的 PID、ModelRunner 对象 ID 均相同，权重加载计数为 1，JIT callable 创建计数为 1；同时验证普通目标失败后继续、签名不兼容拒绝、外部 kill、批中死亡保留前项、超时/取消、OOM 退休与显式 restart。reference 使用真实公开 AFDB 结构及 PAE，并验证 `newPrediction=false` 与 `modelLoads=0`。

这证明客户端生命周期、协议和原生调用复用机制按设计执行；没有把替身测试当作真实 AF3 GPU 执行、性能提升比例或预测科学质量证明。
