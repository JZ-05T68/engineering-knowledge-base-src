# Pre-v0.7 Remediation Specification — TD-04 / TD-05 / TD-06 Codex Handoff

- **Date:** 2026-08-29
- **Status:** IMPLEMENTATION-READY SPECIFICATION；本文件不实施代码、测试或 schema
- **Baseline:** `main` @ `0ec899b`，schema v12，v0.6.1 Competition Demo
  `KEEP_FROZEN`
- **Purpose:** 给下一次 Codex 实施会话提供可直接拆分、编码、测试和提交的最小整改包，
  无需重新进行架构探索。
- **Ordering:** TD-04 → TD-05 → TD-06；每项独立提交、独立 focused tests，最后全量回归。

## Shared boundaries

- 不实现 v0.7 WriteCommand、schema v13、ADR-008/009 或 Personal Experience UI。
- 不新增 Agent Tool，不修改七个 `READ_ONLY` Tool，不修改单步 executor。
- 不触碰 Hosted 写能力；Hosted 继续 READ_ONLY。
- 不修改、删除或重构 v0.6.1 Competition Demo 冻结面。
- 不重构整个 knowledge subsystem 或 AI provider framework。
- 不发起真实 EKB completion / embedding / rerank 调用。

本包不得弱化 v0.7 已冻结前提：WriteCommand 在 Agent Tool Registry 外；Personal Experience
复用 `knowledge_memory(kind=experience)`；草稿 session-only、批准 hash-bound；持久幂等
闭环 commit-unknown；经验+来源+FTS+审计+幂等结果原子提交；schema v13 tombstone 支持
undo/restore；写仅 Local，Hosted READ_ONLY；无持久聊天、无 autonomous multi-step
mutation，来源/prompt/模型内容的 write authority 为零。

---

## TD-04 — Orphan `knowledge_object_sources` on note/evidence deletion

### ID

`TD-04`

### Current severity

**HIGH — silent logical referential-integrity defect.** 数据库外键检查不会报告该问题，孤儿行
却会继续影响 provenance/source-integrity 读取。

### v0.7 dependency classification

**MUST FIX BEFORE v0.7 WRITE IMPLEMENTATION.** v0.7 将增加 experience provenance 和
更多来源生命周期；在此基础缺陷存在时继续扩展关系模型会放大错误并使来源存在性校验失真。

### Problem

`knowledge_object_sources` 用 `(source_type, source_id)` 表示 document/page/note/evidence
多态目标。只有 `knowledge_object_id` 有 FK；`source_id` 对各目标表没有 FK。因此：

- `NoteService.delete_note` 删除 `notes` 行时未删除
  `source_type='note' AND source_id=<note_id>` 的关系；
- `EvidenceBasketService.remove_item` / `_EvidenceRepository.delete_item` 删除单条
  `evidence_items` 时未清理对应 evidence 关系；
- `EvidenceBasketService.clear` / `_EvidenceRepository.clear_items` 批量删除 basket 项时
  未清理这些 item 的 evidence 关系。

结果是逻辑孤儿；`PRAGMA foreign_key_check` 无法发现，因为 SQLite schema 没有可检查的
目标 FK。

### Concrete evidence

- `src/migrations.py:958-975`：表只给 `knowledge_object_id` 声明 FK，`source_id` 是普通
  INTEGER；source type 闭合为 document/page/note/evidence。
- `src/note_service.py:631-649`：`delete_note` 直接执行 `DELETE FROM notes`。
- `src/evidence_basket_service.py:303-314`：公开 remove/clear 委托 repository。
- `src/evidence_basket_service.py:762-807`：repository 只删 `evidence_items` 并重排/
  更新时间。
- `src/document_deletion_service.py:364-405`：文档删除已经给出正确先例，在同一事务内先
  显式清理无 FK 的多态 KO source，再删原实体。

### Invariant

删除 note 或 evidence item 成功后，不得存在指向该原实体的 `knowledge_object_sources`。
关系清理、原实体删除、evidence position compact 与 basket timestamp 更新必须属于同一
事务：全部成功或全部回滚。不得影响指向其他 note/evidence、document/page 或其他 KO 的
来源关系。

### Likely files

- `src/note_service.py`
- `src/evidence_basket_service.py`
- `tests/test_note_service.py`
- `tests/test_evidence_basket_service.py`
- 仅在共享 fixture 明显更合适时：`tests/test_knowledge_object_service.py` 或
  `tests/test_source_fingerprint.py`

不需要 schema migration；一般不应修改 `src/migrations.py`。

### Minimal implementation

1. **delete note:** 在 `NoteService.delete_note` 的同一个数据库连接/事务中，先验证目标 note
   存在，再执行
   `DELETE knowledge_object_sources WHERE source_type='note' AND source_id=?`，随后删除 note。
   把当前事务外的 `get_note` 检查移入事务或以受影响行数守卫消除 TOCTOU；保留现有
   `NoteNotFoundError` / `NoteWriteError` 用户语义。
2. **remove evidence:** 在 `_EvidenceRepository.delete_item` 已有事务内，在删除
   `evidence_items` 前删除该 item 的 `source_type='evidence'` 关系；随后保留现有 position
   compact 与 basket timestamp 行为。
3. **clear evidence:** 在 `_EvidenceRepository.clear_items` 同一事务内，先用 basket 内 item
   ID 的子查询/已查询集合删除对应 evidence source rows，再删 items；不要在删除 items 后才
   查询 ID。
4. 清理 `DELETE` 对“该实体没有 KO source”必须天然幂等（影响 0 行仍继续）；公开删除 API
   对“原实体本身不存在”的既有错误/返回语义保持不变。不要为了整改把第二次
   `delete_note/remove_item` 改成静默成功。
5. 不新增通用 polymorphic framework；本项只修三个已证实的 lifecycle path。

### Transaction expectations

- 所有 SQL 使用服务当前管理的同一连接；禁止先提交关系清理再开启原实体删除。
- 任一清理、原删除、compact 或 timestamp 更新失败，事务回滚后原实体和所有关系保持原状。
- SQL 异常继续映射为现有安全 domain error，不泄露 SQL、路径或用户内容。

### Cleanup semantics

- 只删除与实际被删 note/evidence ID 精确匹配的 KO source link。
- 一个原实体被多个 KO 引用时，所有指向它的 link 都删除，KO 本身不删除、不改内容。
- 一个 KO 的其他来源保持不变；其他 basket、note、evidence、document/page 来源保持不变。
- `clear` 只清当前 basket 中被删 item 的 links；跨 basket item 不受影响。

### Focused test matrix

| Case | Setup | Expected |
| --- | --- | --- |
| Delete note with one/many KO links | 同一 note 被两个 KO 引用 | note 与两条 note-link 一起消失；KO 保留 |
| Delete note without KO links | 普通 note | 删除成功，清理 0 行不报错 |
| Delete missing note | 不存在 ID | 保持现有 not-found/error 语义；无其他变化 |
| Remove evidence with KO link | basket 有多项，目标项被 KO 引用 | 目标与其 link 消失；余项 position 连续 |
| Remove evidence without link | 普通 item | 现有成功行为不变 |
| Clear evidence | 当前 basket 多项均/部分被 KO 引用 | 当前 basket items 与对应 links 清空，返回计数正确 |
| Clear empty basket | 0 item | 返回既有 0 语义；无关 links 不变 |
| Unrelated sources | 同一 KO 另有 document/page/note/evidence 来源 | 只删目标 link，其余完整 |
| Cross-basket isolation | 另一 basket 有 evidence link | clear 当前 basket 不触及另一 basket |
| Failure injection | 关系清理后、原实体删除/compact 前抛错 | 全部回滚，无部分状态 |
| FK check plus logical check | 删除完成 | `foreign_key_check` 通过且显式 orphan 查询为 0 |

### Full-suite expectation

Focused tests 通过后运行完整 `pytest`、`ruff check .` 与仓库 `release_check`。现有 note、
evidence、KO provenance、source fingerprint、document deletion、backup/restore、Agent
read-only 和 Demo freeze 测试必须全绿；不得以 skip/xfailed 绕过。

### Risk

- cleanup 顺序错误会在删除原实体后丢失 basket 子查询集合；
- 分成两个事务会留下相反方向的部分状态；
- evidence compact 更新较多，新增 SQL 必须留在同一 transaction scope；
- 过宽 WHERE 条件可能误删其他 source type/ID 的关系。

### Non-goals

- 不重建 `knowledge_object_sources`，不增加 polymorphic FK 模拟层；
- 不修未证实的其他关系；不修改 KO、note 或 evidence UI；
- 不改变原实体 hard-delete 产品语义；不实现 v13 experience sources。

### Suggested commit message

`fix: clean knowledge object sources on note and evidence deletion`

### Definition of Done

- 三个删除路径在一个事务内清理精确关系且无逻辑孤儿；
- delete-note、remove-evidence、clear-evidence、无关来源、空/重复行为和回滚测试齐全；
- focused + full suite + ruff + release check 全绿；
- 无 schema、Tool、Hosted、Demo freeze 或真实 AI 调用变化。

---

## TD-05 — AI audit / budget composition boundary

### ID

`TD-05`

### Current severity

**MEDIUM — production paths are currently composed correctly, but enforcement is not structural.**
这是新增调用点后会放大的潜在审计/成本绕过面，不是已观测到的生产漏记账事件。

### v0.7 dependency classification

**MUST FIX BEFORE v0.7 WRITE IMPLEMENTATION.** 草稿生成将增加生产 completion 入口；先把
组合根收紧，才能保证 future write-related AI preparation 不会绕过审计或预算。

### Problem

`AuditedAIProvider.wrapped` 是公开属性，调用方可取得底层 provider 并直接调用；
`QwenProvider` 也可被任意代码构造。当前 Local `application_ai_provider()` 与 Hosted
`build_hosted_ai_provider()` 都正确地用 `AuditedAIProvider` + 预算 guard 装配，但不变量部分
依赖开发者不绕过。业务服务的通用 `CompletionProvider` 注入又允许裸 provider，这对 unit
test/mock 合理，对 production composition 不够强。

### Concrete evidence

- `src/ai/provider.py:348-387`：`AuditedAIProvider` 在 `self.wrapped` 暴露底层实例；ledger 和
  budget guard 允许 no-op 默认值。
- `src/runtime.py:118-158`：Local 组合根构造 `QwenProvider` 后正确包装审计与数据库预算。
- `src/hosted/ai_runtime.py:48-69`：Hosted 组合根同样正确包装。
- `src/ai/experience_model_service.py`、`src/ai/rag_answer_service.py`、
  `src/agent/decision/provider.py`：生产能力接受通用 provider，只有实例为
  `AuditedAIProvider` 时才传 feature/target audit metadata。
- `scripts/ai_smoke_test.py`、`scripts/ai_embedding_experiment.py`、
  `scripts/ai_real_*`、`scripts/phase3_embedding_calibration.py`：存在有意的裸 Qwen
  CLI/probe 构造；这些不是应用 production composition root，兼容性必须保留。

### Invariant

任何参与 Agent decision、final answer、experience draft generation 或 future
write-related AI preparation 的**生产** AI 路径，必须通过经批准的
`AuditedAIProvider`，并在网络调用前经过实际 `AiBudgetGuard`（或 Hosted 现有等价预算机制）。
业务代码不可获得底层 transport provider 的公共逃生口。低层 provider adapter tests、
deterministic mocks 与显式付费 CLI probes 可以使用裸 provider，但不得被生产组合根导入。

### Likely files

- `src/ai/provider.py`
- `src/runtime.py`
- `src/hosted/ai_runtime.py`
- 若生产入口需收窄：`src/agent/decision/provider.py`、
  `src/agent/response/final_answer.py`、`src/ai/rag_answer_service.py`、
  `src/ai/experience_model_service.py`
- `tests/test_ai_provider.py`
- `tests/test_ai_runtime.py`
- `tests/test_hosted_runtime.py`
- `tests/test_agent_decision_provider.py`
- `tests/test_agent_final_answer.py`
- `tests/test_experience_model.py`
- 可新增一个轻量 architecture/composition boundary test；不要建立复杂静态分析框架。

### Minimal implementation

1. 将 `AuditedAIProvider.wrapped` 私有化为 `_wrapped`，删除公共直达属性；包装器自身继续实现
   completion/embedding/rerank capability。测试需要观察 transport 时通过受控 fake/spy
   注入，而不是从生产对象向下穿透。
2. 抽取/收紧一个批准的 production provider factory（Local 与 Hosted 可共享最小 helper，
   也可各保留工厂但遵循同一契约）：构造时必须显式提供 ledger 与非 no-op budget guard，
   返回 `AuditedAIProvider`。不得在 factory 外的 `src/` 业务模块构造 `QwenProvider`。
3. 在 production service composition 边界增加 fail-closed guard，例如
   `require_production_audited_provider` 或等价类型/工厂约束。底层 service class 仍可接受
   deterministic mock 供单元测试；“通用测试注入”不能成为生产 root 的绕过路径。
4. `application_ai_provider()` 与 `build_hosted_ai_provider()` 必须只返回 `None` 或已完整装配
   ledger+budget 的 audited provider。Experience draft 必须复用同一批准入口，不新建第二个
   provider construction site。
5. 保留 scripts 的显式裸 provider 用法；通过模块边界/allowlist 测试标明它们是
   operator-invoked probes，不把 scripts import 到应用 runtime，也不改变其 dry-run、调用次数、
   token cap、retry 和审计输出兼容性。
6. 不改变 transport retry policy、provider protocol 或 vendor payload parsing。

### Production composition-root policy

- `src/ai/qwen_client.py` 定义 vendor adapter；批准的 Local/Hosted factory 是 `src/` 内唯一
  可实例化真实 Qwen transport 的位置。
- Agent decision、Final Answer、RAG、experience draft 从 factory 获得共享 wrapper；它们不
  import `QwenProvider`。
- 每种 feature 通过 wrapper 传入稳定 `source_feature` 与 target refs；预算 guard 在 wrapper
  调用底层 provider 前执行。
- no-op ledger/guard 只允许低层 isolated tests，不允许 production factory。

### Focused test matrix

| Case | Expected |
| --- | --- |
| Local API mode factory | 返回 audited provider；ledger 与真实 Local budget guard 生效 |
| Hosted factory | 返回 audited provider；Hosted budget guard 生效；thinking/retry 既有配置不回退 |
| Manual/missing key | 仍返回 None；不构造 Qwen、不联网 |
| Budget denial | completion/embedding 在 transport call=0 时以 typed budget failure 退出并记 rejected |
| Agent decision/final answer/experience draft | production composition 全部使用同一 approved audited path |
| Raw-provider bypass negative test | production root 被 monkeypatch/注入裸 provider 时构造 fail-closed，网络 call=0 |
| Public escape hatch | `AuditedAIProvider` 不再暴露 `.wrapped`；业务模块无法向下直调 |
| Source scan/allowlist | `src/` 中 Qwen 构造只在批准 adapter/composition files 出现 |
| Low-level fake/mock | unit tests 仍可直接注入 deterministic fake，无真实调用 |
| Scripts compatibility | 现有 smoke/experiment/real probe tests 保持调用次数、dry-run 与 retry 断言 |

### Full-suite expectation

运行 `ruff check .`、完整 `pytest` 和 `release_check`；重点确认 AI-offline startup、预算、AI
ledger、Agent reliability、Hosted runtime/security、scripts dry-run、secret scrub 与 Demo freeze
测试全绿。测试不得触发真实 AI。

### Risk

- 过度收窄 service constructor 会破坏 mock-first 测试；约束应放在 production composition
  边界而不是禁止所有 Protocol injection。
- 私有化 `.wrapped` 会影响当前测试的白盒断言；必须改成 factory spy/行为断言，不能新增
  另一个公共 accessor。
- Local 与 Hosted factory 若各自复制策略可能再次漂移；共享最小不变量测试比大规模框架
  重构更合适。

### Non-goals

- 不重写 provider framework，不更换 Qwen，不改变 vendor API、retry 或模型配置；
- 不禁止 scripts/adapter tests 的显式裸 provider；
- 不把 AI 变成启动依赖，不引入新预算产品或远程审计；
- 不实现 v0.7 草稿或 WriteCommand。

### Suggested commit message

`refactor: enforce audited budgeted production ai composition`

### Definition of Done

- production AI 入口只能取得完整 audited+budgeted wrapper；底层 provider 无公共逃生口；
- Agent decision/final answer/experience draft 的组合根负向绕过测试存在且 fail-closed；
- scripts 与低层 tests 兼容；AI-offline 和无真实调用不变量保持；
- focused + full suite + ruff + release check 全绿；无 Demo/Tool/schema 变化。

---

## TD-06 — Typed error semantics at Agent/AI boundaries

### ID

`TD-06`

### Current severity

**MEDIUM — user-facing classification is currently correct only while Chinese message text remains
stable.** 文案、翻译或供应商错误消息变化可使预算/引用/空上下文落入错误类别。

### v0.7 dependency classification

**MUST FIX BEFORE v0.7 RELEASE（可与 v0.7 主体并行，不阻塞最初 schema/WriteCommand
编码）。** v0.7 新写命令本身应从第一天使用 typed errors；旧 Agent/AI 提案链路的已证实
子串分类必须在发布门前移除，避免预算、provider、validation 与未来 approval/idempotency
错误混淆。

### Problem

`src/agent/response/final_answer.py` 捕获 `RagAnswerError` 后搜索“引用校验失败”“空上下文”
“无来源上下文”，捕获 `AIUnavailableError` 后搜索“预算”，再映射为结构化
`AgentResponseErrorCode`。机器语义依赖可变的人类文案。当前 `AIExecutionError.error_class`
和 `AgentResponseErrorCode` 已证明项目接受轻量 typed code，因此无需创建几十个异常类。

### Concrete evidence

- `src/agent/response/final_answer.py:150-185`：用 `in str(exc)` 判断 citation、empty context
  与 budget/provider unavailable。
- `src/ai/rag_answer_service.py:46,109-113,182-198`：所有安全拒绝共享一个
  `RagAnswerError`，只靠中文消息表达原因。
- `src/ai/provider.py:63-99`：已有 `AIError` / `AIUnavailableError` /
  `AIExecutionError(error_class)` 类型基础。
- `src/agent/response/contracts.py:33-50`：已有闭合 `AgentResponseErrorCode` 与结构化响应错误。
- `src/runtime.py:191-203`、`src/hosted/ai_runtime.py:38-45`：预算 guard 当前抛普通
  `AIUnavailableError`，与配置缺失/provider unavailable 同型。

### Invariant

跨 service/UI/Agent 边界的机器决策只读取异常类型、闭合 error code 或 typed result，绝不
读取消息子串。人类消息可独立改写而不改变分类。原始异常、SQL、密钥、路径和 provider
响应不得直接暴露给用户。

未来错误语义至少能稳定映射：validation、provider failure、budget failure、storage/
conflict、approval、idempotency；但按域复用少量基类 + code enum，不为每个文案建立异常类。

### Likely files

- `src/ai/provider.py`
- `src/ai/rag_answer_service.py`
- `src/agent/response/final_answer.py`
- `src/agent/response/contracts.py`（仅当现有 code 不足；优先复用）
- `src/runtime.py`
- `src/hosted/ai_runtime.py`
- `tests/test_ai_rag_answer.py`
- `tests/test_agent_final_answer.py`
- `tests/test_agent_reliability.py`
- `tests/test_ai_provider.py`
- `tests/test_agent_decision_provider.py`

### Minimal typed hierarchy / result model

1. 新增一个明确的 typed budget signal，例如
   `AIBudgetExceededError(AIUnavailableError)`；所有 Local/Hosted budget guard 抛该类型。
   `AIUnavailableError` 保留“未配置/能力不可用”，`AIExecutionError` 保留“已尝试但失败”。
2. 为 `RagAnswerError` 增加闭合 `RagAnswerErrorCode`（建议
   `EMPTY_CONTEXT`、`CITATION_INVALID`、`INVALID_OUTPUT`/`INTERNAL` 的最小集合），错误实例同时
   携带 `code` 与安全 message。也可用两个小 subclass，但不得按每条中文文案建类。
3. `FinalAnswerStage` 仅按 type/code 映射：budget → BUDGET_EXCEEDED，普通 unavailable →
   PROVIDER_UNAVAILABLE，citation → CITATION_INVALID，empty context → deterministic
   no-evidence，execution → FINAL_ANSWER_FAILED，未知 → INTERNAL_FAILURE。
4. v0.7 新域沿同一模式使用少量 code family：validation；provider/budget；storage/conflict；
   approval missing/mismatch；idempotency conflict/replay。`AlreadyCommitted` 是 typed 成功重放，
   不是异常。

### Compatibility path

- 保留现有 exception 基类继承关系，使捕获 `AIUnavailableError` / `RagAnswerError` 的低层调用方
  不立即破坏；新精确类型由窄到宽捕获。
- message 保持现有安全中文用户体验，但测试不得再靠 message 决定 code；只允许断言消息不
  泄密和适合展示。
- 若短期存在第三方/legacy 构造 `RagAnswerError("...")`，构造函数必须要求显式 code，或仅在
  私有兼容 mapper 中映射一次并标记删除；**不得**把子串 fallback 留在 Final Answer/UI。
- Agent execution/Tool 既有 enum 不重命名；用单一 mapping layer 转为 response code。

### Mapping layer

唯一映射位置保持在 `FinalAnswerStage`（或一个紧邻它的纯函数）：输入 typed domain/provider
failure，输出安全 `AgentResponseError` / no-evidence response。UI 只渲染结构化结果，不再次
解析 message。未来 WriteCommand UI 使用独立 domain-to-UI mapper，但沿用相同原则。

### Focused test matrix

| Case | Expected |
| --- | --- |
| Typed budget denial | BUDGET_EXCEEDED；transport call=0；消息可任意改写仍分类不变 |
| Provider unavailable | PROVIDER_UNAVAILABLE；不因消息含/不含“预算”而变化 |
| AI execution failure | FINAL_ANSWER_FAILED + safe detail error_class |
| Rag empty context | deterministic no-evidence；零 completion |
| Rag citation invalid | CITATION_INVALID；非法输出不显示 |
| Rag other typed failure | INTERNAL_FAILURE 或冻结的对应 code |
| Message mutation test | 参数化不同语言/无关键词 message，type/code 映射保持一致 |
| Unknown exception | INTERNAL_FAILURE；只暴露安全消息和 exception class 名 |
| Secret scrub | exception message 含测试 secret 时响应/日志不泄漏 |
| No retry | 每个失败仍最多一次逻辑调用；Agent retry_count=0 |

### Full-suite expectation

运行 `ruff check .`、完整 `pytest` 与 `release_check`。Agent decision/final answer/reliability、
RAG citation、AI ledger/budget、Hosted runtime、安全 scrub、七工具和 Demo freeze 必须全绿；
不得真实调用 AI。

### Risk

- 捕获顺序错误会让 `AIBudgetExceededError` 先被父类吞掉；精确类型必须先处理。
- 只修 budget 而保留 RagAnswer 文案解析仍未完成 TD-06；三个已证实的子串分支都要移除。
- 扩 enum 时必须更新 exhaustive mapping/test，避免未知 code 静默当成功。

### Non-goals

- 不统一全仓所有异常，不建立几十个 exception class；
- 不改 Tool Contract、single-step executor、provider retry 或用户文案体系；
- 不实现 v0.7 WriteCommand errors，只冻结与其兼容的分组方式；
- 不把内部 exception detail 暴露给 UI。

### Suggested commit message

`refactor: replace agent ai message parsing with typed errors`

### Definition of Done

- `src/agent/response/final_answer.py` 不再用消息/子串决定 budget、citation 或
  empty-context 分类；
- provider/RAG 发出闭合 typed semantics，mapping 单点、穷尽、fail-closed；
- message mutation、negative、scrub、no-retry 测试存在；
- focused + full suite + ruff + release check 全绿；无 schema、Tool、Hosted-write、Demo 变化。

---

## Package completion gate

三个整改可按顺序独立提交。Phase 0 关闭前必须：

1. TD-04、TD-05、TD-06 各自 Definition of Done 全部满足；
2. 完整 `pytest`、`ruff check .`、`release_check` 全绿且 0 新 skip；
3. 七个 Agent Tool 仍全为 `READ_ONLY`，single-step executor 不变；
4. Hosted 仍无 mutation API/service；Competition Demo `KEEP_FROZEN`；
5. completion/embedding/rerank 真实调用均为 0；
6. 才可进入 ADR-011 的 schema v13 列冻结与 v0.7 WriteCommand 实施。
