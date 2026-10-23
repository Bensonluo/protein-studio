# AF3 Linux 服务安装模板

状态：用户已于 2026-10-23 搁置部署，本目录保留为待集成验收的模板。当前工作重点为[链级准备复用管线](../../docs/chain-reuse-design.md)，没有执行 Linux 服务安装、GPU 推理或云资源创建。

以下内容描述将已安装的 Foldspace、AF3 v3.0.4、数据库及权重接成服务的方法草稿。模板针对 **Linux native venv**；自定义 inventory 路径还须同步调整 systemd 的代码、解释器与状态目录，不能把配置生成通过视为完整部署已通过。`scripts/verify_af3_campaign.py` 仅完成语法检查，尚未验收。

## 1. 先确认两种运行环境

- `/srv/foldspace`：本项目的完整代码目录。
- `/opt/foldspace/bin/python`：控制器和 worker agent 的 Python 环境。项目标准库后端要求 Python ≥3.10；可统一采用 Python 3.12。
- `/opt/af3/venv/bin/python`：另行安装的 AF3 环境；安装位置可由各机器 inventory 指定。v3.0.4 的 `pyproject.toml` 要求 **Python ≥3.12**，本轮核查的依赖声明含 `jax==0.10.2` 与 Linux `jax[cuda12]`。采用固定 tag 对应的源码与锁文件，记录安装 commit，不用一个任意新 JAX 替换它。[AF3 v3.0.4 依赖声明](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/pyproject.toml)、[锁文件](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/uv.lock)

官方安装说明以 Linux、NVIDIA GPU 和完整搜索资产为基础，提示本地安装需正确配置 CUDA、cuDNN、JAX；官方验证过 A100/H100 80 GB。显存能否容纳本批目标，仍由真实输入、样本数、bucket 与运行参数验收。[官方安装说明](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/docs/installation.md)

不要因为安装了 venv 就认为驱动可用。每台 GPU 主机先运行 `uname -r`、`nvidia-smi`；驱动模块的 kernel headers/development packages 必须对应**正在运行的 kernel**。更新 kernel 后须确认驱动模块仍能加载，并用实际 AF3 Python 检查 JAX GPU。NVIDIA 对操作系统和 kernel 的支持按驱动发行版变化，不能给所有 ECS 镜像写一个通用 kernel 版本。[NVIDIA 驱动安装前检查](https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/pre-installation-actions.html)

如果沿官方容器配方，v3.0.4 Dockerfile 声明 CUDA 12.6.3 / Ubuntu 24.04、Python 3.12、HMMER 3.4 补丁、锁定依赖安装和 `build_data`。这是一份可构建的源码配方；本项目没有发布或拉取过一个可保证存在的预构建 GPU 镜像。此处 systemd 模板直接启动 native venv，不把它混成容器入口。[官方 Dockerfile](https://github.com/google-deepmind/alphafold3/blob/v3.0.4/docker/Dockerfile)

## 2. 文件放到对应机器

| 文件 | 部署位置 | 所在机器 / 用途 |
|---|---|---|
| `controller.json` | `/etc/foldspace/controller.json` | 控制节点；本机监听、账本、序列库配置、worker registry、CPU 准备 argv |
| `controller.env` | `/etc/foldspace/controller.env` | 控制节点；含 registry 使用的各个独立 token 环境变量 |
| 一个 slot 的 JSON | `/etc/foldspace/workers/<slot>.json` | 对应 GPU 主机；该 slot 的物理 GPU、Python、权重路径、URL、超时 |
| 一个 slot 的 env | `/etc/foldspace/workers/<slot>.env` | 对应 GPU 主机；只含该 worker 的 token 变量 |
| 数据库 / worker registry JSON | `controller.json` 中声明的绝对路径 | 控制节点；不放到 GPU worker 后猜测本机路径 |
| CPU 搜索库与索引 | 注册 profile 的实际挂载路径 | CPU 准备所在节点，建议只读挂载；GPU agent 不需要整套搜索库 |

配置 JSON 不包含 token 值。生成的 `.env` 按秘密文件管理，建议 `root:root`、`0600`，由 systemd `EnvironmentFile` 读取；JSON 可为 `root:foldspace`、`0640`。无需把令牌写进 shell 命令、命令历史或 `--token-env` 参数值中：该参数只接受**环境变量名称**。

先由管理员创建 `foldspace` 用户和必要目录，并保证它能读取代码、配置、AF3 安装与模型文件、写入自己的数据目录。以下以默认路径为例；代码及真实数据的复制步骤由实际发布方式完成：

```bash
# 若服务用户已存在，跳过 useradd。
sudo useradd --system --home-dir /var/lib/foldspace --create-home --shell /usr/sbin/nologin foldspace
sudo install -d -o root -g foldspace -m 0750 /etc/foldspace /etc/foldspace/workers
sudo install -d -o foldspace -g foldspace -m 0700 /var/lib/foldspace /var/lib/foldspace-workers
sudo python3.12 -m venv /opt/foldspace
```

系统模板的 `StateDirectory` 创建默认可写位置；如果 inventory 使用其他 `dataDir` / `workDir`，管理员应另行创建并给服务用户正确权限。AF3 compilation cache 也应位于该 slot 可写路径，不能写入只读模型目录。

## 3. 检查配置，再安装服务

无需 token、不会访问 GPU 或控制器的预览：

```bash
/opt/foldspace/bin/python /srv/foldspace/scripts/run_controller.py \
  --config /etc/foldspace/controller.json --dry-run
/opt/foldspace/bin/python /srv/foldspace/scripts/run_gpu_worker.py \
  --config /etc/foldspace/workers/host-a-gpu0.json --dry-run
```

`--check` 要在服务相同身份及 token 环境下运行。下面让 systemd 读取环境文件，令牌值不会出现在命令行：

```bash
sudo systemd-run --wait --pipe --collect --uid=foldspace --gid=foldspace \
  --property=EnvironmentFile=/etc/foldspace/controller.env \
  /opt/foldspace/bin/python /srv/foldspace/scripts/run_controller.py \
  --config /etc/foldspace/controller.json --check

sudo systemd-run --wait --pipe --collect --uid=foldspace --gid=foldspace \
  --property=EnvironmentFile=/etc/foldspace/workers/host-a-gpu0.env \
  /opt/foldspace/bin/python /srv/foldspace/scripts/run_gpu_worker.py \
  --config /etc/foldspace/workers/host-a-gpu0.json --check
```

controller 检查 Python、项目入口、准备 argv 的解释器和脚本、数据库资产预检、registry 与所有 token 的存在；worker 检查入口、AF3 Python 版本、脚本、设备映射与 token 的存在。两者都不加载权重、不执行预测；**AF3/JAX/真实 GPU/权重验收继续运行主手册中的独立 probe 与小规模预测**。数据库预检读取声明的文件元数据，不递归哈希整套库。

安装对应角色模板：

```bash
# 控制节点
sudo install -m 0644 /srv/foldspace/deploy/af3/controller.service \
  /etc/systemd/system/foldspace-controller.service
sudo systemctl daemon-reload
sudo systemctl enable --now foldspace-controller.service
sudo systemctl status foldspace-controller.service
sudo journalctl -u foldspace-controller.service -n 100 --no-pager

# 每一台 GPU 主机
sudo install -m 0644 /srv/foldspace/deploy/af3/worker@.service \
  /etc/systemd/system/foldspace-af3-worker@.service
sudo systemctl daemon-reload
sudo systemctl enable --now foldspace-af3-worker@host-a-gpu0.service
sudo journalctl -u foldspace-af3-worker@host-a-gpu0.service -n 100 --no-pager
```

服务默认在失败后重启；每个 GPU slot 是一个独立实例，只重启失败的 slot。模板为 worker 设置 15 秒重启间隔、10 分钟内最多 5 次启动；达到限制后先修复驱动、路径、模型或配置，再 `systemctl reset-failed <实例名>`。不是无限重启所有 16 张卡。[systemd 服务与重启设置](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)

停止 worker 时先发 SIGTERM，agent 停止取新批并尝试把当前批次逐项回传；模板最多等待 1800 秒。到期后 systemd 清理该服务 cgroup，租约恢复只处理未被接受的目标。长目标需要按实际最长批次调整停止宽限期。不能把整机重启称作无损 drain。

## 4. HTTPS 只转发 worker 协议

优先使用生成器输出的 `controller/nginx.conf`：其域名、TLS 端口与 controller 上游端口已经来自同一 inventory，避免手改三处不一致。补入实际证书路径，安装到该 nginx 实际加载的 `http {}` 配置目录，执行 `nginx -t` 通过后再 reload。原始 [controller.nginx.conf.example](controller.nginx.conf.example) 默认在 **4443/TLS** 上只代理 `/worker/v1/`，controller 默认保持 `127.0.0.1:8765`；`/api/`、静态网页与其他路径返回 404。不要添加把 `/` 代理到同一后端的默认规则，否则会公开当前无用户认证的管理界面。

```bash
sudo nginx -t
sudo systemctl reload nginx
# 在部署终端通过 SSH 转发访问私有管理界面：
ssh -L 8765:127.0.0.1:8765 admin@controller-host
```

worker JSON 的 URL 使用 `https://真实域名:4443`，不加 `/worker/v1` 后缀。agent 验证证书、拒绝跨站重定向；私有 CA 可在该 worker 的允许环境项 `SSL_CERT_FILE` 指定证书路径。实际 token 认证和 worker 身份绑定仍由控制器执行，nginx 不做无鉴权放行。

模板关闭请求/响应代理缓冲，上传和下载直接流转；请求体上限 1 GiB，传输空闲读写超时 1800 秒。这个 nginx 上限并不覆盖控制器 registry 的 `maxBundleBytes`（例如 256 / 512 MiB）和 `maxExpandedBytes`；三者要配套。worker `httpTimeout=600` 为单次 HTTP I/O 的设置，`residentStartupTimeout=1800` 为模型启动等待，`residentRequestTimeout=86400` 为**整个 resident 批次**等待，均由配置生成器写入，不当作单目标预测 SLA。[nginx 代理缓冲与超时](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)、[请求体上限](https://nginx.org/en/docs/http/ngx_http_core_module.html#client_max_body_size)

## 5. 本地 16 卡与现有 ECS 的具体连接方式

16 张本地卡可以在一台、两台或更多机器上。inventory 必须逐主机声明 slot，例如主机 A 的 GPU 0–7 与主机 B 的 GPU 0–7 对应不同 worker ID；**“GPU 0”只在它所在机器内有意义**。最好记录 GPU UUID，避免重启后索引重新排列。此 AF3 native runner 使用一个可见 JAX device，因此 launcher 每个 slot 接受一张显式设备；多个 slot 共同消费大量独立目标，不把 16 个设备 ID 塞给一个单设备 runner。

在已有阿里云 ECS GPU 节点部署相同 agent、固定 AF3 安装与权重、该机器自己的 JSON/env，连接 HTTPS 控制器即可。位置 `local` / `aliyun` 来自控制器管理员 registry，不由客户端任意声明。无需为 GPU 节点复制完整序列库，准备包经哈希校验下载，预测结果逐项回传，控制器验证后发布；不做本地与云 GPU 之间的广域网模型同步。

先只启一个真实 GPU slot，完成 probe、几个长度和采样设置匹配的真实目标，核对 `modelLoads=1`、PID 跨目标稳定、结果结构/置信度可读取，再按 inventory 启动其他 slot。`--check` 成功只代表部署前置检查通过；在线心跳、权重真实加载和目标实际交付分别有不同的证据。

## 6. 本目录已验证的范围

```bash
python3.12 -m unittest discover -s tests -p test_deployment_launchers.py -v
```

13 项专项测试通过，其中包含生成器输出与两个 launcher 的跨模块配置校验；还验证真实 subprocess 的 `exec` 行为、固定 GPU 环境、配置转换、错误前置、实际 registry 文件预检、缺 token 拒绝、令牌不出现在输出中，以及 HTTPS 模板只路由 worker 前缀。测试模块是隔离替身，没有运行 Linux systemd/nginx，也没有进行真实 AF3 GPU 推理；这部分在实际 Linux 主机按上面步骤验收。

官方链接核查日期：2026-10-23；AF3 代码目标版本：v3.0.4。部署时保存该 tag 解析出的 commit、安装环境清单和权重来源标识，避免把文档版本当成运行版本。
