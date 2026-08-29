# ADR-008: Agent Citation Source Contract — Authoritative #N Mapping in the Public /v0.6 Response

- **Status:** PROPOSED（待维护者 / DeepSeek-Pro 评审，未经批准不得实现）
- **Date:** 2026-08-29
- **Deciders:** EKB maintainer（本地单用户工程知识库）
- **Scope:** v0.6.x Demo Integration — Gap A（权威引用映射）的公共契约设计冻结
- **Supersedes:** 无（v0.6.1 冻结清单中的已知缺口 DEMO-03 的正式设计回应）
- **Related:**
  - `docs/demo-integration-contract-design-review.md`（完整评审与决策矩阵）
  - `docs/adr/ADR-006-agent-foundation.md`（Agent 契约，不重开）
  - `docs/adr/ADR-007-public-deployment-foundation.md`（Hosted 边界）
  - `docs/v0.6.1-competition-demo-freeze.md`（Known Limitations #1）

## Background

v0.6.1 比赛演示暴露了真实 Agent API 与 UI 之间的两个集成缺口之一：公共
`AgentRunResponse.citations` 只携带已校验的 stable_id 列表，不含回答正文
`#N` 标记与来源的权威映射。演示 Mode 2 因 demo-only 的 `citations_detail`
可以渲染可点击 `#N`；Mode 1 正确拒绝猜测（测试保护）。

代码事实（全部已核实）：

- `#N` 编号已在 `KnowledgeContextPackager.build` 于模型调用前唯一铸造
  （`stable_id, "#N"`，1-based，保留项顺序）；
- 引用校验 fail-closed：未映射的 `#N` 或伪造 stable_id 使整个回答被拒绝
  （`rag_answer_service._validate_answer_citations`）；
- 公共 DTO 一律 `extra="forbid"`，且 Mode-1 客户端用 pydantic 严格解析 ——
  服务器新增字段会击落冻结客户端。

## Decisions

### 1. 引用映射采用响应级结构化对象（OPTION A）

**Decision:** 在 `AgentRunResponse` 上新增**可选、带默认值**字段
`citations_detail: tuple[CitationDetail, ...] = ()`，其中
`CitationDetail = {display_index: int>=1, stable_id, source_type, title, label}`。
`citations: tuple[StrictStr, ...]` 原样保留，语义与排序不变。

**Reason:** 映射与引用校验器同源（同一个 `citation_by_number` 投影），权威性
无第二来源；对模型零新增依赖（维持 `#N` 语法）；对前端是查表渲染，现有
`demo_ui.build_citation_chips` 的双分支即消费形态；后端复杂度最小。

**Consequences:** 正文中的裸 stable-id 引用进入 `citations[]` 但不进入
`citations_detail`（无显示编号，宁缺毋假）；拒绝正文注解偏移（OPTION B，
偏移脆弱 + 过度设计）与语义 cite-token（OPTION C，需改模型语法与校验重写，
仅当未来出现跨响应持久引用需求才重评）。

### 2. display_index 的语义契约

**Decision:** `display_index` **恒等于**回答正文中的 `#N` 标记数字；映射只含
**通过校验**的标记；允许缺口（包铸造 #1..#K，回答只引用子集）；按
`display_index` 升序；作用域为**单次响应**（以 `request_id` 为界）。

**Reason:** 与正文标记一一对应是 UI 可信渲染的前提；缺口诚实反映"哪些标记
出现在正文中"，重排闭缺会破坏对应；per-response 作用域下 `#N` 本身就是
正确的语义键。

**Consequences:** 一源多处 → 单条目；一句多源 → 多条目（按键组织，无句级
模型）；校验拒绝任何未知标记 → 整个回答 FAILED（现状不变），因此已完成的
回答中不存在"被剔除但残留"的映射。

### 3. 铸造层 = 引用校验完成后的 Agent 响应层

**Decision:** 结构化映射在**校验完成后**由 Agent 响应层一次性生成（扩展校验器
返回值以携带 `(N, stable_id)` 对），Hosted 序列化仅投影；禁止在 Hosted 层从
answer 文本二次解析，禁止把 packager 的调用前超集直接暴露。

**Reason:** "`#N` 是否合法"的语义所有权在校验器；映射是校验结论的直接投影，
二者同源才无双重权威。Hosted 层拿不到上下文包，在那里解析正文 = 反模式。

### 4. 向后兼容与版本偏斜政策

**Decision:** 本字段为 additive（默认空 tuple），`/v0.6` 路径**不 bump**。
由于公共 DTO `extra="forbid"` + 客户端 pydantic 严格解析：实现包必须**同包**
更新客户端容忍（声明新字段），且冻结的 v0.6.1 Mode-1 客户端在整合包落地前
**不得指向**已增强的服务器（两端版本锁定）。外部 JSON 消费者天然兼容。
`/v0.7` 仅用于破坏性变更（删字段/改语义/改 nullability/新必填）。

**Reason:** JSON 层面 additive 安全；pydantic 严格层面是确定的破坏行为，
必须用发布纪律而非侥幸来处理。

**Consequences:** 枚举扩展（如未来 CitationDetail 增字段）按 additive 规则
允许；消费者必须对缺失字段回退到"无猜测基线渲染"（现有 Mode-1 分支）。

### 5. 命名与演示超集连续性

**Decision:** 真实字段沿用演示已冻结的字段名 **`citations_detail`**；
`DemoCitation` 在整合包中改为继承真实 `CitationDetail`、仅追加
`anchor_label`。demo-only 字段收窄为 `mode="mock_demo"`、`anchor_label`、
`demo_note`。

**Reason:** 同名同形让 UI 单一渲染路径覆盖两个模式（消除"if mock 换渲染器"），
并把"两个名字描述同一概念"的漂移风险（总审计 R-20 同族）归零。

**Consequences:** 冻结演示语义零变化（fixture 值、场景、文案不动）；演示
契约的形状由"独立定义"变为"真实超集"，superset 测试方向不变。

## Consequences Summary

- `POST /v0.6/agent/run` 成功响应新增可选 `citations_detail`（默认空）；
  `citations` 不变；路径不 bump；schema 不变（运行时投影，无 v13）。
- 排序契约：`citations[]` = 校验器首次出现序；`citations_detail` =
  display_index 升序、允许缺口；两者均需测试钉住。
- 安全不变量：前端零猜测；权威性唯一来自校验器映射；未映射标记整体拒绝；
  无内部 ID/路径泄漏。
- 归属 **v0.6.2**（对已发布契约的兼容性补全），实现需维护者赛后单独授权
  整合包；本 ADR 仅设计，状态 PROPOSED。

## References

- `docs/demo-integration-contract-design-review.md`（§1、§1.4 决策矩阵、§10 测试清单、§11 发布计划）
- `src/knowledge_context_packager.py`、`src/ai/rag_answer_service.py`、
  `src/hosted_api/contracts.py`、`src/agent_client.py`、`src/demo/contracts.py`、`src/demo_ui.py`
- `docs/v0.6.1-competition-demo-freeze.md`（Known Limitations #1、需后端评审范围）
