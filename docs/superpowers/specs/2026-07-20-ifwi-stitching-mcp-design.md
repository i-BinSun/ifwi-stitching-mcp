# IFWI Stitching MCP — 设计文档

日期：2026-07-20
状态：待审阅

## 1. 目标与定位

一个 **纯 MCP 服务器**（Python + FastMCP），让用户在 MCP host（Claude Code / Desktop）中通过自然语言完成 IFWI stitching 全流程：从 FIV Portal 查询并下载 IFWI、ingredient、stitch tool，然后在本地运行 stitch，产出 stitched 镜像。

**架构选型：MCP，而非 Agent 或 Skill。**
- 本业务的核心是**能力**——带认证的 REST 调用、Artifactory URL 构造、下载、解压、创建 venv、subprocess 运行 stitch 并抓日志。这些需要可靠、可测、可复用的代码，正是 MCP 的定位。
- 不做 Agent：无需脱离 host 自主运行，host 已提供模型与编排循环，再套 agent 是重复造轮子。
- 不用 Skill 替代：Skill 是纯指令（知识/流程），无法固化上述能力。但**后续可选**在 MCP 之上加一层薄 skill，沉淀标准编排流程。

**模型 key：不涉及。** 服务器本身不调用任何大模型；模型由 host 提供并负责自然语言理解与工具编排。

## 2. 第一阶段范围

第一阶段覆盖两条主路径：

**路径 A — FIV Portal 驱动**（对应 expectation.txt 的 2.1 / 3.1 / 4.1 / 5）：
自然语言选项目+版本 → FIV 查询链接 → 下载 IFWI + ingredient → 从 build 扫描并下载 stitch tool → 解压 → 运行 stitch。

**路径 B — 本地文件 + OEMID 反查驱动**（对应 2.2 / 3.2 / 4.2 / 5）：
用户指定本地 IFWI 文件 → 解析 OEMID 得到 Product + IFWI Version + Flavor → 按 product+version 在 FIV Portal 找到 release → 按 flavor 精确定位到一个确认的 IFWI build → 按路径 A 的方式拿 stitch tool → 运行 stitch。

**暂不做**（后续阶段）：
- OEMID 解析失败时的追问式交互（4.2.2）先做最简：返回失败原因让 host 侧模型向用户追问，不做复杂启发式。

## 3. 关键外部依赖发现

### FIV Portal（Django REST API，`/app/rest/*`）
- `get_project_info/` → 项目列表（映射"项目A" → project_id）。
- `get_ifwi_release/`（params: project_id, phase, version）→ 把自然语言版本解析到具体 build（按 ww_name + phase）。
- `get_ifwi_release_package_info/`（params: project_id, phase, version）→ 返回 `release_root` 与 `build_target[]`；每个 target 含 `binary_list[]`。
  - **IFWI .bin 下载 URL = release_root + package_path + full_binary_name**。
- `get_ingredient_detail/`（params: project_id, ingredient_name, ingredient_version）→ ingredient 的 Artifactory 链接（`ingredient_link`）。
- **无专门的 stitch tool 字段**：stitch 在 portal 里是 task type，不是可下载 target。故通过扫描 `build_target[]` 中 package_name 含 "stitch" 的包来定位（4.1.1/4.1.2 合并为一次扫描）。

### Stitch tool（FIV 驱动路径下）
- 是从 Artifactory **下载的压缩包**，解压后**直接可运行**，内含 `cli.py` 与 `requirements.txt`。
- **不需要** 运行 `stitch_tool_generator.py`（那只用于从源码构建，位于 `~/Workspace/IFWI/OakStreamAPIfwi/Ifwi/oak_release/BuildScripts`）。
- 运行方式：
  ```
  python3 cli.py \
    --binary_file <ifwi.bin> \
    --ingredient_name <name> \
    --ingredient_path <path> \
    --config_ini config/Config_Stitch_<target>.ini \
    [--soft_strap "btg:Dma=1,..."]
  ```

### OEMID 解析（本地文件路径下）
- 参考 `~/Workspace/IFWI/OakStreamAPIfwi/Ifwi/oak_release/BuildScripts/stitch/diagnostics/binary_hash.py`，其 `--parse_oem_info` 模式可解析本地 IFWI 文件的 OEM 区，输出：
  ```
  ======== IFWI Entry Dump ========
  Product: OKSDCRB1
  IFWI Version: .2026.28.3.01
  Flavor Description:
    Value: _1P0_NonIPClean_Trace_DebugSigned
    Type: official
  Hash Value: 902b6636...
  ================================
  ```
- **依赖极小**：`binary_hash.py` 仅依赖标准库 + `defs.structure`；`structure.py`（同目录 `defs/`）也**仅依赖标准库**（struct/json/os/re/dataclasses/typing）。
- **策略：拷贝抽取**。把 `parse_oem_info` 相关逻辑 + `structure.py` 中必要的 dataclass（Product / IFWIVersion / FlavorDescription / IFWIEntry 等）抽成 MCP 内部的**零第三方依赖模块** `oem_parser.py`，不依赖任何已解压 stitch tool 或 venv。代价：stitch tool 更新 OEM 结构格式时需手动同步——在模块顶部注明来源文件与同步方式。

## 4. 架构与模块边界

```
ifwi-stitching-mcp/
  server.py            # FastMCP app：注册工具，薄封装层
  fiv_portal.py        # FIV Portal REST 客户端（查询 projects/builds/ingredients/stitch包）
  downloader.py        # Artifactory 下载 + 本地文件拷贝 → 本地缓存
  oem_parser.py        # 零依赖：解析本地 IFWI 的 OEMID → Product/IFWIVersion/Flavor
  stitch_runner.py     # 解压 stitch 压缩包 + 建 venv/装依赖 + 运行 cli.py
  config.py            # 环境变量配置：FIV base URL、tokens、缓存目录
```

每个模块单一职责、接口清晰、可独立测试：
- `fiv_portal` 只懂 HTTP + JSON。
- `downloader` 只懂 URL/路径 → 本地文件。
- `oem_parser` 只懂 bytes → 结构化 OEM 字段（无网络、无 subprocess）。
- `stitch_runner` 只懂 zip/venv/subprocess。
- `server` 把它们组装成 MCP 工具。

**数据流 A（FIV 驱动）：**
```
NL 请求 → 解析 project + version → fiv_portal.get_build()
  → downloader：IFWI .bin + ingredient → 本地缓存
  → fiv_portal 扫描 build_target[] 中含 "stitch" 的包 → downloader 下载压缩包
  → stitch_runner：解压 → 建 venv 装 requirements → 运行 cli.py → stitched .bin
```

**数据流 B（本地文件 + OEMID 反查）：**
```
本地 IFWI 文件 → oem_parser：解析 Product + IFWI Version + Flavor
  → fiv_portal：按 product+version 找 release → 按 flavor 定位确认的 build
  → 之后并入数据流 A 的下载与 stitch 环节
```

## 5. MCP 工具

工具都保持小而单一，便于 host 侧模型组合调用与从局部失败中恢复。

**FIV Portal 查询：**
- `fiv_list_projects()` → 项目名 + id。
- `fiv_find_ifwi(project, phase, version)` → 解析到具体 build，返回 build 摘要 + IFWI 二进制的 Artifactory URL。
- `fiv_find_ingredient(project, name, version)` → ingredient 下载 URL。
- `fiv_find_stitch_tool(project, phase, version)` → 扫描 build 的 `build_target[]`，返回名字含 "stitch" 的包 URL。
- `fiv_match_build_by_oem(product, ifwi_version, flavor)` → 路径 B 反查：按 product + ifwi_version 找 release，按 flavor 定位到确认的 build，返回同 `fiv_find_ifwi` 的 build 摘要。匹配不唯一/不到时返回候选列表交由澄清。

**OEMID 解析：**
- `parse_ifwi_oem(local_ifwi_path)` → 用 `oem_parser` 解析本地 IFWI 文件，返回 `{product, ifwi_version, flavor_value, flavor_type, hash}`；解析失败返回结构化原因（对应 4.2.2，交 host 追问）。

**下载：**
- `download(url_or_path, dest_name?)` → 下载 Artifactory URL 或拷贝本地路径到缓存，返回本地路径。IFWI / ingredient / stitch 压缩包通用。
- `list_local_files()` → 列出缓存已有文件（对应 2.2.1 / 3.2.1）。

**Stitch：**
- `extract_stitch_tool(archive_path)` → 解压 stitch 压缩包到缓存目录；创建 venv 并 `pip install -r requirements.txt`；返回运行目录（含 cli.py）与可用的 `Config_Stitch_*.ini` targets。
- `run_stitch(stitch_dir, binary_file, ingredient_name, ingredient_path, config_ini, soft_strap?)` → 在该 venv 中运行 `cli.py`；返回 stitched `.bin` 路径 + 日志。

编排由 host 侧模型完成，缺参数时向用户追问（如选哪个 config target）。

## 6. 配置与缓存

**环境变量（`config.py`）：**
- `FIV_BASE_URL` — FIV Portal 地址。
- `FIV_TOKEN` — 用户提供的 FIV Portal token（API 认证）。
- `ARTIFACTORY_TOKEN` — 用户提供的 Artifactory 下载 token。
- `IFWI_MCP_CACHE_DIR` — 本地缓存根目录（默认 `~/.ifwi-stitching-mcp/cache`）。

**缓存布局：**
```
$CACHE_DIR/
  ifwi/        # 下载的 IFWI .bin
  ingredients/ # 下载的 ingredient
  stitch/
    <toolname>/venv/    # 每个 stitch tool 自己的 venv（缓存复用）
    <toolname>/Output/  # 解压后含 cli.py 的运行目录
  work/        # run_stitch 的临时工作目录 + 产物 + 日志
```

**venv 策略：** 每个解压后的 stitch tool 配一个 venv；`extract_stitch_tool` 建 venv 并装依赖，`run_stitch` 复用。

## 7. 错误处理

每个工具返回**结构化结果**，失败时把可操作信息交回 host，让模型能追问或换路径，而非抛裸异常。

- **FIV 查询未命中**（项目名/版本解析不到）→ 返回候选列表（项目列表或相近版本），让模型澄清。
- **认证失败（401）** → 明确提示 token 缺失/过期，并区分是 FIV 还是 Artifactory。
- **下载失败** → 返回 HTTP 状态 + URL；网络类错误建议重试。
- **stitch tool 未找到**（扫不到含 "stitch" 的包）→ 返回全部可用 package 名，交由判断。
- **run_stitch 失败** → 返回退出码 + 截断的日志尾部；完整日志留在 `work/`。
- **venv/pip 失败** → 返回 pip 输出，提示依赖/网络问题。
- **OEMID 解析失败**（无有效 OEM 区/格式不符，4.2.2）→ 返回结构化原因，交由 host 向用户追问补充信息。
- **OEM 反查不唯一/未命中** → 返回候选 build 列表（含 flavor），让模型澄清。

## 8. 测试策略

- `fiv_portal.py` — 录制 JSON 响应做单元测试（mock `requests`），覆盖查询、URL 构造、stitch 包扫描、OEM 反查匹配、未命中分支。
- `downloader.py` — mock HTTP + 本地拷贝；验证缓存路径。
- `oem_parser.py` — 用真实（或截取的）IFWI OEM 区字节样本做单元测试，验证 Product/IFWIVersion/Flavor 解析与非法输入分支；零依赖便于直接测。
- `stitch_runner.py` — 用桩 `cli.py`/`requirements.txt` 验证解压、venv 创建、subprocess 调用与日志捕获，不依赖真实 stitch tool。
- `config.py` — 环境变量读取与默认值。
- 端到端手动验证：路径 A（FIV 驱动）与路径 B（本地文件 + OEM 反查）各跑一次真实闭环，作最终确认。

## 9. 依赖

`fastmcp`、`requests`；标准库 `zipfile` / `tarfile` / `venv` / `subprocess` / `struct`。

## 10. 后续可选增强

- OEMID 解析失败时的启发式追问（4.2.2 的进阶交互）。
- 在 MCP 之上加一层薄 skill，沉淀标准编排流程。
- `oem_parser.py` 与上游 `binary_hash.py` / `structure.py` 的自动同步/校验机制。
