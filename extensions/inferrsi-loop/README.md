<!-- Public source distribution: site configuration and raw experiment data are intentionally excluded. -->
# InferRSI + 本机 Codex：探索与消融

## 使用入口

在**本机 Codex CLI** 的项目目录输入需求；模型只在本机调用，远端容器运行实验。App 也可使用同一 skill。独立运行使用现有 supervisor；本机仍需开机联网、认证和权限可用。

```bash
cd /你的本机路径/inferrsi-loop
codex
```

若 `codex` 不在 PATH，使用已安装 CLI 的绝对路径，并写入下述启动配置。通用 skill 的仓库源为 `skills/inferrsi-loop/SKILL.md`；复制到 `~/.codex/skills/inferrsi-loop/` 后重新打开 CLI。不按场景拆分 skill。

## 首次使用：从项目模板创建自己的配置

仓库提供两份**不含个人部署路径或凭据**的模板：

| 模板 | 复制后的示例名 | 用途 |
|---|---|---|
| `config/launch.template.json` | 本机 `config/my-launch.json` | SSH、容器、两端输出目录、Codex 模型及飞书入口；启动和访问预检共用 |
| `config/task.template.json` | 远端 `configs/my-task.base.json` | 优化源码、真实权重、profiling、历史来源、测试规则、设备和预算 |

可让 Codex 按下面的首次使用 prompt 填写，也可自行复制编辑。所有 `<大写占位符>` 都必须替换；无对应资料的**可选来源条目整项删除**，不能留下占位符。脚本会拒绝未填模板。模板不依赖任何既有 run、个人 H3 配置或 MLP 捕获。

1. 填写本机 `my-launch.json`，核实 SSH 个人身份、容器及本机 Codex/lark-cli 入口。凭据保留在本机既有认证配置中。
2. 填写任务模板。将文件传到目标容器，使用现有 `prepare_task.py --base` 生成新的任务配置；它核实源码、证据和协议，准备阶段不调用模型、不占卡。
3. Codex 展示生效配置和资料覆盖情况，完成访问预检后，通过 supervisor 启动新 run。后续每轮复用 InferRSI 的冻结、对照评测和证据保存。

任务模板默认：全仓库、真实权重局部测试、2 次预热＋2 次测量、耗时下降至少 1%、3 个候选、1 小时、物理卡 `[0,1]`。这些都是可修改的**模板值**，不是全局硬编码。外层恢复次数由启动参数指定，示例为 3 次。

`examples/local/protocol` 是明确标为未准备的测试骨架；未匹配模块时，由本机 Codex 复用数值/来源校验 helper 准备局部 adapter，迁移预检通过后才冻结评测。它不会用空测试宣布成功。容器须已有可运行的推理环境和 InferRSI；本模板不是模型部署或通用原生算子构建器。

## Prompt 中哪些内容需要说明

| 信息 | 首次使用 | 后续任务 |
|---|---|---|
| 目标和测试场景 | 必填：探索、profiling、消融或查看 | 必填 |
| SSH 入口、主机/用户、容器、两端 loop 路径 | 提供，或允许 Codex 从指定连接指南和当前项目核实 | 指定 `my-launch.json` 即可 |
| 目标源码、权重、推理 Python | 提供实际容器内路径，不能引用作者的部署代替 | 指定任务基础配置即可 |
| profiling 与历史 | reuse/adaptive 给文档或证据路径；另给手动记录、loop 记录及历史实验目录 | 配置可省略重述；新 run 会刷新已声明的知识来源 |
| 记录文档、模型 | 提供记录 URL；模型可让 Codex 核实本机已有服务和模型名后填写 | 从明确指定的配置读取 |
| 卡号、次数、收益、预算 | 可覆盖模板值；未指定则展示模板值后使用 | 可覆盖，但生成新任务配置 |
| 可编辑范围 | 可省略，默认整个目标项目 | 可缩小，不能扩大到资料目录 |
| 数值规则 | 优先适用模块 CI；无规则时 `exact_or_review`，不能编造通用容差 | 使用冻结规则 |
| 参考目录、旧实验目录 | 有则填写，无则删除可选项；读取范围与可编辑范围分开 | 可在新配置中补充 |

可让 Codex 查找工作区说明、PATH 和用户指定的连接/模型配置入口；不搜索凭据内容，不把“最近一次运行”或个人示例作为未知用户的默认设置。只有缺少影响有效性或授权的信息时才询问。

## 可直接使用的 prompt 模板

以下输入**本机 Codex CLI**。首次模板中替换尖括号；后续模板中的配置必须是上一步创建的自己的文件。不是要求用户先拥有别人的配置。

### 第一次：创建配置并使用已有 profiling

```text
使用 $inferrsi-loop，从本项目 config/launch.template.json 和 config/task.template.json 创建我的配置，不使用个人部署示例。
目标：优化 <模型名>；SSH 指南 <本机文件>，主机 <别名>、个人用户 <用户>；容器 <名称>；远端 loop <目录>、loop Python <路径>；源码 <目录>、真实权重 <目录>、推理 Python <路径>。
复用 profiling <文档或文件>；历史来源为 <手动/loop 文档及远端实验目录>；记录到 <飞书 URL>。本机 CLI、模型和 lark 入口请从现有安装核实，缺项再问我。
全仓库探索，只做真实权重局部单测和数值验证，避免重复失败方案；卡 <编号>，2+2，耗时下降至少 1%，最多 3 个候选/1 小时/恢复 3 次。保存并展示新配置，通过预检后后台执行。
```

### 已完成配置：继续探索

```text
使用 $inferrsi-loop，启动配置 config/my-launch.json，远端基础配置 <我的 task.base.json>。复用已有 profiling，刷新并检索已配置的全部历史来源，避免重复失败方案；全仓库仅真实权重局部性能和数值验证。新建 run，3 个候选/1 小时/最多恢复 3 次；其余读取这两份明确配置，展示生效参数后后台执行并记录。
```

### 从零采集 / 按需补采

```text
使用 $inferrsi-loop，启动配置 config/my-launch.json，远端基础配置 <我的 task.base.json>。本次先串行采集 torch profiler 和 xprofiler，分析阶段、算子与 kernel gap，再探索；计时对照关闭 profiler。复用项目已有采集脚本，缺少工作负载适配先补齐。新建 run，3 个候选/8 小时，单次采集超时 3 小时，最多恢复 3 次；其余读取上述配置。
```

允许按需补采时，将“本次先串行采集……”替换为“先复用指定 profiling；模型说明证据缺口后允许补采，最多 1 次”。`profile.mode` 对应 `collect` 或 `adaptive`，采集脚本、请求和预算须在执行前配置，不能仅修改模式就声称能采集任意模型。

### 有候选：做消融

```text
使用 $inferrsi-loop，启动配置 config/my-launch.json，远端基础配置 <我的 task.base.json>。从 <候选文档/结果路径> 读取优化项和补丁，先核实原始基线、依赖与 short request 协议，再新建“全部组合＋逐项移除”的消融矩阵。只做 short E2E，每侧 2+2，不重新 profiling、不搜索新优化，保留负收益结果；8 小时/最多恢复 3 次。展示配置和矩阵后后台执行并记录。
```

局部探索模板没有 short E2E adapter。消融前必须复用或补齐适用的 `protocols.short` 及真实请求；缺失时明确准备，不回退到任意 E2E 测试。

### 查看 / 恢复

```text
使用 $inferrsi-loop，启动配置 config/my-launch.json，检查 run <准确 ID> 的进度、候选数、有效实验数和失败原因；只读，不启动实验。
```

要恢复时追加：“若无在途控制进程，按该 run 的冻结配置和剩余预算恢复；只补传缺失记录，不另开探索。”

`config/launch.template.json`、`h3-93.task.json`、`h3-93.profile.json`、`h3-93.knowledge.json` 是作者当前 MiniMax-H3/93 的**完整填写示例**，用于查看字段关系。仅实际使用该部署时才选择它们；普通首次用户使用上面的空模板。

## 配置如何决定执行行为

| 选择 | 配置 | 行为 |
|---|---|---|
| 探索 | `task: explore`，`exploration.validation: local_only` | 真实权重局部性能＋模块数值验证，达标登记候选，反馈后继续 |
| 消融 | `task: ablate`，`stages: ["short"]` | 固定组合的 short E2E 对照，不在执行中搜索新优化 |
| 复用 | `profile.mode: reuse` | 只读指定证据，拒绝补采 |
| 新采集 | `profile.mode: collect` | 本轮开始先采集；恢复运行不重复采集 |
| 按需补采 | `profile.mode: adaptive` | 先复用；模型可在 `max_refreshes` 内申请采集 |
| 不采集 | `profile.mode: skip` | 仅用于不需要 profiling 的固定消融 |

不同场景共用 Controller、Runner、InferRSI Evaluator 和产物格式。由一个通用 inferrsi-loop skill 编排，不按场景拆分 skill 或复制执行逻辑。

主要模块按当前代码组织：

| 模块 | 实现与位置 |
|---|---|
| Adapter | 本机 `adapter.py`、`preflight_access.py`：模型入口与访问预检 |
| Planner | 本机 `planner.py`：读证据、生成 `proposal.json`、`rationale` 和候选补丁 |
| Protocol | 本机 `local_tests.py` + 93 `lifecycle.py`、`protocol.py`：补资料、准备/修复测试、冻结前预检 |
| Runner | 远端 `runner.py`：从配置的 allow 列表中分配空闲卡、执行作业并保存输出 |
| Evaluator | 93 `evaluator.py`：复用 InferRSI campaign，验证数值与性能证据并导出判定 |
| Controller | 本机 `supervise_h3.py`、`local_driver.py` + 93 `controller.py`、`ssh_step.py`：调度、预算与恢复；`profiling.py` / `ablation.py`处理对应任务 |
| Reporter | `report_results.py` 收集 93 结果；本机 `reporter.py` 生成独立小节、表格并保留尝试原因 |

```text
Prompt → 本机 Codex 准备新 config.json / task.txt → Supervisor / local_driver
  → 93 Controller：冻结源码、资料、预算；按配置采集或复用 profiling
  → 本机 Planner（经 Adapter 调模型）→ 候选源码 + proposal.json + patch
  → Protocol：补资料 / 生成局部测试 → 93 迁移 baseline 预检
  → 93 Evaluator + Runner → InferRSI campaign → decision.json
  → Controller：反馈下一候选，或同候选修协议后重评，或预算结束
  → Reporter：results.json → 飞书独立 run 小节 + sync-receipt.json
```

固定消融走 Controller 的明确变体矩阵，不调用 Planner 搜索新候选。协议修复和资料回补不增加候选数；有效实验数只统计形成完整有效性能指标的候选。

### 测量与可编辑范围

- `project.editable` 默认 `["."]`；可通过 `--editable` 缩小范围。评测协议、输入、CI 门槛与基线单独冻结，候选修改项目中的测试文件不会改变冻结验收。
- InferRSI 接收已隔离的项目目录边界，因此新增或删除源码文件也会进入候选执行快照；不会只复制基线已有文件。大源码包通过 SSH 压缩传输，截断后的只读查询可重试，实验提交和执行不盲目重发。
- 探索阶段所有优化都只跑对应模块的局部单测，不能回退到 short/official E2E。`screen_routes` 只描述已有测试的覆盖范围，不限制编辑范围。未匹配的全仓库改动进入 `awaiting_local_test`；本机 driver 自动调用 Codex 读取基线、候选、现有测试及模型元数据，准备匹配模块的局部测试，经 SSH 传到 93 冻结后继续评测。跨模块改动需覆盖所有受影响路径，不能用无关算子的收益替代。
- 新局部测试在首次评测前冻结，基线与候选共用。沿用当前任务的收益门槛、超时、测量计划与配置的设备范围；生成的 adapter 不能自行更改这些设置。权重留在 93，本机只读源码和元数据。模型缺资料时先在白名单内自动回补，协议故障按版本修正并重新预检；预算耗尽或缺口不可恢复时，记录为 `local_test_unavailable`（未测量、未验证）。模型调用异常交给有限恢复调度处理。
- 有参数模块的局部测试必须使用真实模型权重或推理时直接捕获的权重；输入可以固定，无参数算子也可使用固定输入或真实中间张量。权重/输入文件、捕获脚本及来源记录的指纹保留并校验；禁止随机生成权重。已有 MLP 数据来自 TP2 的 `RowParallelLinear.self.weight` 和真实输入捕获，不能据此声称覆盖任意层或输入。
- 缺少当前真实请求的精确 packed 长度，不应单独阻止局部验证：可依据当前源码语义和模型元数据构造确定性固定输入，记录源码位置、元数据、所选长度/head 数/dtype/布局与步长及分支条件，区分源码事实与测试选值。基线与候选共用冻结输入并实际覆盖修改分支；结果明确标为“固定输入局部结果，真实负载代表性未验证”，不能外推整模型收益。正确性必需的维度或语义仍不明确时才请求具体资料；边界小样本不冒充代表性性能输入。有参数部分仍使用真实权重，修复旧协议仍保留原输入字节与哈希，收益和数值门槛不变。
- 每个探索候选独立基于本轮冻结基线，局部达标后登记到 `candidate-optimizations.json`，不自动合入下一轮基线。组合收益在消融阶段测量。
- Python 算子源码可以直接进入上述流程。原生源码、头文件或构建配置变化必须使用重编译并验证加载的协议；缺少该适配时阻止测量，不使用旧二进制冒充候选。当前没有通用 C++/XPU 自动构建器。
- `warmups`、`measurements` 统一写入本轮 screen/short/full 协议，控制性能阶段的次数。CI 的检查次数与 profiler 的一次预热/一次采集不是性能重复次数。
- short 使用原有短请求和基本视频有效性检查；official 阶段使用原有精度、质量 CI。short 通过不等于官方质量通过。
- 正式性能及新采集中的无 profiler baseline 默认要求主机设备无其他负载。设备选择遵循 `resources.allow/exclude`，H3 示例仅允许物理卡 0、1。
- 局部收益模板默认 1%，可由 prompt 设置并在新配置中冻结。局部性能与模块数值验证均通过才登记候选；数值待确认者不计入候选。消融阈值只用于报告筛选，负收益也保留。

### 数值精度如何判断

数值对比是必需的，统一固定误差阈值不是必需的：

1. 检查形状、类型、有限值和模块语义边界，再对相同权重、相同输入比较参考与候选输出。
2. 优先复用适用于该模块的已有精度 CI 断言，记录出处和规则。不会将历史 `atol=1e-5 / rtol=0.016` 重新设为通用门槛。
3. 模板及当前 MLP 回放使用 `exact_or_review`：输出数值一致即可通过；出现非零差异则记录全输出 max-abs、MAE、RMSE、relative-L2，标为 `pending_numerical_review`，不自动判成功或失败，继续探索其他方案。
4. 对允许浮点差异的方案，事先根据模块已有标准、参考路径误差和必要的 FP32/FP64 对照制定规则，冻结在新任务中再评测，不能按候选结果放宽规则。`exact` 适用于明确要求一致的模块；`limits` 支持带依据的逐指标上限；更复杂断言复用模块 CI adapter。

现有 MLP 参考为原始 `F.linear + TP2 all_reduce`。这些判定仅覆盖当前局部样本，不是整模型视频/音频质量保证。性能和数值是独立条件；两次测量也不代表统计显著。

### 消融计划示例

源码目录由本机 Codex 根据文档中的实际改动准备。`baseline` 来自本轮 `project.path`，必须确认为不含待消融优化的基线。

```json
{
  "design": "leave_one_out",
  "factors": ["A", "B"],
  "stages": ["short"],
  "min_gain_pct": 3,
  "selection_stage": "short",
  "variants": [
    {"name": "all", "enabled": ["A", "B"], "source": "/绝对路径/all"},
    {"name": "no_A", "enabled": ["B"], "source": "/绝对路径/no_A"},
    {"name": "no_B", "enabled": ["A"], "source": "/绝对路径/no_B"}
  ]
}
```

自动展开为 `baseline → all`、`all → no_A`、`all → no_B`。每条对照仅运行 short E2E。`min_gain_pct: null` 表示不筛选；阈值作用于 short 协议主指标，即两个用例平均耗时之和。每用例 P95/吞吐及原始样本另行保留，不能将主指标阈值解释成 P95 阈值。

其他设计使用 `design: explicit` 和 `comparisons: [{"reference":"baseline","candidate":"A"}, ...]`。不自动生成所有组合，避免无意扩大实验预算。当前变体以源码目录表达；环境开关型优化需要先由 Codex 准备可验证的源码/启动适配，不能只填写名称。

## 模型与内部执行入口

`my-launch.json` 的 `codex/model/effort` 决定本机调用；必须与任务配置的 `model.name/effort` 一致。服务与登录由本机 Codex 管理，不把认证信息传到远端。作者 H3 示例填写 `gpt-6-astra/medium`，其他用户自行选择其可用模型。

以下命令供 Codex 调用和排障，日常用户使用上面的 prompt。尖括号均需替换为自己的路径。

远端容器准备任务（`base.json` 是填好的任务模板）：

```bash
<loop-python> <remote-loop>/scripts/prepare_task.py \
  --base <my-task.base.json> --output <new-task.json> \
  --task explore --profile reuse --warmups 2 --measurements 2 \
  --rounds 3 --seconds 3600 --local-min-gain-pct 1
```

本机启动；只整理已有结果时添加 `--report-only`：

```bash
python3 scripts/run_h3.py --launch-config /absolute/config/my-launch.json \
  --run-id my-task --remote-config /absolute/remote/new-task.json --wait-for-devices
```

常用准备选项：

- `--evidence`：实际远端 profiling 摘要文件；文档 URL 应由 Codex 读取并核实原始路径，不能直接当文件传入。
- `--knowledge-sources`：显式来源列表，替换基础配置中的 `knowledge_sources`；不会自动扫描整台机器。
- `--project`、`--editable`：源码和可编辑范围。参考、权重、历史文件保持只读。
- `--local-protocols`：注册已有模块 adapter；省略则为未覆盖模块自动准备局部测试。模块数值规则通过其 `local_validation.numerical_rule` 冻结；`--numerical-rule` 是已有 H3 MLP 适配器的便利入口。
- `--profile collect/adaptive`：已有 H3 协议复用双 profiler 适配；其他协议须先在基础配置的 `profile` 中提供 `directory/argv/resources/timeout_seconds`，collector 在 `{output}` 生成 `summary.json`。脚本目录冻结，复用同一 Runner；没有适配会明确报错。
- `--task ablate --profile skip --ablation-plan <file>`：要求已有 short 协议和明确源码变体矩阵。

采集超时需小于总预算。`make_h3_config.py` 保留为作者原部署的兼容助手，依赖其 H3 请求、捕获和目录，**不是首次使用的通用配置生成器**。旧 run 继续使用冻结条件，不能改 manifest 绕过运行时校验。

## 产物、验证与扩展

- 容器、项目路径与 Python 来自启动配置；原始证据位于 `<remote_runs>/<run-id>`，本机汇总位于 `<local_runs>/<run-id>/results.json`。
- 新 profiling：`jobs/profile-000`；按需请求：`profiling/request-*.json`；补采：`jobs/profile-001` 等。原始 trace、分析、版本、源码哈希和无 profiler baseline 分开保留。
- 消融：`comparisons/<序号>/<阶段>/comparison.json`，汇总在 `results.json.ablation`；`selected_factors` 仅表示达到所设筛选阈值，不是自动采纳。
- 探索：`candidate-optimizations.json` 和 `results.json.candidate_optimizations` 登记局部合格项，包含补丁、来源、基线/候选哈希及判定证据；`pending_numerical_review` 与失败项单独保留。`accepted` 仅兼容历史完整验证结果，不把局部候选标成整模型已验收。
- 新模块局部测试：`rounds/<轮次>/local-test-request.json`、`local-test/` 和 `local-test-receipt.json`；本机 `local-test-preparation/` 保留模型调用记录。测试文件或候选被改动时拒绝继续使用旧凭据。新增数值容差仍需在任务配置中有对应模块的依据，自动补测试不允许临时放宽容差。
- 实验记录写入启动配置 `document` 指定的已授权飞书文档，不另建 Markdown 实验报告。H3 示例指定 <REPORT_DOCUMENT_URL> 。

验证覆盖 profiling 策略、固定消融、2+2 配置、整个项目范围、原生构建证明、局部候选登记、禁止 E2E 回退及数值待确认分支。真实 InferRSI CPU 测试用于验证调度，不代表 H3 性能结果。新双 profiler 适配、局部数值判定和 H3 short 消融须区分预检与真实设备验收。

运行测试：

```bash
<loop-python> -m unittest discover -s <remote-loop>/tests -v
```

原生构建适配扩展：可信的冻结 protocol 设置 `native_build_contract: true`，在隔离输出目录重编译，并在每个 quality/measurement 结果中返回 `native_build.source_hash`（对应输入源码指纹）及 `loaded_artifacts`（实际加载路径、SHA-256、`loaded: true`）。Evaluator 核对证据；单独打开配置标志不能通过验证。源码树不能被构建产物污染。新的局部测试或其他任务适配优先复用现有 Runner/Evaluator，而非增加一套 agent 或 skill。

## 独立终端运行与恢复

Prompt 输入到**本机 Codex CLI 的交互会话**；App 也能准备相同任务，但无人值守使用终端启动。CLI 必须具备当前工作目录、个人 SSH、模型及飞书认证和所需网络权限。内部恢复 CLI 使用 `--approve-for-me`，仍遵守本机 sandbox；需要人工审批的权限不能自动获得。应在首次启动前验证这些能力，不要关闭安全校验或复制凭据到 93。

首次启动先填写两份配置模板，由 Codex生成本次新配置与 task.txt。恢复任务则明确指定 run ID及冻结配置，不依赖当前聊天或“最近一次”的猜测。

Codex 负责准备文件并执行下列入口，用户无需手工填写 Python 参数：

```bash
python3 scripts/supervise_h3.py --task-file /absolute/run/task.txt --recoveries 3 -- \
  --launch-config /absolute/config/my-launch.json --run-id <new-run-id> \
  --remote-config <remote-config.json> --wait-for-devices
```

入口以独立 session 启动后台 Python supervisor，stdin 脱离终端、日志写入运行目录；macOS 上使用 caffeinate 防止空闲睡眠。关闭 App 或原终端不会通过终端挂断结束该进程；关机、重启、合盖睡眠及网络中断仍会影响运行，不自动跨重启恢复。

复用链路为 supervisor → run_h3 → local_driver → 本机 CLI / SSH / InferRSI。失败时按 `--recoveries` 上限调用本机 CLI 排查并重试同一运行；同步失败仅补传，不重跑实验。正常完成和预算结束不另开探索。冻结协议错误由 lifecycle 保留旧证据并创建修正版评测；权限缺失或外层恢复耗尽记录为 needs_attention，不伪造成功。重新启动同一命令可读取现有运行继续，锁阻止两个 supervisor 同时控制同一运行。

查看运行目录 `supervisor-status.json`、`supervisor.log`、`attempt-*.log`、`recovery-*.log`、`results.json`、`sync-outbox.json`。`task.txt` 保存用户需求和证据位置，`supervisor-config.json` 保存启动参数；不得放入密码或 token。飞书同步依然复用现有 reporter。

## SSH / 飞书入口与权限预检

`--launch-config` 指定的文件同时用于访问预检、driver、reporter 和恢复 CLI；SSH/容器/模型/文档不需要分别改脚本。`ssh_guide` 指向用户的本机连接指南，`ssh` 是已配置的认证入口，`expected_user/expected_hostname` 用于核对身份；`jump_hostname` 可为 null。`lark/lark_skill` 指定本机入口和操作说明。旧 `config/local-access.json` 仅为历史命令兼容，不是首次用户的默认配置。

```bash
python3 scripts/preflight_access.py --config /absolute/config/my-launch.json --output /tmp/inferrsi-access.json
```

supervisor 在后台 worker 中、执行实验前自动运行相同检查；失败写入 `access-preflight.json` 和 `supervisor-status.json`（needs_attention），不消耗探索轮次。仅保存检查结果类别，不保存 CLI 原始输出、文档正文或凭据。飞书检查为读取目录，不修改文档；客户端正常刷新认证仍可能需要本机凭据存储权限。

普通终端成功、Codex 内失败时，应检查沙箱网络/凭据存储权限；两边 DNS 都失败时检查 VPN/DNS。使用用户已配置的个人认证入口，不重置登录、不输出凭据、不关闭主机校验。若支持针对性权限审批，可按失败原因申请后重试，后台不得绕过审批。

预检 ready 仅表示**当前进程读取访问可用**，不证明飞书写权限或未来 token 刷新成功。恢复 CLI 是另一个沙箱，不能假定继承外层批准：在声明无人值守就绪前，让与恢复相同 model、工作目录和 `--approve-for-me` 参数的 CLI 执行上述预检，检查其实际结果；失败则先解决权限或明确保留人工恢复边界。脚本不会自动放宽沙箱。

### 2026-11-25 独立 CLI 验证与兼容修复

当前内置 CLI 不再接受 `--full-auto`，恢复入口使用 `--approve-for-me`：保留 workspace-write，通过自动审批请求必要的网络/凭据访问，不关闭沙箱。独立 CLI 实测默认沙箱预检失败，针对性审批后 DNS、SSH 身份及飞书读取通过。审批被拒绝仍需人工处理。

reporter 在运行目录中调用 lark-cli，确保 `@sync-payload.xml` 位于该工具允许的当前目录内；同步异常返回专用补传状态，不重跑实验。进程可脱离终端运行，但本次没有关闭 App 做完整 GPU 探索验收，不保证关机、合盖或权限变化后的无人值守成功。

独立后台 CLI 已完成目标飞书文档的单条权限验证记录及回读（revision 40），不是推理实验结果。


## 项目知识：手动优化和 loop 历史一起检索

基础配置的 `knowledge_sources` 显式列出文档和目录，每项包含 `id`、`kind`、`url/path`、`environment`；飞书表格可设 `read_sheets: true`。新 run 先在本机读取飞书正文和嵌入表格，再在远端收集实验方案、补丁、结果、失败日志及参考源码；恢复 run 使用原冻结快照，不刷新或修改历史。

复用 InferRSI `knowledge_find` 的 `Corpus/Document/rank` 检索，原有通用 knowledge search 也保留。新增代码只负责项目资料收集和来源保存，不新建一套排序算法。每轮提供相关摘要，同时把**完整目录和收录正文**放到 Planner 与局部测试生成器的只读 `references/project-knowledge/`，要求先检索相近方案、失败原因及差异再提案，不能只依赖前 20 个摘要。

产物位于新 run 的 `frozen/knowledge/`：

- `catalog.json`：原始路径/文档锚点、版本、环境、内容哈希、原记录状态和未收录路径；不把 blocked 改写为优化失败。
- `content/` 和 `documents/`：收录文本及原始文档响应；旧环境只作经验，不自动成为当前性能证据。
- `coverage.json`：每个来源的收录数量、缺失或超限文件。全来源优先收集小型方案/结果/源码，再收日志；单文件默认 2 MiB、总文本 64 MiB。大 trace、权重、二进制及复制工作树不全文收入；遗漏必须报告，不能声称机器上所有资料均已覆盖。

作者 H3 示例明确配置 14 个来源：手动 profiling 文档、loop 文档、新容器 experiments、旧 diagnostics、迁移的 legacy-experiments、旧 MLP loop、当前 recipes、迁移记录、reviews、已有 references/h3，以及旧 review/quality/PR 结果和 Flash Attention 诊断源码。范围包含未写进飞书的远端试验。prepare_task 会把显式声明的目录加入只读资料回补范围；这不会扩大 project.editable。成功、负收益、数值失败、执行失败和待验证记录都保留，使用时核对具体环境和原始指标。

2026-11-25 已核验 14 个来源，手动文档 revision 1450、loop 文档 revision 75 及 19 个嵌入表格；索引含 11,092 条文档/文件记录（不是优化项数量）。`references/h3/knowledge-audit.json` 保存覆盖摘要和真实检索回执，完整索引位于示例容器 `/home/h3-dev-v033rc1/knowledge/inferrsi-history-20261125-v3`。这是有明确遗漏清单的历史快照，不是新增性能验证。

可单独收集或检索而不启动模型/GPU：本机 `scripts/collect_knowledge.py --sources <sources.json> --fetch-documents --output <new-documents.json>`；把快照传到远端后，用同一脚本 `--sources <sources.json> --documents <new-documents.json> --output <new-index>`。检索为 `--output <index> --query "attention swiglu norm"`，需安装 InferRSI 的 Python。

## 手动优化参考资料

`references/h3/catalog.json` 记录手动 profiling 文档 revision 1446 的来源与已核实的 93 资料入口；`guide.txt` 指引按模块读取。源码副本涵盖当前安装的 indexed modulation、RMSNorm、QK/RoPE、H3 blocks/pipeline，历史融合捕获脚本、新版 fusion replay 和版本迁移差异。安装目录源码可能含手动修复，不称为纯净上游。

`checkpoint-headers.json` 仅含真实文件头提取的相关 tensor 键名、shape、dtype 及头部哈希，不含权重；头部哈希不能代替权重内容验证。旧融合中的真实形状合成输入不能充当当前真实权重证明。真实 stride、运行 dispatch 和加载路径仍需在 93 核实。

新配置设置 `reference_directory` 为容器项目下 `references/h3` 的绝对路径。make_h3_config.py 默认填写；手动准备配置时也应填写。Controller 将其复制到冻结目录，Planner 获得查找指南，局部测试准备获得完整参考文件。已有冻结运行不变，不能直接续用旧运行来启用新资料。参考资料不是新的数值门槛，也不解决第三轮路径断言缺陷；该协议修复需单独处理。

### 三轮失败对应的当前版本补充

先读 `references/h3/current-rounds.json`：逐项列出 indexed RMSNorm、head-aware QK norm、masked-unpadding 的当前调用点、权重键名、布局来源及尚缺的动态证据。catalog 的 `use` 区分当前源码、旧思路、版本映射，以及“新版脚本读取旧捕获”的混合来源；旧数据不得直接作为当前验证依据。

Planner 现在也收到独立只读参考目录，测试生成器继续读取同一冻结参考包。新运行冻结前核对 catalog 标为当前源码的原文件哈希，版本变更时要求刷新资料。固定输入仍允许，但必须标明由当前源码构造，不能称为真实 request 捕获。只确认了 FL2VA 的具体权重映射，不推断其他任务使用同一分片。

协议复制后的输入使用 `verify_consumed_input(contract, actual_path)` 按已注册 input 角色和内容哈希验证，不能比较注册目录和执行目录的绝对路径。新增 CPU 回归覆盖复制成功、内容篡改和错误角色拒绝；生成器被要求复用此 helper。旧运行的冻结断言不修改，修正后的协议需在新运行中验证。第三轮仍须证明 masked 分支在目标工作负载命中；packed 路径绕过该改动，不能据局部 gather 提速宣称 H3 有收益。

## 自动回补与版本化评测

新配置的 `local_recovery` 复用原 driver、Runner 和 InferRSI：

- `local_recovery.max_evidence_rounds`：同一候选最多可请求几轮资料，默认 **6**（目录查询也计一次）。在 `config/task.template.json` 或自己的任务基础 JSON 中设置；H3 示例为 `config/task.template.json`。已有配置的显式值优先，新默认不会修改旧 run 的冻结配置。增加次数仍受总时间预算限制，不等于增加探索候选数或外层恢复次数。模型返回 `missing_evidence`（text/list/safetensors_header），远端仅在 `evidence_roots` 中读取有界源码/元数据；不传权重、不执行任意 shell。证据保存在 `supplements/`，重新交给同一候选。修复包保留旧协议中不超过 8 MiB 的文本输入原始字节；日志和一般回补仍限制 2 MiB，不传 checkpoint 权重。`kind=text` 支持白名单目录内的 `.py/.json/.txt/.md/.toml/.yaml/.log` UTF-8 普通文件；实际读取也受大小上限约束，超限明确拒绝，不静默截断。模型须阅读失败回执，不能原样重复被类型、大小或路径规则拒绝的请求；可改读已有的小型元数据并明确剩余限制。相关已知文件应合并请求，目录列表不能代替正文。
- `max_protocol_versions`：测试协议最大版本数。先在 `protocol-versions/vNN/` 暂存，并复制到不同目录执行 baseline 质量预检；通过后才激活为冻结评测协议。预检不计性能实验。
- 正式评测的协议故障创建新版本与新 InferRSI campaign，旧协议、日志和失败结果全部保留；不修改候选、基线和质量/收益门槛。数值不通过及需要数值人工复核不是“修协议直到通过”的理由。
- 外层恢复 agent 返回 retry/report_only/needs_attention，无法修复的确定性问题不再重复启动同一任务。

计数分别为 `candidate_count`（已提出/导入候选）、`completed_candidates`（已处理候选）、`effective_experiments`（有完整有效性能指标的候选）、`protocol_repairs`、`evidence_rounds`。兼容字段 completed_rounds 仍表示已处理候选，不代表有效实验。各种回补/预检/修复共用原时间预算，不增加候选预算。

复核历史失败时可让 CLI 调用 `scripts/prepare_replay.py --original <old-run> --output <new-config> --max-seconds <budget>`，再通过既有 supervisor 启动新 run ID。它导入原候选并保留原第三轮协议用于检验自动修复；不改旧运行。历史方案回放验证不计为新发现优化。独立验证开始后只允许外层读取状态，不能替内部模型修补协议或结果。

## 飞书记录格式

现有 reporter 统一生成每个 run 的独立二级小节，与此前 checkpoint 优化等小节并列。同一 run 重复同步更新原小节，保留其他记录。格式为：时间和 run ID → 优化说明 → 尝试原因 → 结果概述 → 简洁对照表 → 完整证据路径与适用范围。表格列为优化项/阶段、具体改动、基线→候选耗时、耗时下降、数值验证、结论。时间使用 Asia/Shanghai；无测量保留原因，不记作无收益，局部候选不代表 E2E 已验收。`report_results.py` 收集已有证据，`reporter.py` 负责渲染、同步和回读验证；不增加模型或新的记录 skill。

独立验收期间外层只观察；需要调整记录脚本时等运行及 supervisor 结束后修改。历史记录整理不重跑实验、不改原始输入或结果。

“尝试原因”直接复用 Planner 已有 rationale（每项一句简洁中文），由 report_results 收集、reporter 默认写入，无需新增模型调用。旧记录缺 rationale 时沿用原 hypothesis；缺少依据则标为待补充。飞书小节中已有的手工原因优先保留，同步时缓存到本次运行的 report-notes.json；重新收集结果和重复同步不会用自动生成内容覆盖它。纯自动原因会随新候选更新；修订已保留的原因可直接编辑飞书同一段，下次同步会回读保存。

## 参数化局部测试与日志回补

探索使用同一局部测试脚本和冻结的输入 JSON，按模块的源码约束选择多个合理尺寸与适用分支。每个用例记录源码/模型元数据来源、确定约束与测试选值、shape/dtype/stride、分支条件和生成规则。参数模块加载真实模型权重；无参数模块只需有来源的固定输入。调用真实生产实现和后端，不用 mock 证明收益。

缺少真实 request 的精确长度不阻止合法固定输入测试，但结论限定为“固定输入局部验证，真实负载代表性待确认”。先冻结计时范围、各用例次数及汇总方式，再对比原版和候选；保留每个用例的数值和耗时。边界用例用于正确性，不单独代表性能；不得将长度张量创建收益当成整个 attention 收益。prefix 候选覆盖多个合法序列长度、完整/部分 KV prefix。更换请求仅需复用脚本并补充新尺寸；dtype、并行或批处理变化时重新核对分支适用性。探索仍不运行 short/official request。

日志回补继续沿用 `local_recovery.max_evidence_rounds=6`（按请求批次计数）和单文件 `2 MiB` 上限，支持 `.log`；超限明确返回失败，当前未实现定界摘录。模型须读取失败回执，不重复提出条件未改变的拒绝请求。修复后的重试创建新 run，保留旧候选、基线和门槛，旧运行及旧证据不改写。

## Public distribution and validation

This directory is an optional extension; it does not replace native InferRSI. Install it in its own environment with `python -m pip install -e .`. Run its CPU orchestration checks with `PYTHONPATH=src python -m unittest discover -s tests -q`. These checks do not establish H3 accelerator performance or output quality. Native InferRSI remains pinned by pyproject.toml.

Personal deployment JSON, historical source copies, model metadata, raw runs and site bootstrap/replay utilities are excluded. `config/examples/synthetic.launch.json` is a test fixture, not a real deployment. Fill the configuration templates for your own environment. The optional legacy H3 collector has labelled site placeholders; provide your existing collector through the generic profile command interface or adapt those explicit legacy match strings to your frozen script bundle before use. No credentials are stored in this package.
