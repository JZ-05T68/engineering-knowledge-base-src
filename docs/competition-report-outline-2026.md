# Competition Report Outline 2026（比赛报告正式大纲 · 赵涵工作底稿）

制定日期：2026-08-29（CURRENT AS OF 2026-08-29；volatile 事实标注见 §28 规则）。
报告撰写人：赵涵（目标 2026-09-25 前完稿）。技术事实来源与最终数字冻结见
`docs/competition-report-fact-freeze-template.md`（2026-09-15 填写）。

配套文档（本文档不重复其内容，只引用）：
事实底稿 `docs/competition-report-handoff-v0.6.1.md`（A–M 素材+写作红线）、
最终审计 `docs/v0.6.1-competition-demo-final-audit.md`（声明矩阵+证据清单+事实表）、
演示冻结 `docs/v0.6.1-competition-demo-freeze.md`、操作手册
`docs/v0.6.1-competition-demo-runbook.md`、人工测试
`docs/competition-manual-test-plan.md`、答辩材料
`docs/competition-defense-material-outline.md`。

## 0. 全局写作规则

1. **红线**（违者返工，完整清单见 handoff-v0.6.1 写作红线节）：不写"公网已上线/
   WP6A 已通过/多用户 SaaS/实时联网校验/changed=篡改/预置演示=实时 AI/回答必然
   正确/大量生产客户/自动多步 Agent/自动更新知识"；不写"100% 准确/完全可信/零幻觉"。
2. **联网措辞**：用"EKB 的核心知识管理能力和比赛预置演示可在本地离线运行；
   真实 Agent 模式在启用云端模型时需要网络和 API 配置"。禁用"全程无需联网"。
3. **模式区分**：Mode 2 = 确定性预置演示（非实时 AI）；Mode 1 = 本机 Agent 接入；
   Public Deployment = DEFERRED。
4. **时效标注**：所有易变事实标注 `CURRENT AS OF 2026-08-29` 或
   `FINAL — TO BE FILLED 2026-09-15`；测试数字当前值 2685 passed / 4 skipped
   仅作 CURRENT 证据，终稿以 9/15 重跑为准。
5. **引用规范**：关键事实句尾括注证据编号（E-xx，见 §22 证据索引）。

## 1. 项目摘要（Executive Summary）

- **章节目标**：3 分钟让评委懂产品、信证据、记住差异。
- **核心问题**：这是什么？解决什么？凭什么是它？
- **READY NOW**：定位段（handoff-v0.6.1 §A）：工程项目真正容易丢失的不只是文件，
  还有工程师解决问题的经验与判断依据。EKB 把工程资料、工程经验组织起来，
  用 AI 回答工程问题，每条回答标注依据、可点回来源，来源变化会如实提示——
  **让工程经验可查、可复用、可回源、可核验**（E-01）。
- **待 9/15**：三人人工测试结论一句（E-12）。
- **图表**：产品一图流（§8 工作流图）。**截图**：FIG-DEMO-01。
- **支撑**：final-audit 定位节；README。
- **允许**：本地优先、单用户、演示冻结、2685 项自动化测试（CURRENT）。
- **禁止**：开头写"基于 RAG/Agent"（从工程知识丢失讲起）；任何红线句。

## 2. 项目背景与问题定义

- **目标**：定性说清问题真实存在；**核心问题**：痛点的因果链。
- **READY NOW**：六点问题框架（handoff-v0.6.1 §B）：资料分散 / 经验在个人脑中 /
  新人重复踩坑 / 传统搜索只找到文件 / 通用 AI 答案难核验 / 资料更新后旧结论失效。
- **待 9/15**：无。
- **图表**：痛点因果链示意。**截图**：无。
- **支撑**：handoff-v0.6.1 §B。**允许**：定性论述。**禁止**：编造市场统计数字。

## 3. 用户痛点与应用场景

- **目标**：具象化到一个工程师的一天。**核心问题**：谁在什么场景受益。
- **READY NOW**：三个场景——设备调试查手册、排查历史故障、新人接手老系统；
  场景 B 的真实故事线（编码器接线错误→PID 震荡→定位与处理，演示语料预置）。
- **待 9/15**：无。**图表**：场景卡片×3。**截图**：FIG-DEMO-04。
- **支撑**：runbook 场景 B 节。**允许**："以预置演示语料呈现"。
- **禁止**：把演示语料说成真实客户数据。

## 4. 产品定位与目标

- **目标**：产品边界清晰。**核心问题**：做什么、不做什么。
- **READY NOW**：本地单用户个人工程知识资产系统（非云文档/非多用户 SaaS/
  非信息采集平台）；长期目标 Document→Understanding→Retrieval→Reuse→Capability。
- **待 9/15**：无。**图表**：定位象限（可选）。
- **支撑**：AGENTS.md 产品哲学、final-audit 声明矩阵 #10/#11。
- **允许**：产品边界即设计选择。**禁止**：多用户/云同步暗示。

## 5. EKB 整体解决方案

- **目标**：一句话方案 + 工作流图。**核心问题**：怎么把痛点变成流程。
- **READY NOW**：工作流：资料进入→整理→检索→Agent 提问→有依据回答→Citation→
  Source Viewer→Integrity/Trust Boundary（handoff-v0.6.1 §C）。
- **待 9/15**：无。**图表**：本文档 §8 工作流图（报告/PPT/答辩三用）。
- **截图**：FIG-DEMO-02/03。**支撑**：runbook 主脚本。
- **允许**：全流程均已实现。**禁止**：流程图中出现"云校验/自动更新"环节。

## 6. 系统总体架构

- **目标**：两级架构分开讲。**核心问题**：评委版 vs 技术版不混层。
- **READY NOW**：评委版：工程资料→知识组织→检索→只读 Agent→回答→来源→
  完整性边界（handoff-v0.6.1 §D）；技术附录：Local Streamlit + 知识/检索服务 +
  单步 Agent 执行器 + 七只读工具 + Final Answer Stage + 引用校验 + Hosted HTTP
  seam + SQLite v12（final-audit Architecture Story 附录）。
- **待 9/15**：无。**图表**：架构图两张（judge 版简、appendix 版详）。
- **支撑**：`src/agent/tools/bootstrap.py`（工具清单）、`src/agent/execution/executor.py`（单步语义）。
- **允许**：7 只读工具/单步/120k 上限/schema v12（均有代码出处）。
- **禁止**：把 Hosted API 描述为公网生产部署。

## 7. 核心功能设计

- **目标**：按功能域展示完成度。**核心问题**：每个功能解决什么。
- **READY NOW**：导入（PDF 逐页 PNG+文本层+状态分类）、浏览/笔记、检索（多字段
  +FTS）、证据篮（选区级证据包）、知识对象/记忆、AI 台账（可审计）、备份恢复。
- **待 9/15**：各功能人工测试结论（E-12）。**图表**：功能地图。
- **支撑**：README 功能节、CHANGELOG。**测试**：`tests/test_database.py` 等
  全量套件（CURRENT 2685）。
- **允许**：逐功能描述。**禁止**："全行业领先"类空话。

## 8. 工程知识组织与检索

- **目标**：知识模型是差异化地基。**核心问题**：资料怎么变成可检索知识。
- **READY NOW**：页面/笔记/对象/记忆/证据五类知识载体；stable_id 体系；
  FTS5+混合检索（v0.5.x 评测基线）。
- **待 9/15**：无。**图表**：知识模型 ER 简图。
- **支撑**：`src/models.py`、v0.5.x 评测文档。**允许**：检索工程实证。
- **禁止**：未跑过的准确率数字。

## 9. Agent Foundation

- **目标**：讲清"受控"二字。**核心问题**：Agent 为什么可信、可控。
- **READY NOW**：单步执行器（一次决策+至多一个只读工具、不重试不循环）、
  七只读工具清单、决策/终答两阶段、120k 请求上限、封闭错误目录
  （final-audit Architecture 附录、handoff-v0.6.1 §E）。
- **待 9/15**：无。**图表**：Agent 时序简图。
- **支撑**：`src/agent/execution/executor.py`、v0.6.0 release notes。
- **测试**：`test_agent_execution` 等。
- **允许**："有意的单步设计"（安全边界）。**禁止**："自动多步/自主推理"。

## 10. 引用、证据与来源追溯

- **目标**：核心差异化。**核心问题**：从结论回到原文的链路。
- **READY NOW**：强制引用+逐条校验（失败整体降级）、UI 引用 chips、来源详情
  viewer（标题/类型/位置）、证据篮选区级追溯（handoff-v0.6.1 §F）。
- **待 9/15**：无。**图表**：引用链路图。**截图**：FIG-DEMO-02/03。
- **支撑**：`src/agent/response/final_answer.py`、`pages/0_知识Agent.py`。
- **测试**：`test_demo_contract` 引用校验、`test_demo_ui` 引用双模式。
- **允许**：#N 仅演示契约映射，Mode 1 为独立来源列表（如实写）。
- **禁止**："引用永不失败/来源自动正确"。

## 11. 来源完整性与信任边界

- **目标**：第二差异点。**核心问题**：变化如何被诚实呈现。
- **READY NOW**：指纹快照比较语义；changed=快照不一致≠篡改；fail-closed 空态
  「不会编造答案」（handoff-v0.6.1 §G）。
- **待 9/15**：无。**图表**：信任边界示意。**截图**：FIG-DEMO-05/06。
- **支撑**：`src/source_fingerprint.py`、场景 C 冻结值。
- **允许**：per-source 完整性为**演示契约能力**（DEMO-ONLY），本地指纹机制已实现。
- **禁止**："实时完整性监控/被篡改检测/所有来源自动可信"。

## 12. 比赛演示闭环

- **目标**：三场景讲透。**核心问题**：每场演示了什么、为什么重要。
- **READY NOW**：按冻结清单逐场写：Judge problem / 用户动作 / UI 输出 /
  证明了什么 / 截图 / 声明分类（freeze manifest Primary Scenarios 节 +
  handoff-v0.6.1 §I）。A=grounded 快速成功；B=历史经验复用；C=完整性
  （DEMO-ONLY per-source 状态）。
- **待 9/15**：终版截图与排练结论。**截图**：FIG-DEMO-02…06。
- **允许**：场景 C 标注演示预置。**禁止**：升级 C 为生产能力。

## 13. 系统安全、隐私与本地优先设计

- **目标**：隐私是设计而非口号。**核心问题**：数据在哪、谁能看。
- **READY NOW**：127.0.0.1 绑定、数据/代码分离、生产库不进 Git、演示与生产
  物理隔离、密钥仅环境变量、AI 调用台账可审计（handoff-v0.6.1 §H）。
- **待 9/15**：无。**图表**：数据边界图。
- **支撑**：`src/hosted_api/security.py`、AGENTS.md 安全节。
- **允许**：本地优先为硬约束。**禁止**："绝对安全/军事级加密"。

## 14. 自动化测试与质量保障

- **目标**：用数字与机制说话。**核心问题**：质量怎么保证。
- **READY NOW（CURRENT AS OF 2026-08-29）**：全量 2685 passed / 4 skipped /
  0 warnings / exit 0（2026-08-29 实测）；契约测试体系（DTO 兼容/确定性/离线
  哨兵）；Ruff。
- **待 9/15**：**FINAL — TO BE FILLED 2026-09-15**：冻结重跑全量数字。
- **图表**：测试金字塔。**支撑**：final-audit Test Evidence。
- **允许**：标注 CURRENT。**禁止**：把 2685 当终稿最终数字静默使用。

## 15. 三人真实人工测试

- **目标**：展示真实人工验证体系。**核心问题**：谁测的、怎么测、结论如何。
- **READY NOW**：测试体系设计（分组 A–P、三人分工、用例模板、ID 约定、
  严重级定义）→ `docs/competition-manual-test-plan.md`。
- **待 9/15**：**FINAL**：用例数/通过数/缺陷分类/回归结论（TBD，摘要模板见
  manual-test-plan §Summary，勿填假数字）。
- **图表**：缺陷分布（9/15 后）。**截图**：缺陷记录样例（可选）。
- **允许**：体系+TBD。**禁止**：现在填任何通过率。

## 16. 版本演进

- **目标**：展示工程节奏。**核心问题**：每版解决什么、边界在哪。
- **READY NOW**：v0.5.3 可审计 AI 基础 → v0.6.0 Agent Foundation（**发布但
  公网部署 DEFERRED：WP6A PARTIAL/PAUSED、WP6B NOT STARTED**）→ v0.6.1
  Competition Demo Experience（FROZEN，KEEP_FROZEN）（final-audit Version
  Evolution 节）。
- **待 9/15**：无。**图表**：时间线。**支撑**：CHANGELOG、release notes。
- **允许**：按历史文档表述。**禁止**：改写部署状态。

## 17. 创新点

- **目标**：站得住的创新主张。**核心问题**：新在哪、凭什么是我们做的。
- **READY NOW**（均须可落证据）：① 工程资料+工程经验的统一知识组织；
  ② 回答→引用→来源检查的追溯闭环；③ 无依据 fail-closed（不编造）；
  ④ 来源完整性/信任边界意识；⑤ 本地优先隐私设计；⑥ 单步只读 Agent 的
  安全边界（handoff-v0.6.1 定位+final-audit 矩阵）。
- **待 9/15**：无。**图表**：创新点-证据对照表。
- **允许**："工程组合创新/面向工程场景的设计"。**禁止**："世界首创/学术首创"。

## 18. 应用价值

- **目标**：真实场景价值，不夸大。**核心问题**：谁用、省什么、留什么。
- **READY NOW**：个人工程师知识资产沉淀；新人复用经验减少重复踩坑；
  资料密集型小团队的可行起点（本地单用户形态）。
- **待 9/15**：无。**图表**：价值-角色对照。
- **允许**：定性价值。**禁止**：市场规模、客户数、效率百分比。

## 19. 当前限制

- **目标**：诚实清单（加分项）。**核心问题**：知道自己的边界。
- **READY NOW**：final-audit Known Limitations 五条（演示语料未正式批准/
  per-source 完整性仅演示契约/Mode 1 需手动启动/部署推迟/单步形态）。
- **待 9/15**：补充人工测试发现的限制。**允许**：全部如实。
- **禁止**：把限制写成卖点或隐瞒。

## 20. 后续规划

- **目标**：路线图对齐既有规划。**核心问题**：下一步去哪。
- **READY NOW**：v0.7.0 Personal Experience Agent → v0.8.0 Reliability →
  v0.9.0 Hardening → v1.0.0 Personal Experience System；部署与语料批准按
  独立门禁（handoff-v0.6.1 §M）。
- **待 9/15**：无。**图表**：路线图时间线。**禁止**：承诺具体上线日期。

## 21. 总结

- **目标**：回扣主题。**核心问题**：评委带走哪一句话。
- **READY NOW**：回扣"让工程经验可查、可复用、可回源、可核验"+信任边界记忆点
  （场景 C）。
- **待 9/15**：测试结论收尾句。**允许**：克制。**禁止**：夸大。

## 22. 附录 / 证据索引（Evidence Index）

| E-ID | 报告主张 | 来源文档 | 代码 | 测试 | 截图 | commit |
| --- | --- | --- | --- | --- | --- | --- |
| E-01 | 定位与五关键词 | handoff-v0.6.1 §A / final-audit Positioning | pages/0_知识Agent.py | test_agent_demo_page 初始渲染 | FIG-DEMO-01 | 461157b |
| E-02 | 单步 Agent+七只读工具 | final-audit 附录 | src/agent/execution/executor.py、tools/bootstrap.py | test_agent_execution 等 | — | v0.6.0（bb1a4207） |
| E-03 | 引用校验 fail-closed | handoff §F | src/agent/response/final_answer.py | test_demo_contract | FIG-DEMO-02 | bb90d23 |
| E-04 | 来源检查 viewer | handoff §F | pages/0_知识Agent.py、demo_ui | test_demo_ui viewer 语义 | FIG-DEMO-03 | 461157b |
| E-05 | 历史经验复用 | runbook 场景 B | src/demo/fixtures.py | test_demo_contract preset | FIG-DEMO-04 | 8a15253 |
| E-06 | 完整性边界（DEMO-ONLY） | freeze manifest 场景 C | src/demo/contracts.py | test_demo_ui 免责断言 | FIG-DEMO-05 | 8a15253 |
| E-07 | 无证据空态 | runbook 状态表 | pages/0_知识Agent.py | test_demo_ui 空态 | FIG-DEMO-06 | bb90d23 |
| E-08 | 离线确定性演示 | freeze manifest | src/demo/mock_agent.py | test_demo_contract 离线哨兵 | FIG-DEMO-01 | 8a15253 |
| E-09 | Mode 1 明示失败 | runbook If Mode 1 fails | src/agent_client.py | test_agent_demo_page 拒连 | 07（可选） | bb90d23 |
| E-10 | 演示重置 | freeze manifest Reset | pages/0_知识Agent.py | test_agent_demo_page reset | — | f113cc4 |
| E-11 | 自动化测试 2685/4（CURRENT） | final-audit Test Evidence | — | 全量套件 | — | 8442d42 树实测 |
| E-12 | 三人人工测试 | manual-test-plan | — | 人工 | 记录 | FINAL 9/15 |
| E-13 | 部署 DEFERRED | entry doc / final-audit 矩阵#10 | — | — | — | b6035bd |

## 23. 推荐截图索引（正式 ID）

| ID | 画面 | 分辨率 | 必须可见 | 禁止可见 | 报告图注 | PPT 图注 | 证据主张 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| FIG-DEMO-01 | Agent 初始屏 | 1920×1080 | 品牌/●预置离线演示/3 场景卡/右栏信任提示 | 技术 ID、fixture 名 | 知识 Agent 工作台一屏闭环 | 产品总览 | E-01/E-08 | 待终版截取 |
| FIG-DEMO-02 | A 场景回答 | 1920×1080 | ✓徽章/引用 2 条/正文 chips/预置标识 | JSON、异常 | 回答标注依据与引用 | 有依据回答 | E-03 | 待终版截取 |
| FIG-DEMO-03 | A 来源详情 | 1920×1080 | 来源详情·已验证/标题/页码/为什么与回答有关 | 路径、内部 ID | 结论可回到来源 | 可追溯 | E-04 | 待终版截取 |
| FIG-DEMO-04 | B 场景 | 1920×1080 | 经验回答+⚠限制横幅+知识记忆来源 | — | 历史经验可复用 | 经验复用 | E-05 | 待终版截取 |
| FIG-DEMO-05 | C 完整性 | 1920×1080 | 来源状态·来源发生变化+免责+演示预置说明 | "篡改/不可信"字样 | 来源变化如实提示 | 信任边界 | E-06 | 待终版截取 |
| FIG-DEMO-06 | 诚实空态 | 1920×1080 | ○未找到资料+不会编造答案 | 报错样式 | 无依据不编造 | 诚实边界 | E-07 | 待终版截取 |

截图在终版 UI（队友 polish 后）于 9/10–9/15 采集；复现步骤见 runbook 截图清单节。
另可选：FIG-DEMO-07 安全失败态、FIG-HOME-01 首页入口、FIG-1366-01 笔记本分辨率。

## 24. 报告写作时间表

| 阶段 | 时间 | 内容 |
| --- | --- | --- |
| 大纲与证据维护 | 08-29 ~ 09-09 | 本大纲+handoff 保持与仓库一致；队友 polish |
| 人工测试窗 | 09-10 ~ 09-15 | 三人按 manual-test-plan 执行；缺陷分类/修复/回归；终版截图；演示排练 |
| **报告交接冻结** | **09-15** | 填写 fact-freeze-template；数字/截图/commit 全部定稿 |
| 正式撰写 | 09-15 ~ 09-25 | 赵涵按本大纲成稿 |
| 技术事实复核 | ~09-20 | 维护者核对事实表与红线（不重写文字） |
| 完稿 | ≤09-25 | 留缓冲，不压最后一天 |
