<!-- Public source distribution: site configuration and raw experiment data are intentionally excluded. -->
# InferRSI loop v3

本机 Codex 负责提出优化和修复测试；93 上的原生 InferRSI 负责冻结、对照评测、选优、恢复已有执行记录及最终复测。v3 在 v2 上增加可选目标调度，保留 v1/v2 及原有按次数探索、单独消融用法。默认开启 Knowledge；未设置 `objective.enabled=true` 时不启用目标驱动。

## v3：目标驱动的自动优化

只增加 `objective.py` 状态调度，复用本机 driver/supervisor、局部测试恢复、原生 campaign 和 ablation。模型、推理强度、API 路由及认证继承方式不变。模型仍仅在本机运行；93 负责实验。

```mermaid
flowchart TD
    A[本机 Codex：prompt + 配置模板] --> B[预检、冻结原始基线和目标请求]
    B --> C[复用或采集 profiling]
    C --> D[检索历史与本 run 经验]
    D --> E[本机模型提出候选；局部性能与数值验证]
    E -->|缺资料或脚本异常| R[有限回补、修复、重新预检]
    R --> E
    E --> F[原生 campaign 对照与 final]
    F --> G[批次结束：兼容组合分别跑 T2VA / FL2VA short E2E]
    G --> H[目标请求：当前最好组合对比固定原始基线]
    H --> I{两任务都达标？}
    I -->|否且有预算| D
    I -->|是| J[累计优化消融：全组合与有效逐项移除]
    J --> K[独立目标复测与质量检查]
    K -->|通过| L[goal_met 或 pending_review]
    K -->|未通过且有预算| D
    I -->|预算耗尽| M[保留最好版本；goal_not_met]
    E -.经验及证据.-> N[Reporter：飞书结果与目标差距]
    K -.最终证据.-> N
```

每批从当前已验证的最好源码出发；用于累计目标比较的原始基线始终不变。局部收益不能相加代替 E2E 加速比。组合对两种任务均不回退才采用。下一批自动读取目标差距、前批结果及失败经验；Knowledge off 也保留本 run 自己的反馈。`adaptive` 补采仍由模型说明缺口，受全程补采次数限制。

目标按任务独立配置：

| 配置 | 含义 |
|---|---|
| `min_speedup: 1.2` | 相对固定原始基线至少 1.2×；延迟是 baseline/candidate，吞吐是 candidate/baseline |
| `threshold` | 绝对目标，单位与测试脚本 `metric_unit` 一致；与 min_speedup 二选一 |
| `screen_stage` | 该任务的 short E2E 组合/消融测试 |
| `validation_stage` | 判断最终目标的测试；指定 official 请求才能声称完整请求达标 |
| `workload` | 冻结请求身份；请求文件及 quality、长度、并行、dtype 等保存在相应测试目录 |
| `batch_candidates` / `max_batches` | 每批候选数 / 最多批数，另受总 `budget.max_rounds` 限制 |
| `batch_seconds` / `verification_seconds` | 每批探索 / 单个组合或验证阶段的最长秒数 |
| `final_reserve_seconds` | 总时间内为最终验证保留的时间；不是额外预算 |
| `ablation_every_batches` | 0：达标后消融；正数：每 N 批额外消融一次 |

目标与局部候选筛选门槛独立。所有任务都要满足目标、无性能回退且通过冻结质量规则；有人工质量审查要求时最终状态为 `pending_review`。少量样本不证明统计显著性。超时明确 `goal_not_met`，保存目标差距和最好版本，不无限探索，也不靠重启延长预算。默认不会为了未达标自动更改请求、质量或数值门槛。

消融复用现有执行器。跨批补丁默认依赖前批优化；不能合法移除的前置项明确记为依赖排除，不拼出无效源码。消融后若选择更好的子组合，仍须重新做目标验证。部分消融或复测缺证据不能报告达标。

**首次配置**：填写 `config/launch.template.json` 和 `config/task.template.json`，再填写并合入 `config/objective.template.json`、`config/objective-protocols.template.json`。后两者是待填模板，不包含可直接运行的 H3 请求。优先将现有 H3 short/official 脚本和 CI 接成逐任务接口；不要另造测量器。`prepare_task.py --objective <目标.json> --target-protocols <测试接口.json>` 负责合入新配置，沿用其余参数；配置不覆盖旧 run。示例 1.2×、48 小时只是演示值，不是已证实可达的 H3 指标。

可直接给本机 Codex 的简短 prompt（替换尖括号）：

```text
使用 $inferrsi-loop，在 v3 仓库从 launch/task/objective 模板创建配置。
项目 <源码/模型/权重>；目标 <SSH机器/容器/推理Python>，卡 <编号>；profiling <复用资料或采集脚本>，记录到 <飞书文档>。
T2VA、FL2VA 的 <目标请求> 均达到原始基线的 1.2×，quality=lossless；每批3候选，最多6批，总48小时，预留12小时。
探索只做真实权重局部性能和数值验证，2+2，Knowledge on；批间测组合，达标后消融并独立复测。核实并冻结配置后独立执行。
```

已有配置：

```text
使用 $inferrsi-loop v3，读取 <launch.json> 和 <task.json>，创建目标驱动新运行。
两任务目标均为原始基线 1.2×，每批3候选、最多6批、总48小时，物理0–1卡；复用 profiling。
其余使用已填写配置；达标后消融及独立目标复测，结果同步配置中的飞书。
```

原有非目标 prompt 继续有效。要关闭目标驱动，在新配置中省略 `objective` 或设 `enabled=false`。

运行路径：`supervise_h3.py → run_h3.py → local_driver.py → SSH ssh_step.advance → controller.run_loop → objective.advance`。后者建立 `batches/batch-NNN` 子运行，复用原流程；本机 driver 自动向正在等待的子运行提交模型结果。组合/消融继续走原生 campaign。查询顶层 `summary.json` 可看 `objective.phase/batch/target_results/best_source/active_run`；原始评测在 `batches/`、`validation/`、`ablations/`。恢复读取冻结代码和状态，不依赖聊天上下文。

开发验证：公开源码包的 87 项 CPU 测试通过，覆盖 blocked 后继续探索和消融、多候选独立复测、两批目标迭代、两种任务检查及断点恢复；skill 格式和未定义名称检查通过。模型回复在编排测试中使用固定测试补丁，性能测量与 final 调用真实原生 InferRSI。尚未执行 H3/P800 目标驱动实机验收，不能据此声称达到示例性能目标。

## blocked 修复

原生 `campaign status` 对合法 blocked 状态返回退出码 2。适配器现在仅接受结构完整的 blocked 状态，记为未完成，不误当 SSH/CLI 崩溃；其他错误仍报错。外层与原生源码摘要编码不同，候选身份改为核对原生保留的源码快照。所有进入组合的候选必须有真实局部 PASS 和独立复测证据；同 campaign 中非 best 的合格候选另用原生测试确认。部分失败不会隐藏其他已验证候选，也不会将失败项纳入消融。已有冻结 run 不原地修改；用新代码创建新运行，旧运行用原冻结版本恢复。

## 原有架构与复用

| 部分 | 实现与复用 | 输入 → 输出 |
|---|---|---|
| Adapter | 复用本机 Codex、SSH 预检、driver 和 supervisor | prompt、启动配置 → 本机 agent 日志、远端状态 |
| Knowledge | 复用原生 `knowledge search` 和 `knowledge_find.rank`；扩展资料收集与经验回写 | 源码、profiling、手动/loop 历史 → 冻结索引、相关上下文、每轮经验 |
| Protocol | 复用测试生成、回补、预检、H3 脚本；补充任务覆盖及冻结的证据检查入口 | 候选、真实权重/固定输入 → 评测规则和测试脚本 |
| Harness | 原生 `campaign start/diagnose/round/status/recover/final` | 冻结基线、候选及测试 → 原生 assessment、selection、证据 |
| Scheduler | 保留设备锁和有限恢复；扩展兼容 campaign 复用、探索到消融衔接 | 预算、候选清单 → 关联 campaign、消融矩阵 |
| Reporter | 复用飞书同步与回执，改为四列 | 原生结果及证据路径 → 同级 run 小节，保留人工原因 |

```mermaid
flowchart TD
    A[本机 Codex：读取 prompt 和模板] --> B[访问预检、准备并冻结配置]
    B --> C{本次任务}
    C -->|探索| D[复用 / 采集 / 按需补采 profiling]
    D --> K[Knowledge：检索历史与本 run 经验]
    K --> E[本机模型：提出候选和源码修改]
    E --> F[准备局部测试、补资料、基线预检]
    F -->|可恢复问题| R[本机模型修复测试脚本]
    R --> F
    F --> G[93 原生 InferRSI：冻结并执行对照]
    G -->|测试异常且预算允许| R
    G --> H[性能、数值、逐用例证据]
    H --> W[回写经验并保留原生结论]
    W -->|继续探索| K
    W -->|探索结束| I[原生 final；汇总全部合格局部候选]
    I --> J{配置是否消融}
    C -->|仅消融：导入已有候选| L[依赖与冲突检查；生成组合及移除矩阵]
    J -->|是| L
    L --> M[T2VA、FL2VA 分别 short E2E；原生 final]
    J -->|否| N[Reporter：四列表格和证据路径]
    M --> N
    W -.保留每轮证据.-> N
```

外层没有重新实现性能打分或全局最优选择。相同基线、协议内容和指标的候选复用 campaign；换模块/测试口径或修正测试脚本建立关联的新 campaign。不同算子的耗时不混排。“合格候选清单”收集所有满足原生门槛且证据完整的候选，不只收集当前 best。局部候选不等于最终 E2E 验收。

`campaign recover` 收集已有执行结果，不重跑。AB/BA 模式不使用仅限 `service_reuse` 的 `--retry-of/--repair-protocol`。外层不改原生 `state.json`。

## 首次使用

在**本机**打开 Codex CLI，进入本仓库并使用 `skills/inferrsi-loop/SKILL.md`（安装到本机技能目录后可写 `$inferrsi-loop`）。App 也能发起同一任务；需要关闭 App 后持续执行时，使用本机终端启动的 CLI 和 supervisor。本机须持续联网且保持运行。93 不安装模型凭据。

新用户从两个模板建立自己的配置，Codex 可代填并核实，无需手工执行 Python 命令：

- `config/launch.template.json`：本机 SSH/Codex/飞书入口，目标容器、远端 Python/包/运行目录，本机运行目录。
- `config/task.template.json`：目标源码、权重、profile 策略、只读资料、设备、测试规则、预算、Knowledge、后续阶段。

| prompt 中需要说明 | 可以省略 | 可以覆盖 |
|---|---|---|
| 首次使用：目标项目/模型、远端机器和容器、允许卡号、资料位置、实验目的、预算及记录目标；无法在本机发现的连接入口 | 已在用户指定配置中填写的值；没有的可选资料来源直接删掉 | 轮数/时间、卡号、Knowledge on/off、profile 策略、收益门槛、2+2 次数、恢复限制、是否消融及消融时间 |

必须有可核实的源码、权重、推理环境、SSH 指南和实际入口、飞书 CLI/文档。模型与推理强度默认继承发起任务的 Codex 会话。认证在本机配置，不把密钥写入 prompt/仓库/93。模板中的 `<...>` 必须填完；示例个人路径不是通用默认。源码默认整个项目可编辑，包括算子；权重、历史资料及冻结验收规则只读。

运行前 `preflight_access.py --config <launch.json>` 检查个人 SSH 身份和飞书读取。沙箱失败与网络/认证失败分别处理；不自动重置认证。读权限不能证明写权限，写入以同步后的回执为准。独立恢复 CLI 也需在相同权限环境中验证。`run_h3.py` 必须显式传 `--launch-config`，不会回退到旧容器。

## 可粘贴到本机 Codex 的 prompt

首次配置（替换尖括号）：

```text
使用 $inferrsi-loop，在当前仓库从配置模板创建我的配置：
项目/模型 <源码绝对路径、模型名称、权重目录>；目标 <SSH 机器、容器、推理 Python>，卡 <物理编号>；
复用 profiling <文件或文档>，历史资料 <目录/文档>，记录到 <飞书文档>。
全仓库探索 3 个候选，总预算 1 小时；真实权重局部性能和数值验证，2+2，收益门槛 1%，Knowledge on。
请发现并核实本机 SSH、Codex 和飞书入口，展示有效配置后独立执行，不重复无新增依据的失败方案。
```

已有配置继续探索：

```text
使用 $inferrsi-loop，启动配置 <我的 launch.json>，任务基础配置 <我的 task.json>。
新建运行，复用已有 profiling，全仓库探索 3 个候选，1 小时，物理 0–1 卡。
Knowledge on；只做真实权重局部单测及数值验证，避免重复失败方案；完成后同步配置中的飞书文档。
```

先 profiling 再探索：

```text
使用 $inferrsi-loop，读取 <launch.json> 和 <task.json>，创建新运行。
先用已有 torch profiler、xprofiler 采集脚本获得基线，再探索 3 个候选；总预算 <时间>，卡 <编号>。
只做局部性能和数值验证，完成后记录飞书。
```

探索后消融 / 仅消融：

```text
使用 $inferrsi-loop，读取 <launch.json> 和 <task.json>，探索 3 个候选后自动做消融。
总预算 <时间>，其中预留 <时间> 给消融；卡 <编号>。
对兼容候选测基线、全部组合、逐项移除；T2VA 和 FL2VA 分别跑已配置 short request，quality=lossless，2+2。
```

```text
使用 $inferrsi-loop，读取 <launch.json> 和 <task.json>，仅对 <候选清单/文档及补丁路径> 做 short E2E 消融。
卡 <编号>，预算 <时间>；检查依赖冲突，分别报告 T2VA、FL2VA，记录到配置文档。
```

Knowledge 对照可以用同一 skill 和两份新配置：on 使用 0–1，off 使用 2–3，其余基线、事实资料、测试规则、模型和预算一致。`prepare_task.py` 提供 `--knowledge on|off --devices 0,1`；模型不需要自行改脚本。两组有共享主机 CPU/内存干扰时不得声称严格硬件等价。这里支持独立 on/off 运行与证据隔离，**不把两次普通运行自动称为原生 `benchmarks compare` 已验收的对照研究**；原生内置任务没有 H3，本文不声称完成该任务移植。

## 测试与恢复规则

- 探索全部只做局部性能及模块数值验证，不转 short/official E2E。有参数模块加载真实权重；无参数模块允许有源码/模型元数据依据的固定输入，标明真实负载代表性。
- `workload_tasks` 在 H3 配置中为 `t2va/fl2va`。测试脚本声明 `cases` 与 `coverage`：不同布局、分支、权重分别覆盖；确实相同可共用并解释；首帧专属模块注明 T2VA 不适用。每个用例保留原始 samples/数值结果，不只报告汇总。
- 2 次预热、2 次测量由测试脚本执行；原生外层 `warmups=0,repetitions=1`。两次样本不证明统计显著性。收益门槛是候选筛选规则，不是探索停止目标。
- 优先原有模块 CI；无适用规则时 `exact_or_review`。无差异可通过，非零差异待确认；不自动放宽阈值。冻结的证据检查脚本在原生评测内验证 provenance、次数、用例完整性；缺证据是 INCONCLUSIVE。
- `local_recovery.max_evidence_rounds=6`，按批计；UTF-8 `.log` 等单文件最多 2 MiB；`max_protocol_versions=4`。修复保留原输入、数值门槛及旧证据，先基线预检，再评测。
- 数值失败或无收益记录结论后继续；候选缺资料/测试异常耗尽预算标“未测量”，不阻止其他候选。SSH/权限/设备等全局问题有限恢复，未恢复保留状态。
- 候选数、评测尝试、有效实验、合格候选分别统计。时间预算含修复；原生 final 有预留，不能用完搜索时间后假称完成最终复测。
- `workflow.after_explore=ablate` 时需提前配置 `ablation_stages` 和 `ablation_max_seconds`（包含在总时间内）。复用 `ablation.py`，按独立补丁组合，冲突/依赖不满足记录排除原因。只有一个合格项做基线对照；没有则跳过。二进制改动需显式构建好消融版本。
- 原生 FAIL 的逐项移除对照可能表示“移除后变慢”，不能据此判该项无效；记录组合内贡献。short E2E 只做既有 functional 校验，完整模型质量/官方请求验收是单独阶段。

## Knowledge 与上下文

默认 `knowledge.enabled=true`。手动优化文档、未写入文档的原始实验、loop 记录、当前源码和历史版本均可列入 `knowledge_sources`。冻结 `catalog.json/coverage.json`，保留版本、出处和未读部分。旧环境仅提供线索，不冒充新环境布局或性能证据。

每轮 `knowledge/content/*.json` 追加候选、尝试原因、环境/基线、范围、结论与原始证据路径；下一轮通过原生 rank 检索相关经验，不拼接完整对话。新运行要在 `knowledge_sources` 指向前序运行根目录；H3 v2 示例已配置。这里迭代的是有证据的经验和后续优化选择，不是训练模型权重，也不自动改 skill。

off 会同时清除外部知识源、历史文件和混合参考包，关闭原生 campaign Knowledge；回补仅允许源码、权重及 `knowledge.factual_evidence_roots`。保留共同 profiling/任务事实及本 run 反馈。Planner、测试生成和恢复都遵守此开关；两组运行目录不得相互作为事实根目录。

## 独立运行、进度与记录

Codex 根据 prompt 准备新任务配置，再调用已有 `supervise_h3.py → run_h3.py → local_driver.py`。监督进程保存 `task.txt`、配置、PID、日志和恢复决定；关闭 App 后可继续。受限权限、认证过期、主机休眠/断网不会自动消失，超过预算明确留待处理。内层模型的活动以 agent JSONL 日志为准。

本机运行目录可看 `supervisor.log`、`supervisor-status.json`、`remote-status.json`；远端看 `summary.json`、`candidate-optimizations.json`、`campaigns/`。实验结束统一收集并同步，实时进度来自日志/状态文件，飞书不是实时控制台。同步失败只补传，不重复跑实验。

每个 run 在飞书新建同级 h2，包含 Asia/Shanghai 时间、run ID、Knowledge on/off。固定四列：

| 优化项 | 尝试原因 | 实测结果 | 结论 |
|---|---|---|---|
| 改动及局部用例/任务 | 一句瓶颈依据与预期作用 | 基线→候选、收益、数值结果 | 候选/无收益/数值失败/未测量及原因 |

证据使用含 run ID 的 `results.json`、补丁和原生评测绝对路径。重复同步只更新本 run；人工补充的原因保存在 `report-notes.json`，不会被后续生成覆盖。完整过程保留 JSON/日志，不另建 Markdown 实验报告。

## 继承当前会话的模型与推理强度

在本机 Codex CLI 选好 model/强度后直接输入 README 中的 prompt。无需在启动与任务配置重复填写。优先级按字段为：命令行显式覆盖 > launch 可选覆盖 > task 可选覆盖 > 当前会话。省略或 null 表示继承；例如只覆盖模型时，强度继续继承。

后台启动前保存 `parent-session.json`；初始化时生成 `model-selection.json`，记录最终 model/effort 及来源，并传到93冻结配置。Planner、测试生成和 supervisor 恢复均使用同一选择。运行中更换外层模型不会改变已启动实验；改变模型请新建运行。旧运行继续用旧版本代码和冻结配置。

读取限于本机 Codex 状态库中当前 thread 的 rollout 路径及模型/强度元数据，不读取认证文件，不修改 provider/base_url。该元数据接口可能随 Codex 版本改变；找不到当前会话或缺少字段会明确报错，不静默回退。在普通终端直接运行脚本、没有当前 Codex 会话时，需显式提供 `--model`、`--effort`，或填写可选配置。无需改配置中的认证信息。

## Public distribution and validation

This directory is an optional extension; it does not replace native InferRSI. Install it in its own environment with `python -m pip install -e .`. Run its CPU orchestration checks with `PYTHONPATH=src python -m unittest discover -s tests -q`. These checks do not establish H3 accelerator performance or output quality. Native InferRSI remains pinned by pyproject.toml.

Personal deployment JSON, historical source copies, model metadata, raw runs and site bootstrap/replay utilities are excluded. `config/examples/synthetic.launch.json` is a test fixture, not a real deployment. Fill the configuration templates for your own environment. The optional legacy H3 collector has labelled site placeholders; provide your existing collector through the generic profile command interface or adapt those explicit legacy match strings to your frozen script bundle before use. No credentials are stored in this package.
