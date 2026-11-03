# 远程执行模式：本地验证记录

> 配套文档：`2026-10-28-remote-execution-status-design.md`（下称"设计文档"）。本文保存设计过程中的验证原始记录，设计文档 §12.4 只摘录结论。文中的 P13 等编号指设计文档 §1 的问题清单，§ 编号指设计文档的章节。

## 附录 A：Linux pod 命令本地验证记录（2026-10-28）

> 这是引入 wrapper **之前**的验证记录，当时用的是 `sh -c 'set -e; git clone ...; pip install ...; exec python3 -m ifwi_mcp.worker ...'` 这种命令。现在的 pod 命令已经改为调用 `stitch_wrapper.py`（附录 B），这里保留作为镜像和拼接工具在 Linux 上可用的依据。

**目的**：在真正接入 Job API 之前，用目标镜像在本地起容器，按 server 实际会下发的命令完整跑一遍，验证镜像、网络、引导命令和 worker 在 Linux 上是否可用。

**环境**：

- Docker Desktop（Linux 引擎），镜像 `amr-registry.caas.intel.com/fia-cloud/fiv-ubuntu24.04-python3.13:v1.0.0`。
- plan 使用本地已保存的真实 plan `plan-20261010-103115-16b183`：SiEn-OakStream-DiamondRapids-AP / Orange / 2026.40.5.01，IFWI 为 `OKSDREL1_IPCleanExternal_NoTrace_DebugSigned_2S`，ingredient 为 MMC1 和 MMC2 0.997.0。
- 容器内只注入 `ARTIFACTORY_TOKEN` 和 `FIV_TOKEN`。这个 plan 的来源是 FIV 而不是固定 URL，所以需要 FIV token。

**结果**：

| # | 验证项 | 结果 |
|---|--------|------|
| 1 | 镜像工具链 | ✅ git 2.43、Python 3.13.18、pip 26、curl、unzip、7z 都有；以 root 运行；`WORKDIR=/app`；镜像内置代理 `proxy-prc.intel.com:913`，`no_proxy` 包含 `.intel.com` |
| 2 | 容器网络 | ✅ github.com、raw.githubusercontent.com、pypi.org、fiv-ifwi.intel.com、af01p-sc Artifactory 都可以访问 |
| 3 | 完整命令（直接从 GitHub `main` clone） | ⚠️ clone → pip → worker → 从 FIV 下载 → 准备 venv → **拼接工具执行成功**（日志中有 `Binary created at: .../tool/Output/outimage.bin`），但 runner 只查找 `tool/output/`，因此报 `STITCH_NO_OUTPUT`（P13）。容器退出码 1，约 2 分 40 秒 |
| 4 | 同一命令，临时把 `Output` 加入查找列表 | ✅ 成功，容器退出码 0，约 3 分 06 秒。上传了 stitched bin（64 MB）、log、`.map`、`_fit.log` 和 `result.json`（`ok=true`） |
| 5 | `set -e` 失败即停止 | ✅ 分支不存在时 clone 失败，容器以退出码 128 结束，worker 没有启动 |
| 6 | `exec` 后的 PID | 命令直接作为容器启动命令时为 **1**；外面再套一层 shell 时为 **7**（收不到 SIGTERM） |

这次测试在 Artifactory 上留下了两个目录：`.../FIV_Portal/stitch_task/plan-20261010-103115-16b183-0b5020dd/`（失败的那次）和 `...-c1b5391c/`（成功的那次），可以按需删除。

---

## 附录 B：stitch_wrapper 本地验证记录（2026-10-28）

**环境**：

- 用 cloud-dockerfile 的 `Dockerfile-linux` 在本地构建测试镜像 `fiv-ubuntu24.04-python3.13:stitch-wrapper-dev`。之前的层全部命中缓存，只新增了 wrapper 这一层。
- 在一个 Docker 网络中运行模拟服务，它同时扮演回调接口和 Artifactory（`PUT/HEAD/GET`），所有写操作都不会进入真实的 Artifactory。
- wrapper 作为容器命令运行（`pid=1`）；用 `spec.worker.module` 指定一个假的 worker 来覆盖各种异常场景。

**结果：23/23 通过**

| 场景 | 退出码 | 回调 | 结果 |
|------|--------|------|------|
| worker 写结果文件 / 只打印到 stdout / 用 commit SHA 拉取 | 0 | 200 | `ok`，`result_source` 分别为 `worker_file` / `worker_stdout`；commit 正确 |
| 拼接失败 | 1 | 200 | `STITCH_RUN_FAILED`（透传） |
| worker 被 SIGKILL | 1 | 200 | `REMOTE_WORKER_CRASHED`（`signal=SIGKILL`） |
| worker 以退出码 0 结束但没有结果 | 1 | 200 | `REMOTE_RESULT_MISSING` |
| 分支不存在（clone 重试 3 次，间隔 5s、10s） | 1 | 200 | `REMOTE_BOOTSTRAP_FAILED`（`step=clone`，`exit_code=128`），并补写了 `result.json` |
| 回调先 503 两次 / 返回 409 / 返回 413 / 一直返回 429 / 响应被截断 | 0 | 503,503,200 / 409 / 413 / 429×7 / 截断,200 | 5xx、429 和截断会重试（429 一直重试到时间用完），409、413 立即停止；截断的响应不会把成功误报为失败 |
| 没有 `report_url` | 0 | 无 | 只写 Artifactory |
| 检查传给 worker 的环境变量 | 0 | 200 | 凭据、`IFWI_STITCH_*`、`IFWI_MCP_EXEC_TIMEOUT` 都正确；`IFWI_MCP_EXEC_MODE` 和 `IFWI_MCP_SERVER_MODE` 已被去掉 |
| 凭据来自 `/run/secrets` 文件 | 0 | 200 | 日志显示 `credential ... file /run/secrets/ifwi_artifactory_token` |
| `--spec-url` | 0 | 200 | 正常 |
| 用完时间预算 | 1 | 200 | `REMOTE_TIMEOUT`，并且**先回调、后上传** |
| `docker stop`（SIGTERM），worker 自己处理信号 / 不处理信号 | 1 | 200 | `worker_file` 中的结果 / `REMOTE_TERMINATED`；`docker stop` 在 0.7s 内完成 |
| spec 不合法 / `term_grace_seconds` 超出上限 | 2 | 200 | `WRAPPER_SPEC_INVALID`，回调带 `run_id` |
| worker 留下占着 stdout 的孤儿进程 | 0 | 200 | 不会卡住（约 5s 后清理） |
| 剩余预算不足以启动 worker | 1 | 200 | `REMOTE_TIMEOUT` |
| `/tmp` 只读（无法创建工作目录） | 1 | 200 | `WRAPPER_INTERNAL_ERROR`，仍然完成了回调并写了 `result.json` |

- 在 `pod.log`、`result.json` 和回调内容中**都没有找到任何 token**。

**真实拼接**（真实 plan、真实 worker、从 FIV 和 Artifactory 下载真实输入；结果写到模拟服务）：

| 场景 | 结果 |
|------|------|
| GitHub `main` | bootstrap 69.5s，worker 175s；worker 报 `STITCH_NO_OUTPUT`（P13），wrapper 透传，并保留 worker 写的 `result.json` |
| 修复 `Output` 后的本地仓库，按 commit SHA 拉取 | **成功**：64MB stitched bin、log、map、fit log 全部上传，回调 `ok=true` |

**代码评审**：独立评审发现 5 个问题，都已修复，并补充了对应的测试场景：

- `http.client` 的异常没有被捕获，会把成功误报为失败；
- 时间紧张时上传排在回调前面，可能挤掉回调；
- spec 不合法时，回调的 `run_id` 为空，会被 server 拒绝；
- `term_grace_seconds` 没有上限；
- 创建工作目录失败时没有回调。

复查没有发现新问题。设计文档评审时又发现 wrapper 对 413、405 这类不可能成功的 4xx 也会重试，已改为除 408、429 外的 4xx 一律停止（`41b1fb8`）。

### B.1 发布镜像 v1.1.0 复测（2026-10-28）

镜像：`amr-registry.caas.intel.com/fia-cloud/fiv-ubuntu24.04-python3.13:v1.1.0`（本地，image id `299695e3a963`）。

| 检查项 | 结果 |
|--------|------|
| wrapper 版本 | `/app/stitch_wrapper.py` 统一换行符后，与 `feat/stitch-wrapper` 分支 HEAD（`41b1fb8`）逐字节一致；`--version` 输出 `stitch_wrapper 1.0.0 protocol 1` |
| 环境变量 | `IFWI_ARTIFACTORY_TOKEN`、`IFWI_FIVPORTAL_TOKEN`、`IFWI_CI_MASTER_TOKEN` 都已写入；没有 `IFWI_MCP_*_MODE`；代理设置和 `PIP_BREAK_SYSTEM_PACKAGES` 不变；`/app/main.py` 仍在 |
| 23 个场景 | **23/23 通过**，没有发现 token 泄露，wrapper 为 `pid=1` |
| 真实拼接（**不注入任何 token**，只用镜像里内置的） | GitHub `main`：bootstrap 55s，worker 123s，报 `STITCH_NO_OUTPUT`（P13，符合预期）。修复了 `Output` 的本地仓库（按 commit SHA）：**成功**，64MB stitched bin 的大小和 sha256 与回调中报告的一致 |

---

## 附录 C：真实 Linux Job API 实测记录（2026-10-29）

**环境**：Job API `http://infra-api-fiv-linux.bios-dev-sh-cloud.intel.com/v1/fivportal/job`（不带 token），镜像 `amr-registry.caas.intel.com/fia-cloud/fiv-ubuntu24.04-python3.13:v1.1.1`（wrapper 1.1.1）。server 运行在开发机 `FIVDEVOPS.ccr.corp.intel.com:18780`，代码来自分支 `agents/remote-execution-status-flow-design`，使用真实的 FIV 和 Artifactory（`DEG-IFWI-LOCAL/FIV_Portal/stitch_task/`）。

**探针 pod**

| pod | command | Job API 的状态序列 | 结论 |
|-----|---------|--------------------|------|
| `fivportal-xkg7tr` | `python3 /app/stitch_wrapper.py --version` | 0.3s 创建 → `jobpending`（pod `Pending`）→ 29s 后 `jobsucceeded`（pod `Succeeded`） | 镜像可拉取，wrapper 可运行 |
| `fivportal-dzoxqd` | 只在按参数拆分时才能执行的 Python 代码 | `jobpending` → `jobactive`（pod **`Failed`**）→ 约 70s 后 `jobpending`，`pods: null`，之后不再变化 | 命令**不是**按参数拆分执行的 |
| `fivportal-zt8ztt` | 同上，但只在经过 shell 时才能执行 | `jobsucceeded`，并上传了 `/proc` 信息 | 命令经过 shell：1 号进程为 `sh -c "<command>"`（`SigCgt=0x10002`，即只处理 SIGINT 和 SIGCHLD，不处理 SIGTERM），我们的进程是 `pid=6` |
| `fivportal-xjgx52` | `exit 3` | `jobpending` → `jobactive`（pod `Failed`）→ 约 136s 后 `jobpending`，`pods: null` | 失败的 job 从不报 `failed`；K8s 不重试 |

**真实拼接**（`scripts/example_rest.py` 的 STITCH_PAYLOAD：项目 232，Orange，2026.41.3.01，OKSDCRB1_NonIPClean_Trace_DebugSigned，MMC1 0.967.0）

| 次 | 代码 | pod | 结果 |
|----|------|-----|------|
| 1 | `5486fca`（无 `exec`） | `fivportal-jqp08q` | 117s 成功，由回调结束。wrapper `pid=6`，`pod_deadline_in=3539s`，bootstrap 15.7s，worker 52.7s。64MB bin 的 sha256 `c730e789…27bb5` 与回调中报告的一致 |
| 2 | `5486fca`，运行中取消 | `fivportal-a7oq6p` | 任务为 `cancelled`、`pod_delete=done`；**没有回调，也没有 `pod.log` 和 `result.json`**（wrapper 收不到 SIGTERM） |
| 3 | `49b4a8e`（加 `exec`），运行中取消 | `fivportal-l1v5nw` | wrapper `pid=1`；删除后 1 秒内依次完成：`pod terminated (SIGTERM)`、回调 `REMOTE_TERMINATED`（`accepted:false`）、写出 `result.json` 和 `pod.log` |
| 4 | `49b4a8e`，完整运行 | `fivportal-ifsq8n` | 82s 成功，由回调结束，bootstrap 17.8s，worker 52.2s，64MB bin |

结果都写在 `stitch_task/` 下的各个 `<run_id>/` 目录中；探针写的 `_probe/` 目录已经删除。