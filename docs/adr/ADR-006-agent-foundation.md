# ADR-006: v0.6.0 Agent Foundation — Tool Contract, Read-only Single-step Agent, Audit and Budget

- **Status:** Accepted
- **Date:** 2026-08-26
- **Deciders:** EKB maintainer（本地单用户工程知识库）
- **Scope:** v0.6.0 Agent Foundation 第一阶段架构冻结
- **Supersedes:** 无（首个独立 ADR；v0.5.x 使用内联 ADR：V53-ADR-01..08）
- **Related:** `docs/v0.6.0-entry-architecture.md`、`docs/v0.5.3-decision-gate.md`

## Background

v0.5.3 Decision Gate 结论为 **CONDITIONAL GO**（HIGH），进入 v0.6.0 前必须冻结：
Tool 协议、read-only allowlist、Agent 安全预算、审计模型（F-02/F-03），并把
Public Deployment 单设为独立 Gate（F-04）。

代码审计确认：Page Retrieval / Knowledge Retrieval / Knowledge Object Read /
Knowledge Memory Read / RAG Answer / AI Ledger 已达服务级 READY；
Provenance Inspection / Source Integrity / Evidence Lookup 需要 Adapter 统一入口；
没有 NOT READY 项。`src/ai/provider.py` 已有 vendor-neutral Protocol、
`AIError` 异常族、`AuditedAIProvider` 审计包装、`AiBudgetGuard` 预算门；
`build_stable_id`、`ContextItem`、`KnowledgeContextPackage`、`AuditedAIOutput`
与 citation 校验可复用。

## Decisions

### 1. v0.6.0 第一阶段是否 read-only

**Decision:** 是。第一阶段 Agent allowlist 只包含 READ_ONLY Tool。

**Reason:** 与 v0.5.3 fail-closed 与 Local-first 边界一致；在审计、预算、回滚
语义未验证前，任何写能力都会引入不可逆副作用风险。

**Consequences:** WRITE_REVERSIBLE / WRITE_DESTRUCTIVE 在 Phase 1 拒绝注册；
写类 Tool 需要后续独立 ADR 才能进入 allowlist。

### 2. 是否 single-step 起步

**Decision:** 是。第一阶段 = single-step：每次 run 最多 1 次 Tool Call。

**Reason:** 单步结构天然无循环、无递归、无自我重试，成本与失败面最小；
multi-step 的 loop detection / 回滚 / 审计语义尚未验证，不应在第一阶段引入。

**Consequences:** 第一阶段不能表达"多工具组合查询"；这类需求返回边界回答并
提示用户拆分为多个单步请求。Multi-step 推迟到 Phase 3 并需要新 ADR。

### 3. 第一阶段最大 Tool Call 数

**Decision:** **1**（`AgentBudget.max_tool_calls = 1`，硬上限）。

**Reason:** 与 single-step 一致，且是 v0.5.3 Decision Gate F-03 要求冻结的
`max tool calls` 的最小可验证值。

**Consequences:** 任何超过 1 次工具调用的执行路径在 Phase 1 不可达；预算门在
Tool 执行前检查并 fail-closed。

### 4. 是否允许 Agent 自主 retry

**Decision:** 不允许。Agent 层 autonomous retry = **0**。

**Reason:** v0.5.x 已确立"只有最底层 transport 可做极少量明确 retry，语义不满
永不重试"。Agent 自主重试会引入烧钱循环与不可审计副作用。

**Consequences:** 三层 retry 边界冻结：
- Transport（QwenProvider）：最多 2 次额外尝试，仅网络错误 / HTTP 429 / 5xx；
- Tool Adapter：0 次重试，失败即映射为 `ToolError`；
- Agent：0 次自主重试，失败即结束 run 并记入审计。

### 5. 是否引入第三方 Agent Framework

**Decision:** 否。不引入 LangChain / LangGraph / CrewAI / AutoGen / MCP framework
或任何编排框架。

**Reason:** EKB 是单用户、本地优先、长期可维护的个人项目；现有
Protocol + frozen dataclass + service 工厂已足够表达 100～300 行级 Tool/Agent
编排。引入框架只会增加依赖债务与隐性行为。

**Consequences:** 第一阶段用纯 Python 实现 Tool Contract 与单步编排；如未来出现
明确不可替代的框架收益，需单独 ADR 重新评估。

### 6. Tool Contract 的边界

**Decision:** 冻结 `ToolDefinition` / `ToolInput` / `ToolResult` / `ToolError` /
`ToolContext` / `ToolRegistry` / `ToolAdapter` 七件套。

- Agent 只依赖 `ToolRegistry` 解析 Tool，不 import 任何具体 service；
- 每个 Tool 对应一个 `ToolAdapter`，负责参数校验、service 调用、结果/错误映射；
- `ToolResult` 结构化（status/data/references/warnings/error/metadata），
  Agent 禁止解析文本/Markdown/字符串；
- `ToolRegistry` 在解析时执行 allowlist 与 side-effect 校验。

**Reason:** v0.5.3 Decision Gate F-02 的直接落地；统一契约是审计、预算、测试
的共同前提。

**Consequences:** Adapter 层成为唯一允许出现 service 类型映射的位置；新增 Tool
必须先在 registry 注册并声明 side-effect class，否则不可调用。

### 7. RAG Answer 是 Tool 还是 Final Answer stage

**Decision:** **Final Answer stage，不是 Tool。**

**Reason:** RAG Answer 是"基于已选上下文的一次模型 completion"，本质是 Agent
自己的回答生成阶段；把它包装成 Tool 会导致"模型调用再包模型调用"，预算归属、
citation lineage、错误语义都会双重计数。Tool 的职责是确定性本地读取，模型调用
职责在 Agent 的 decision 与 answer 两个阶段各一次。

**Consequences:**
- Tool allowlist 不含 `rag_answer`；
- Final Answer Stage 复用 `RagAnswerService.answer` 的 context package 与
  citation 校验语义，但输入上下文来自本 run 的 `ToolResult.references` 投影；
- 每次 run 的模型调用数为：1 decision + 1 final answer（`ANSWER_DIRECTLY` 时
  仅 1 decision）。

### 8. Agent 审计数据最小集合

**Decision:** 冻结 `agent_runs` / `agent_steps` / `tool_calls` 的最小字段集
（见 Entry Architecture §11），并强制最小审计数据原则：

- 保存：run/step/tool_call id、status、tool_name、error_code、duration、
  referenced stable_ids、token usage、decision summary；
- 不保存：完整用户私人正文、完整 prompt、完整 context、完整模型输出、
  chain-of-thought；
- 模型调用继续由 `AuditedAIProvider` 写入 `ai_calls`，用
  `source_feature=agent_decision|agent_answer` 区分。

**Reason:** 延续 v0.5.3 最小审计数据原则；审计目标是"可解释、可追溯、可统计
成本"，而不是复制用户数据。

**Consequences:** 任何持久化审计结构不得新增"全文"列；违反该原则的字段需要在
单独 ADR 中论证。

### 9. schema v13 立即进入 Phase 1 还是延后

**Decision:** **延后（DEFERRED）。** Phase 1 使用 runtime-only execution trace
（内存 frozen dataclass 链 + 结构化日志 + `ai_calls` 关联），不执行 migration。

**Reason:** Phase 1 是单步结构，`ai_calls` 已能完整还原执行链；现在建
`agent_runs / agent_steps / tool_calls` 三表是"因为以后可能需要而堆表"，违反
本阶段防过度设计原则。schema v13 草案已在本 ADR 冻结，待 Phase 3 multi-step
真正落地时在同一 v0.6.x 支版本执行 migration。

**Consequences:** Phase 1 无 schema 变更；Phase 2/3 若需要持久化执行链，必须
先对照本 ADR 的表结构与真实执行语义再写 migration。

### 10. Public Deployment 是否与 Phase 1 解耦

**Decision:** **解耦。** Public Deployment 是独立 Gate，不与 Agent Phase 1 绑定。

**Reason:** 当前本地假设（127.0.0.1、单用户、无 auth、本地 API key、SQLite、
Windows 脚本）在公网下全部需要重新审查；把公网化与 Agent 最小链耦合会同时放大
安全面与实现面，且 v0.5.3 Decision Gate F-04 已要求"先出安全/隔离设计再部署"。

**Consequences:** Phase 1 继续默认 `127.0.0.1`；公网化在独立的 Public
Deployment Gate 中单独决策（auth、隔离、限流、预算、密钥、隐私、SQLite 并发、
文件系统、Windows 假设、滥用防护、成本护栏），本 ADR 不选云厂商、不实现 auth。

## Consequences Summary

- v0.6.0 第一阶段 = 7 个只读 Tool + 单步执行 + 1 工具调用 + 2 模型调用 +
  0 自主重试 + 纯 Python 实现 + runtime trace 审计 + 统一预算；
- 所有写能力、多步、公网、schema v13 持久化都被明确推迟到后续 Phase / 独立 Gate；
- 测试策略 Mock-first，不烧真实 token；
- v0.6.x 的 Decision Gate 从 v0.7.0 入口要求倒推，不预设 `v0.6.3`。

## References

- `docs/v0.6.0-entry-architecture.md`
- `docs/v0.5.3-decision-gate.md`（CONDITIONAL GO，F-01..F-06）
- `docs/v0.5.x-roadmap.md`
- `src/ai/provider.py`、`src/runtime.py`、`src/ai/rag_answer_service.py`
- `src/knowledge_context_packager.py`、`src/source_fingerprint.py`
- `src/models.py`（stable_id / ContextItem / AuditedAIOutput）
