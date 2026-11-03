# 本地模式验证矩阵

**日期：** 2026-09-11
**状态：** 待评审草案
**范围：** IFWI stitch plan 的本地执行模式（`execution.mode = "local"`）。

## 目标

建立可追溯的端到端基线，验证基础 IFWI 与 ingredient 的四种来源组合。Stitch tool
是独立输入：默认从 FIV 目标 release 获取，也可选相邻 release 或本地 tool。

| ID | 基础 IFWI 来源 | Ingredient 来源 | 主要覆盖风险 |
| --- | --- | --- | --- |
| LM-1 | FIV Portal | FIV Portal | 全链路 FIV 托管流程 |
| LM-2 | FIV Portal | 本地路径 | FIV tool config 解析本地 ingredient |
| LM-3 | 本地路径 | FIV Portal | 本地 IFWI 与已发布 ingredient 的兼容性 |
| LM-4 | 本地路径 | 本地路径 | 完全本地/离线输入流程 |

## 通用验收证据

每次运行均记录以下信息：

| 项目 | 需留存的证据 |
| --- | --- |
| 测试身份 | 用例 ID、日期、执行人、project、phase、目标 config |
| 基础 IFWI | 来源、路径或 FIV URL、版本、选定 binary、SHA-256 |
| Ingredient | 名称、来源、路径或 FIV URL、请求版本、实际选定 payload、SHA-256 |
| Stitch tool | 来源、FIV release/version（如适用）、package URL/path、入口脚本、选定 config |
| Plan | `plan_id`、plan JSON 路径、命令预览 |
| 环境准备 | 解析路径、已获取 artifact、warning、tool venv/dependency 结果 |
| 执行 | 命令行、exit code、workspace 路径、完整 log 路径 |
| 结果 | Stitched binary 的路径和非零大小、manifest、archive、output SHA-256 |

成功用例必须满足：

1. `stitch_build_plan` 保存的 plan 之 `sources` 与目标来源组合一致。
2. `stitch_prepare_environment` 解析为预期本地路径，且不下载声明为 local 的输入。
3. `stitch_execute_plan` 返回 `exit_code = 0` 并生成非空 stitched binary。
4. Log、`manifest.json`、配置要求的 deliverables archive 可获得，且 manifest checksum
   与 artifact 一致。
5. Ingredient 选择或替代 tool 选择产生的 warning 均已检查并记录。

## 统一流程

每个基线用例按以下顺序执行：

1. 选择 `project`、`phase`、IFWI release/version（适用时）、`swimlane`（适用时）及
   `Config_Stitch_*` target。
2. 确定测试 ingredient 清单。基线从单 ingredient 开始，明确其预期版本和 payload。
3. 按 [Stitch Tool 选择](#stitch-tool-选择) 完成 tool 的确认。
4. 按所选用例来源调用 `stitch_build_plan`。
5. 检查命令预览及 saved plan：一个 IFWI source、每个 ingredient 一个 source entry、
   一个 stitch-tool source。
6. 调用 `stitch_prepare_environment`，将实际路径和 fetched entries 与预期比对。
7. 调用 `stitch_execute_plan`，收集通用验收证据。

## Stitch Tool 选择

### ST-1：目标 IFWI release 的 FIV tool

1. 使用确定的 `project`、`phase`、`version`、`swimlane` 调用
   `fiv_prepare_stitch(project, phase, version, swimlane)`。
2. 确认 `prepared = true`，并检查 `selected_version`、`stitch_dir`、`venv_python`，以及
   `config_targets` 包含目标 config。
3. 记录 artifact URL/package name、dependency source，以及入口是 `cli.py` 或 legacy
   `stitch2.py`。

### ST-2：相邻 release 的 FIV tool

1. 先按 ST-1 查询目标 release。
2. 如返回 candidates，检查 candidate 的 version 和 package URL，不允许自动选择。
3. 人工选择兼容版本后再次调用 `fiv_prepare_stitch`，并在 plan 设置 `stitch_version`。
4. 记录 IFWI/tool version 差异、选择理由及全部 warning。

### ST-3：本地 stitch tool

1. 以 archive 或解压后的 directory 作为 `stitch_path`。
2. Archive 必须经 `extract_stitch_tool` 确认可发现 `cli.py` 或 `stitch2.py`、venv 创建成功、
   dependency source 明确；directory 必须确认入口脚本和 config 文件存在。
3. 检查 `sources.stitch_tool.kind = "local"`，且准备环境后无 FIV stitch-tool 下载。

## LM-1：FIV IFWI + FIV Ingredient

### LM-1：确认 IFWI

1. `fiv_list_swimlanes(project, phase)`：多个 candidate 时明确选择 swimlane。
2. `fiv_list_releases(project, phase, swimlane)`：确认目标 version 存在。
3. `fiv_find_ifwi(...)` 后调用 `fiv_list_ifwi_binaries(...)`。
4. 多 binary 时明确选择并记录 `binary_name`；使用 build-target fallback 时明确记录。
5. `fiv_prepare_ifwi(...)`：检查 `ifwi_url`、`binary_file`、binary inventory；确认缓存文件
   非空并记录 SHA-256。

### LM-1：确认 ingredient

对每个 ingredient：

1. 选择精确 `name` 和 `version`。
2. `fiv_find_ingredient(project, name, version)`：返回 name/version 必须完全匹配请求。
3. `fiv_prepare_ingredient(project, name, version)`：确认 `ingredient_dir` 存在。
4. 检查 `extracted_files`，确定 payload，记录其路径和 SHA-256。

### LM-1：创建、准备并执行

1. 使用 FIV IFWI version/binary 与 `{name, version}` ingredient entries 建 plan。
2. 检查 `ifwi.kind = "fiv"`、每个 `ingredients[*].kind = "fiv"`。
3. 准备后确认 `fetched` 包含 IFWI 及所有 ingredient URL。
4. 按通用退出准则验收执行结果。

## LM-2：FIV IFWI + 本地 Ingredient

### LM-2：确认 IFWI

按 LM-1 完成 IFWI 的 release、swimlane、binary 选择和 `fiv_prepare_ifwi` 文件确认。

### LM-2：确认 ingredient

对每个本地 ingredient：

1. 记录 name、来源 owner、version 和提供的 path。Path 可以是 payload file 或解压 directory。
2. 确认 path 存在且可读。Directory 必须列出候选 payload；拒绝空目录或只有 archive 的目录。
3. 检查选定 stitch config 中该 ingredient section 的 `regex_mandatory_dict` keys。
4. 预先记录预期选择：唯一严格 match、多个严格 match 的确定性选择及 warning，或无严格
   match 时的 best-effort 选择及 warning。
5. 使用 `{ "name": "<name>", "path": "<local path>" }` 创建 ingredient plan entry。

### LM-2：创建、准备并执行

1. 检查 `ifwi.kind = "fiv"`，所有本地项为 `ingredients[*].kind = "local"`。
2. 准备后确认 `fetched` 中没有 FIV ingredient URL。
3. 检查 `ingredient_args` 或渲染命令，确认实际 mapping 是预期的
   `{regex_key: absolute_path}`。
4. 按通用退出准则执行和验收，ingredient resolution warning 必须评审。

## LM-3：本地 IFWI + FIV Ingredient

### LM-3：确认 IFWI

1. 确认 `.bin` 存在且可读，记录绝对路径、文件大小和 SHA-256。
2. 调用 `parse_ifwi_oem(local_ifwi_path)`，记录 `product`、`ifwi_version`、`flavor`。
   OEM region 不可解析时停止并调查。
3. 已知 project context 时调用
   `fiv_match_build_by_oem(project, product, ifwi_version, flavor, phase, swimlane)`。
4. 记录 FIV match 是唯一、歧义或不存在。无 match 不阻塞 local 执行，但代表来源未确认。
5. 使用 `ifwi_path` 建 plan，确认 `sources.ifwi.kind = "local"` 且路径为绝对路径。

### LM-3：确认 ingredient

按 LM-1 处理每个 FIV ingredient：精确 `name + version` lookup、prepare、检查 payload
inventory，并记录 checksum。

### LM-3：创建、准备并执行

1. 有 OEM-matched release 时使用 ST-1；否则使用人工确认兼容版本的 tool 或 ST-3。
2. 检查 `ifwi.kind = "local"`、每个 `ingredients[*].kind = "fiv"`。
3. 准备后确认 `fetched` 只包含 ingredient，而不包含本地 IFWI。
4. 按通用退出准则验收；IFWI/tool 兼容性以本次 stitch 成功为证，而非仅依赖版本标签。

## LM-4：本地 IFWI + 本地 Ingredient

### LM-4：确认 IFWI

按 LM-3 验证本地 IFWI：存在性/可读性、checksum、OEM parse 和可选 FIV provenance match。
即使 FIV 不可用也必须可执行，这是离线回归基线。

### LM-4：确认 ingredient

按 LM-2 验证本地 ingredient：确认 path、检查 directory candidates，预判 config regex
selection 或 best-effort warning，并记录每个 ingredient 选定的 payload。

### LM-4：创建、准备并执行

1. 使用 ST-3 完成完全本地流程；ST-1/ST-2 可作为后续混合来源变体。
2. 检查 `ifwi.kind = "local"`、每个 `ingredients[*].kind = "local"`，且
   `stitch_tool.kind = "local"`。
3. 准备后确认 `fetched` 为空。
4. 按通用退出准则验收。全部 local path 存在时，运行不应依赖 FIV 或 Artifactory 网络连接。

## 基线通过后的扩展

| 扩展项 | 适用范围 | 验证内容 |
| --- | --- | --- |
| 多 ingredient | 所有 LM 用例 | Pipe-separated names/paths 保持 name 到 payload 映射 |
| 多 IFWI binary | FIV IFWI 用例 | 显式 binary 选择与正确 artifact 下载 |
| 多 swimlane | FIV IFWI/tool 用例 | 歧义被暴露，已选 swimlane 控制所有 FIV artifact URL |
| Ingredient directory 歧义 | 本地 ingredient 用例 | 精确、多 match、无 match 的行为及 warning |
| Tool 兼容性 | 所有用例 | 当前 release、相邻 release、新 `cli.py`、legacy `stitch2.py` package |
| Stitch options | 所有用例 | 每个相关 config target 和合法 `soft_strap` syntax |
| 失败保留 | 所有用例 | Download/auth/setup/non-zero/timeout failure 的错误信息及已创建 log 的保留 |
| 并发 | 至少 LM-1 和 LM-4 | 同时运行的 plan 使用独立 workspace、log、output、deliverables |

## 明确的非目标

本矩阵验证 local execution 和 local deliverable collection。当前 server 从
FIV/Artifactory 下载 artifact，但没有将 stitched result 上传或登记回 FIV Portal 的 API。
回传 FIV 的验证需要另行定义 interface contract、permissions model、metadata schema 和测试计划。
