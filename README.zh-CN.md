# IFWI Stitching MCP

[English](README.md) | 简体中文

一个 [MCP](https://modelcontextprotocol.io/) 服务器（Python + [FastMCP](https://github.com/jlowin/fastmcp)），
让 MCP 宿主（Claude Code、VS Code Copilot 等）能够驱动完整的 IFWI stitching 流程：查询 **FIV Portal** 的项目与
release，从 Artifactory 下载 IFWI / ingredient / stitch 工具，解析本地 IFWI 镜像的 OEM 区域，并在独立的
virtualenv 中运行 stitch 工具。

所有函数都返回**统一的结果结构** —— `{"ok": True, "data": {...}}` 或
`{"ok": False, "error_code": <枚举>, "message": <str>, "detail": {...}}`。任何裸异常都不会跨越工具边界。

## 能用它做什么

用自然语言跟你的 MCP 宿主说需求，它会自己挑选合适的工具。典型场景：

- **"DMR B0 PowerOn 分支最新的 Orange release，MMC 是什么版本？"** —— 宿主查到 release，列出它的 binary
  （如果不止一个会先问你选哪个），再读出这个 binary 里烤进去的 ingredient 版本。
- **"把 MMC1 和 MMC2 都换成 0.967.0，帮我 stitch 一个新镜像。"** —— 宿主基于基础 IFWI 和两个新版本
  ingredient 生成一份 plan，把最终要跑的命令给你确认，然后下载所需的一切、跑 stitch 工具，最后把 stitch
  好的 `.bin` 和产物清单交给你。
- **"这是一个本地 `.bin` 文件，我不知道它来自哪个 release。"** —— 宿主解析它的 OEM 区域，反查出对应的
  FIV build。
- **"MMC1 这个 ingredient 历史上一共发布过哪些版本？"** —— 直接查 FIV Portal。

如果你只是想在 MCP 宿主里使用这个服务器，看下面的[普通用户](#普通用户)部分就够了。每个场景背后具体调用
了哪些工具，详见[开发者](#开发者)部分。

## 普通用户

### 环境要求

- **Python 3.9+**

### 安装

```bash
# 在项目根目录
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

# 可编辑安装（改代码即生效，无需重装）
pip install -e .

# 带开发/测试依赖
pip install -e ".[dev]"
```

### 配置

服务器需要三个值：`FIV_BASE_URL`（启动时必需）、`FIV_TOKEN` 和 `ARTIFACTORY_TOKEN`（懒校验，只有调用到
需要它们的工具时才检查）。

把 [`.env.example`](.env.example) 复制成项目根目录下的 `.env` 并填入自己的值：

```bash
cp .env.example .env
```

服务器启动时会自动（通过 `python-dotenv`）从当前工作目录或其上级目录加载 `.env`，不需要任何宿主专属的
`envFile` 设置。进程环境里已经存在的值（例如宿主 `env` 块里内联设置的）始终优先于 `.env`。

| 变量 | 是否必需 | 用途 |
|------|----------|------|
| `FIV_BASE_URL` | **是** | FIV Portal 的基础 URL，例如 `https://fiv.example.com`。 |
| `FIV_TOKEN` | 调用 FIV 时 | FIV 访问 token，以 `Authorization: Bearer <token>` 发送。 |
| `ARTIFACTORY_TOKEN` | 下载时 | Artifactory 访问 token，下载产物时以同样方式发送。 |
| `IFWI_MCP_CACHE_DIR` | 否 | 缓存根目录，默认 `~/.ifwi-stitching-mcp/cache`。 |

其余配置（stitch 任务在哪跑、产物怎么打包、老版本工具的依赖兜底）都有合理默认值，只有你确实需要改动时才
用得上，见[高级配置](#高级配置)。

### 注册到 MCP 宿主

**Claude Code / Claude Desktop**（`.mcp.json`，顶层键 `mcpServers`）：

```json
{
  "mcpServers": {
    "ifwi-stitching": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "ifwi_mcp.server"],
      "cwd": "/path/to/ifwi-stitching-mcp"
    }
  }
}
```

设置 `cwd` 能保证不管 Claude Code 自身进程的工作目录在哪，服务器都能找到你的 `.env`。如果你不想依赖
`.env`，也可以继续把值内联写进 `env` 块——完整的覆盖项列表见[用 MCP 宿主配置，而不是配置文件](#用-mcp-宿主配置而不是配置文件)。

**VS Code Copilot**（`.vscode/mcp.json`，顶层键是 `servers`，不是 `mcpServers`）：

```json
{
  "servers": {
    "ifwi-stitching": {
      "type": "stdio",
      "command": "${workspaceFolder}/.venv/Scripts/python.exe",
      "args": ["-m", "ifwi_mcp.server"],
      "cwd": "${workspaceFolder}",
      "dev": { "watch": "ifwi_mcp/**/*.py" }
    }
  }
}
```

`dev.watch` 会在源码改动后自动重启服务器（配合可编辑安装，改完保存即生效）。如果你更喜欢 VS Code 自带的
`envFile`，它依然可用——最终设置的是同一批环境变量。

`command` 的路径分平台：Windows 是 `.venv/Scripts/python.exe`，Linux/macOS 是 `.venv/bin/python`。两种宿主配置
都是按机器走的，按服务器实际运行的位置选 —— 注意写在 WSL 里的配置要用 Linux 路径，即使 VS Code 本体跑在
Windows 上。

### 排障

- **改了代码不生效**：MCP server 是常驻进程，需重启。VS Code 里配了 `dev.watch` 会自动重启；否则用
  `MCP: List Servers` → Restart。
- **工具列表/描述是旧的**：运行 `MCP: Reset Cached Tools`。
- **服务器起不开**：看 `MCP: List Servers` → Show Output，`validate_startup()` 失败会打印一条说明清楚的 JSON。

## 开发者

### 架构

一层很薄的 FastMCP 工具层背后是若干职责单一的模块。

| 模块 | 职责 | 约束 |
|------|------|------|
| `result.py` | `ok()` / `err()` 辅助函数 + `ErrorCode` 常量 | — |
| `config.py` | 环境变量、`.env` 加载、配置文件、缓存布局、启动校验、token 访问 | **唯一**读取 `os.environ` 和配置文件的模块 |
| `fiv_portal.py` | FIV Portal REST 客户端（项目、release、swimlane、各类查找） | 只做 HTTP + JSON —— 不碰文件，不起子进程 |
| `downloader.py` | Artifactory 下载 + 本地拷贝 → 缓存 | 只处理文件 —— 不感知 FIV |
| `oem_parser.py` | 零依赖的 OEM 区域解码器 | 仅标准库 —— 不联网，不起子进程 |
| `archive.py` | `.zip` / `.tar*` / `.7z` 解压 | 纯文件 I/O |
| `stitch_runner.py` | 解压归档 + 建 venv + 渲染并执行 `cli.py` 命令 | **唯一**使用 subprocess / venv 的模块 |
| `prepare.py` | 查找 → 下载 → 解压，覆盖 IFWI / ingredient / stitch 工具 | 编排上面几层 |
| `plan.py` | 已确认的答案 → 校验过的 plan + 渲染出的命令行 | 不联网，不起子进程 |
| `executor.py` | 准备环境，然后在本地或远程 runner 上执行 plan | 读取执行开关 |
| `deliverables.py` | 把结果与产物收集到同一目录并生成清单 | 纯 I/O + 下载 |
| `server.py` | FastMCP 应用：把上述能力注册成薄封装工具 | 只做编排 |

### 高级配置

#### 配置文件 —— 决定 stitch 任务在哪里跑

一个 JSON 配置文件持有**执行开关**。路径取 `IFWI_MCP_CONFIG`，默认位于缓存目录旁边
（`~/.ifwi-stitching-mcp/config.json`）。文件不存在即表示"全部用默认值"，也就是本地执行。

```json
{
  "execution": {
    "mode": "remote",
    "endpoint": "https://stitch-runner.example.com/api/v1",
    "token": "<runner-access-token>",
    "timeout_seconds": 3600,
    "poll_interval_seconds": 5
  },
  "deliverables": {
    "archive": true
  },
  "stitch": {
    "fallback_deps": ["colorama", "crcmod", "cryptography", "lxml", "packaging", "cbor2"]
  }
}
```

| 键 | 默认值 | 用途 |
|----|--------|------|
| `execution.mode` | `local` | `local` 在本机以子进程执行 stitch 命令；`remote` 把 plan 交给远程 runner。 |
| `execution.endpoint` | — | **`mode` 为 `remote` 时必填**，runner 任务 API 的基础 URL。 |
| `execution.token` | — | 访问 runner 的 Bearer token。任何工具都不会回显它。 |
| `execution.timeout_seconds` | `3600` | 本地执行超时即终止；远程轮询超时即放弃。 |
| `execution.poll_interval_seconds` | `5` | 远程任务状态的轮询间隔。 |
| `deliverables.archive` | `true` | 是否额外把收集到的产物打包成 zip。 |
| `stitch.fallback_deps` | `[]` | 当工具目录里既没有 `requirements.txt`，也没有平台相关的 `requirements_windows.txt`/`requirements_linux.txt`（见下文）时，安装到工具 venv 里的 pip 包列表。默认为空——不配置就不会安装任何东西。 |

`IFWI_MCP_EXEC_MODE`、`IFWI_MCP_EXEC_ENDPOINT`、`IFWI_MCP_EXEC_TOKEN` 和 `IFWI_MCP_STITCH_FALLBACK_DEPS`
（逗号分隔）会覆盖文件里的值。无效的 execution 段会导致**启动失败**，因此配错的远程端点会在任何实际工作
开始前就被拦下。

#### 工具没有 `requirements.txt` 时的依赖安装

`extract_stitch_tool` 会为每个工具建一个 venv 并安装依赖，顺序如下：

1. `cli.py`/`stitch2.py` 同级目录，或工具根目录下的 `requirements.txt`。
2. 工具根目录下任意位置（按当前系统选择）嵌套的 `requirements_windows.txt` / `requirements_linux.txt`——
   老版本的工具包会以这种方式携带 FIT 工具自身的依赖，通常在离入口脚本好几层的地方（例如
   `FITm_Py/<version>/` 下）。这是真实存在的文件，不是兜底：FIT 工具正是用同一个 venv 里的
   `sys.executable` 调起的，所以它的依赖也必须装进这个 venv。
3. 只有当以上两者都找不到时，才会用 `stitch.fallback_deps` / `IFWI_MCP_STITCH_FALLBACK_DEPS`（如果配置了的话）。
   默认不装任何东西；只在某个工具包彻底没有 requirements 文件、且其 owner 还没补上之前，把这个当作临时手段来配置。

#### 用 MCP 宿主配置，而不是配置文件

上面每个键都有对应的环境变量，所以整套配置都可以放在宿主的 `env` 块里（例如 `.mcp.json`、
`.vscode/mcp.json`），完全不需要配置文件：

| 环境变量 | 对应配置项 |
|----------|------------|
| `IFWI_MCP_EXEC_MODE` | `execution.mode` |
| `IFWI_MCP_EXEC_ENDPOINT` | `execution.endpoint` |
| `IFWI_MCP_EXEC_TOKEN` | `execution.token` |
| `IFWI_MCP_EXEC_TIMEOUT` | `execution.timeout_seconds` |
| `IFWI_MCP_EXEC_POLL_INTERVAL` | `execution.poll_interval_seconds` |
| `IFWI_MCP_DELIVERABLES_ARCHIVE` | `deliverables.archive`（`0`/`false`/`no`/`off` 表示关闭） |
| `IFWI_MCP_STITCH_FALLBACK_DEPS` | `stitch.fallback_deps`（逗号分隔） |
| `IFWI_MCP_CONFIG` | 配置文件本身的路径 |

环境变量优先级始终高于文件，因此可以用一份共享配置文件放默认值，再由某个宿主单独覆盖 mode。

#### 远程 runner 协议

当 `mode` 为 `remote` 时，服务器会：

1. `POST <endpoint>/jobs`，请求体 `{"plan": {...}}` → 期望返回 `{"job_id": "..."}`
2. 轮询 `GET <endpoint>/jobs/<job_id>` → 期望返回 `{"status": "running|succeeded|failed", "exit_code": 0,
   "command_line": "...", "log_tail": "...", "deliverables": [{"name", "url", "kind"}]}`
3. 把每个产物 URL 下载到本地 deliverables 目录

runner 需要自行完成与本地模式相同的环境准备 —— plan 里已经携带了所需的全部信息。以上三步都会带
`Authorization: Bearer <execution.token>`。

### 工具

所有工具都返回上文描述的统一结果结构。

| 工具 | 签名 | 用途 |
|------|------|------|
| `fiv_list_projects` | `()` | 列出 FIV 项目（`id`、`name`、`project_name`）。 |
| `fiv_get_project` | `(project)` | 项目详情 + 由 `ifwi_type`/`ifwi_sub_type` 推出的粗分类（IFWI server/client/graphic，或 UP/BIOS）。 |
| `fiv_list_swimlanes` | `(project, phase=None)` | 列出配置的 build type / swimlane 分支（取自 build 元数据，仅可见项），可按 phase 过滤。 |
| `fiv_list_releases` | `(project, phase, swimlane=None)` | 某 phase 下的 release，按时间从新到旧（无需提供 version）。指定 `swimlane` 可查非默认泳道。 |
| `fiv_find_ifwi` | `(project, phase, version, swimlane=None)` | 解析 IFWI 包（`ifwi_url` 为可下载的 `.7z`）及其 `.bin` 列表。省略 `swimlane` 且存在多个时返回 `MULTIPLE_SWIMLANES`。 |
| `fiv_list_release_binaries` | `(project, phase, version, swimlane=None)` | **选 IFWI 的首选方式。** release report 里经过验证的 IFWI binary，每条直接指向一个 `.bin`。 |
| `fiv_list_ifwi_binaries` | `(project, phase, version, swimlane=None)` | *回落*方案：列出该 release 的所有 build target，含各自的 `.7z` `package_url` 与内含 `.bin`。仅在用户否决了 report 里全部 binary，或没有 report 时使用。 |
| `fiv_get_binary_ingredients` | `(project, phase, version, binary_name=None, swimlane=None, package_name=None)` | 列出某个 release binary 里烤进去的 ingredient（名称、版本等）—— 例如读出某个 `.bin` 用了哪个 MMC 版本。`binary_name` 必须精确匹配 `fiv_list_release_binaries`/`fiv_list_ifwi_binaries` 返回的条目；若省略且该 release 有不止一个 binary，会返回 `IFWI_BINARY_AMBIGUOUS` 并列出全部候选，而不是自己猜一个。 |
| `fiv_find_ingredient` | `(project, name, version)` | 按精确名称和版本解析 ingredient 的下载 URL。 |
| `fiv_list_ingredient_versions` | `(project, name, status="ALL", include_stitch_ingredient=0)` | 列出 FIV 记录过的某个 ingredient 的全部历史版本 —— 权威来源，走 FIV 专用接口，而不是扫描历史 release binary 推断。 |
| `fiv_find_stitch_tool` | `(project, phase, version, swimlane=None)` | 解析 stitch 工具包（`stitch_url` 为可下载的 `.7z`）及其文件列表。若 `build_target` 中没有任何包上报 stitch 工具，会回退去 Artifactory 上直接浏览 `release_root`（`<release_root>/<target 名称>/<package>`）再判定找不到。 |
| `fiv_match_build_by_oem` | `(project, product, ifwi_version, flavor, phase, swimlane=None)` | **路径 B** —— 用解析出的 OEM 信息反查 build。 |
| `parse_ifwi_oem` | `(local_ifwi_path)` | 解码本地 IFWI 的 OEM 区域（偏移 `0xF00`，256 字节），返回 `product`、`ifwi_version`、`flavor_value`、`flavor_type`、`hash`。 |
| `download` | `(url_or_path, category="ifwi", dest_name=None)` | 下载 Artifactory URL，或把本地文件拷入缓存。`category` ∈ `ifwi` / `ingredients` / `stitch`。 |
| `list_local_files` | `()` | 列出各分类下已缓存的文件。 |
| `extract_archive` | `(archive_path, dest_name=None)` | 解压任意 `.zip`/`.tar*`/`.7z` 到缓存，返回 `extract_dir`、`file_count` 及内含 `.bin`。用于 IFWI `.7z` 包。 |
| `extract_stitch_tool` | `(archive_path)` | 解压 stitch 工具、建 venv、列出 `Config_Stitch_*.ini` 目标。 |
| `run_stitch` | `(stitch_dir, binary_file, ingredients, config_ini, soft_strap=None)` | 在 venv 中运行 stitch 工具的 `cli.py`，返回最新产出的 `.bin`。`ingredients` 是 JSON 数组字符串，例如 `[{"name": "MMC1", "path": "/abs/dir"}, {"name": "MMC2", "path": "/abs/dir2"}]` —— 所有条目会在一次调用里一起 stitch（按供应商工具自身的语法用竖线拼接）。 |
| `fiv_prepare_ifwi` | `(project, phase, version, swimlane=None, binary_name=None, from_build_targets=False)` | 取回基础 `.bin`。默认读 release report（直接下载）；`from_build_targets=True` 则改为下载并解压 build target 的 `.7z`。 |
| `fiv_prepare_ingredient` | `(project, name, version)` | 下载 + 解压 ingredient（精确版本，不回退）。 |
| `fiv_prepare_stitch` | `(project, phase, version=None, swimlane=None, search_neighbors=False)` | 下载 + 解压 stitch 工具并建 venv。指定版本没有工具时立即失败；带 `search_neighbors=True` 重试才会去查相邻版本并给出候选。 |
| `get_execution_config` | `()` | 报告执行开关：`mode`、`endpoint`、各超时、是否已设置 token。 |
| `stitch_build_plan` | `(project, phase, ingredients, config_ini, version=None, swimlane=None, ifwi_binary=None, ifwi_path=None, ifwi_from_build_targets=False, stitch_version=None, stitch_path=None, soft_strap=None)` | 把与用户确认好的答案变成一份保存下来的 plan + 命令行。不下载任何东西。`ingredients` 是 JSON 数组字符串，每个 ingredient 一条：`{"name": str, "version": str}`（从 FIV 取）或 `{"name": str, "path": str}`（已有的本地目录/文件）。 |
| `stitch_get_plan` / `stitch_list_plans` | `(plan_id)` / `()` | 重新读取某份 plan，或列出全部。 |
| `stitch_prepare_environment` | `(plan_id)` | 只准备环境（下载并解压），不执行。 |
| `stitch_execute_plan` | `(plan_id)` | 准备环境 → 本地或远程执行 → 收集产物。 |
| `stitch_get_deliverables` | `(plan_id)` | 重新读取某份已完成 plan 的产物清单。 |

`phase` ∈ `Blue` / `Orange` / `Purple` / `Daily`。`version` 形如 `YYYY.WW.D.NN`（允许带前导点）。

#### 错误码

每个失败都带有稳定的 `error_code`，取值范围：

`INVALID_ARGUMENT`、`MISSING_TOKEN`、`AUTH_FAILED`、`PROJECT_NOT_FOUND`、`RELEASE_NOT_FOUND`、
`MULTIPLE_SWIMLANES`、`INGREDIENT_NOT_FOUND`、`STITCH_TOOL_NOT_FOUND`、`IFWI_BINARY_NOT_FOUND`、
`IFWI_BINARY_AMBIGUOUS`、`OEM_REGION_NOT_FOUND`、`OEM_PARSE_FAILED`、`OEM_MATCH_AMBIGUOUS`、
`OEM_MATCH_NONE`、`DOWNLOAD_FAILED`、`EXTRACT_FAILED`、`VENV_SETUP_FAILED`、`STITCH_RUN_FAILED`、
`CONFIG_INVALID`、`PLAN_INVALID`、`PLAN_NOT_FOUND`、`ENV_PREPARE_FAILED`、`REMOTE_EXEC_FAILED`、
`REMOTE_TIMEOUT`、`DELIVERABLE_MISSING`、`INTERNAL_ERROR`。

### 工具调用顺序

这些是 MCP 宿主（或者直接手动调工具的人）把用户的一句话需求变成一个 stitch 好的镜像时，实际走的步骤。

#### 选择 IFWI binary

版本定下后，分两步选 binary —— **不要**一上来就枚举 build target：

1. `fiv_list_release_binaries(project, phase, version)` → 拿到 release report 里的 IFWI binary，展示给用户确认。
   每条都是直接的 `.bin` URL，不需要下载 `.7z` 再解压。
2. 只有当用户说“都不是”时，再调 `fiv_list_ifwi_binaries(project, phase, version)` 枚举该 release 的全部
   build target，并用 `ifwi_from_build_targets=True` 把这个选择记录下来。

没有 report 的 release 会返回 `RELEASE_NOT_FOUND`，`fiv_prepare_ifwi` 会自行回落到 build target 流程。
其他错误（`MULTIPLE_SWIMLANES`、`AUTH_FAILED` 等）**不会**被回落吞掉 —— 必须先解决，否则可能拿到错误
泳道的 binary。

#### Plan 驱动流程（推荐）

宿主先向用户提问，再把确认好的答案交过来：

1. 用查询类工具（`fiv_list_projects`、`fiv_list_releases`、`fiv_list_release_binaries` 等）把答案补齐。
   有歧义时会以错误形式返回，并在 `detail.candidates` 里给出候选，宿主据此再问一句即可。
2. `stitch_build_plan(...)` → 保存下来的 plan + `command_line`，例如：
   `{python} cli.py --binary_file {binary_file} --ingredient_name MMC1|MMC2 --ingredient_path {ingredient_path} --config_ini {config_ini}`。
   （多个 ingredient 会拼进同一次调用，`ingredients` 列表里一条对应一个。）花括号是占位符，对应那些要等
   环境准备完才存在的路径 —— 把这行展示给用户做最终确认。
3. `stitch_execute_plan(plan_id)` → 准备环境（下载 IFWI 包、ingredient、stitch 工具，建 venv），在本地或远程
   runner 上执行解析后的命令行，返回退出码、真正执行的命令，以及产物清单。

产物落在 `$CACHE_DIR/deliverables/<plan_id>/`：stitch 后的 `.bin`（`kind: stitched_bin`）、运行日志
（`kind: log`）、工具写到 `output/` 的其它文件（`kind: output`）、逐项带大小与 sha256 的 `manifest.json`，
以及一个 zip（除非关闭）。**执行失败时日志依然会被收集**，并从 `detail.deliverables` 引用。

如果想先把东西都拉下来检查一遍再执行，先调 `stitch_prepare_environment(plan_id)`。

#### 路径 A —— 底层，一步一个调用

1. `fiv_list_projects` → 选一个项目
2. `fiv_list_release_binaries(project, phase, version)` → 选一个 `.bin` 并直接 `download`；若用户全部否决，
   回落到 `fiv_find_ifwi` / `fiv_list_ifwi_binaries`（若返回 `MULTIPLE_SWIMLANES` 先定 swimlane），再
   `download(ifwi_url)` + `extract_archive(local_path)`
3. `fiv_find_ingredient(...)` + `download(...)`
4. `fiv_find_stitch_tool(...)` + `download(..., category="stitch")`
5. `extract_stitch_tool(archive_path)` → 选一个 `config_ini` 目标
6. `run_stitch(stitch_dir, binary_file, ingredients, config_ini)` → stitch 后的 `.bin`

#### 路径 B —— 从一个本地 IFWI 镜像出发

1. `parse_ifwi_oem(local.bin)` → `product`、`ifwi_version`、`flavor_value`
2. `fiv_match_build_by_oem(project, product, ifwi_version, flavor, phase)` → 定位到 swimlane / 包
3. 接着走 `stitch_build_plan` / `stitch_execute_plan`（把本地镜像作为 `ifwi_path` 传入）

### 开发

```bash
# 跑完整测试套件（HTTP 用 responses 打桩；stitch_runner 的测试会真的建 venv）
.venv/bin/python -m pytest -v
```

测试覆盖全部模块与 server 层。HTTP 由 `responses` 打桩；`stitch_runner` 的测试会真实创建 virtualenv 并起子进程，
因此耗时略长。
