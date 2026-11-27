---
name: inferrsi-loop
description: 使用本机 Codex 和原生 InferRSI 准备、执行、恢复推理优化实验，支持 profiling、全仓库探索、消融和飞书记录；保留证据及版本。
---

# InferRSI loop

单一通用 skill，不按场景复制流程。模型仅在本机调用，93 执行实验；保留 v1/v2，v3 增加可选目标驱动。先定位用户指定仓库、读 README 和适用 AGENTS.md；已有运行必须使用其冻结版本，不在 v1/v2/v3 间迁移状态。

## 配置与入口

首次从 `config/launch.template.json`、`config/task.template.json` 创建自己的配置，由 Codex 按需求填充核实；不要要求用户已有个人 H3 配置。可从用户指定指南、工作区说明和 PATH 发现已有 SSH/Codex/飞书入口，不扫描凭据。所有占位符必须填完，无可选资料则删掉该来源。

配置优先级：本次明确要求 > 用户指定配置 > 经核实并展示的模板默认。覆盖建立新运行，不改冻结配置。启动前简要展示项目/基线、profile 策略、卡号、层级、次数、门槛、预算、Knowledge、记录目标及配置来源；已确定内容不重复询问。

SSH 按用户指南使用个人账号及已配置认证入口，严格校验主机密钥。`launch.json` 明确 ssh/host/expected_user/expected_hostname/ssh_guide，以及 lark/lark_skill/document、容器、远端 Python/目录；`run_h3.py` 必须显式传 launch 配置。认证只在本机，不写入 prompt/仓库/93。

复用 `preflight_access.py`。区分沙箱权限、DNS、认证和目标身份错误；普通终端成功不等于恢复 CLI 有权限。按环境工具申请必要权限，不禁用沙箱、不重置认证。只读预检不证明飞书写权限，写后以 fetch 和同步回执确认。预检失败保留原因，不让模型无限重复权限重试。

## 流程与复用

根据 prompt 组合现有配置，复用 prepare_task、local_driver、run_h3、supervise_h3 和 H3 适配。

- `task=explore`：模型探索；本地模块性能与数值验证；原生 campaign 管理冻结、AB/BA、判定、选优、状态和 final。
- `profile.mode=reuse/collect/adaptive`：按 prompt 复用、先采集或模型说明缺口后有界补采。P800 复用 torch profiler 与 xprofiler；收益计时关闭 profiler。
- `workflow.after_explore=ablate`：预留消融预算，合格局部候选交给现有 ablation.py；检查依赖冲突后生成基线/全部/逐项移除。也可直接 `task=ablate` 导入已有优化。
- 查询或补记录：只读现有 run，不重启实验。

兼容基线/测试/指标复用 campaign；不同模块不混选全局 best。修正测试脚本建立关联新评测，保留原证据和剩余预算。原生 recover 只收集已有结果；不将 service_reuse 的 retry/repair 参数套到 AB/BA。所有原生结论保持原样，证据不完整不得登记为合格候选。

## Knowledge 与 RSI

默认 on。声明手动文档、loop 文档、未写文档的原始实验、当前及旧源码参考；检查 catalog/coverage 中缺失、超限、未读资源。使用原生检索及完整相关正文。旧环境经验可以提供思路，不能冒充当前布局、权重或收益。重复失败方向须说明新的证据/实现/环境；缺资料或执行崩溃不等于策略被证伪。

每轮经验回写 run/knowledge，随后检索复用；用任务摘要、最近反馈、相关经验和按需资料，避免拼接整段对话。新 run 的 knowledge_sources 要包含前序经验根目录。这里迭代经验和后续决策，不自动训练模型或改 skill。

off 同时关闭原生知识及外层历史注入：移除历史源和混合参考包；回补只允许源码/模型事实和明确 factual_evidence_roots。保留同一任务事实及本 run 自己的反馈；Planner、测试生成和恢复均不得转向其他运行或历史文档。on/off 的设备与预算由 prompt 配置，不把两次普通运行夸大为已完成严格对照。

## 局部测试与恢复

全仓库可编辑包括算子；权重、历史和冻结规则只读。探索全部只跑局部测试，不因缺测试转 E2E。有参数模块用真实权重，禁止随机造权重；无参数模块记录固定输入的源码/元数据依据。

同一测试实现配参数化 cases。注明来源、源码约束与所选测试值、shape/dtype/layout/stride、分支条件、生成规则、计时范围和聚合方式。T2VA/FL2VA 分支、布局或权重有差异分别覆盖；相同用例共用须解释，首帧专属模块注明 T2VA 不适用。每个用例保留数值与原始 samples，不挑最快用例。缺实际 request 精确长度不阻止有依据的固定输入局部测试，标明真实负载代表性待确认。新请求复用脚本，新增尺寸补用例；并行、dtype、batch、backend 改变则重新检查适用性。

必须调用实际候选生产代码；原生算子修改要证明重建及加载二进制身份。原有模块数值 CI 优先，无适用规则用 exact_or_review；不放宽门槛。2+2 由脚本执行，外层不重复叠加；少量样本不宣称统计显著性。局部合格只登记候选，不外推 E2E。

回补默认6批，文本含.log单文件最多2 MiB，测试版本最多4；按配置覆盖。读取错误区分类型、大小、路径和缺失，同条件失败不原样重试。补资料后继续同一候选，修复保持输入字节、规则、用例不变；基线预检后再冻结评测。候选次数与有效实验分别报告。数值失败/无收益继续下一候选；资料和脚本预算耗尽记录未测量，不阻塞其他候选。全局故障有限恢复。

消融分别执行 T2VA、FL2VA short E2E，冻结请求与 lossless。只有一个合格项则做基线对照，没有则跳过；依赖导致的无效移除不执行。移除后变慢表示组合内正贡献，不能把原生该对照的 FAIL 写成优化无效。短请求不等于完整请求质量验收。

## 独立执行与记录

保存 task.txt、有效配置/来源、预算、历史摘要和证据路径，恢复不依赖当前聊天。后台复用 supervisor，核实真实 PID/状态；查看 supervisor.log、remote-status.json、原生 campaign。同步失败仅重试发布，不重复实验。运行期间不修改冻结代码；用户要求独立验收时外层只读观察，等待结束后再修开发实现。

飞书操作读取本机 lark-doc skill。每个 run 新建并列 h2，标题含 Asia/Shanghai 时间、run ID 和 Knowledge 条件。固定四列：**优化项 | 尝试原因 | 实测结果 | 结论**。原因复用 Planner rationale，一句说明瓶颈依据和预期作用；实测写用例/任务、基线→候选、收益、数值结果；未测量写具体原因。保留负收益和未完成项。证据用含 run ID 的 results.json、补丁、评测绝对路径。

复用 reporter，不另写记录 skill。重复同步只更新本 run，手工补充的原因保存在 report-notes.json，后续收集/同步不得覆盖。实验记录放已授权飞书，不另建 Markdown 实验报告。无同步回执不声称已记录。

## 模型与强度继承

默认不填写 launch.model/effort 或 task.model.name/effort，也不把模板默认值当成本次选择。由已有启动脚本按 CODEX_THREAD_ID 读取本机当前会话最新 turn_context，启动后台前保存 parent-session.json，初始化时保存 model-selection.json 并冻结到93。命令行明确覆盖优先，其次 launch、task 可选覆盖，再继承会话；每个字段独立解析。Planner、测试生成和恢复复用同一记录，不随着外层后来切换模型而改变。

不要仅删除 --model 让子进程猜全局默认，也不要读取其他 thread 的模型。元数据不可读时明确提示；普通终端没有会话时使用显式覆盖。API 路由、provider、认证仍由本机 Codex/CC Switch 管理，不复制到93，不为了继承修改认证。现有冻结运行不变，修改选择创建新运行。

## 可选目标驱动（v3）

仅用户要求目标驱动时使用 v3 及 objective 模板；v1/v2 和非目标调用保留。先填写 launch/task，再合入 objective 与逐任务测试接口模板，所有占位符必须核实；不要把部署示例当通用配置。目标每任务选加速比或绝对阈值，固定原始基线、请求及质量规则；H3 必须同时配置 T2VA/FL2VA。目标请求若是 short，不得写成 official 已达标。

复用 objective.advance 的批次调度；不要在聊天中另写循环。探索仅局部验证；每批组合做 short E2E；目标检查用配置的 validation_stage。未达标且有预算继续探索；达标后做累计消融与独立目标复测；人工质量审查未完成时是 pending_review。每轮失败、目标差距和新瓶颈供下一批复用。Knowledge off 仍保留本 run 反馈。

默认不逐候选消融，ablation_every_batches=0 表示达标后消融；可配置间隔。跨批依赖不明时保守保留，记明无法独立移除的项。达到最大候选/批次/时间时保存最好版本及未达标原因，不增加预算、不放宽验收、不伪造目标达成。缺测量/未完成消融不能报告 goal_met。Reporter 保持四列表格，额外简列每任务目标与实测；证据指向各批次和最终复测目录。

原生 blocked 是有效失败状态，保留其证据并继续其他合格候选；不要直接修改原生 state.json。局部及最终复测要核对保留源码快照，不能把不同编码的摘要直接比较。运行时仍只能使用其冻结版本。
