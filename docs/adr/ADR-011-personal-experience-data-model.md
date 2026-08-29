# ADR-011: Personal Experience Data Model — Extend Knowledge Memory with Provenance, Idempotency and Tombstone Semantics

- **Status:** PROPOSED（待维护者 / DeepSeek-Pro 评审，未经批准不得实现）
- **Date:** 2026-08-29
- **Deciders:** EKB maintainer（本地单用户工程知识库）
- **Scope:** v0.7 Personal Experience 领域模型与 schema v13 概念设计
- **Supersedes:** 无；复用并扩展 schema v12 Knowledge Memory
- **Related:**
  - `docs/v0.7-personal-experience-agent-architecture-prestudy.md`
  - `docs/adr/ADR-010-v0.7-controlled-write-boundary.md`
  - `docs/v0.7-write-failure-matrix.md`
  - `docs/v0.7-implementation-readiness-plan.md`

## Context

仓库已有完整的 Knowledge Memory 域：`knowledge_memory_entries.kind` 包含
`experience`，并已有 title/content/root_cause/lesson/outcome/context_conditions、
active/archived 生命周期、Knowledge Memory FTS，以及只读 Agent 的
`knowledge_search` / `get_knowledge_memory` 入口。`KnowledgeEpistemicBasis` 还已有
`PERSONAL_EXPERIENCE` 与 `DIRECT_OBSERVATION` 先例，且明确这些依据由用户声明，不能从
来源链接自动推导。

v0.7 需要的不是第二套 PersonalExperience 存储，而是为“用户批准的 AI-assisted
experience”补足现行 v12 没有的 provenance/origin、墓碑、持久幂等、write audit 和
多来源引用语义。

## Problem

新建平行 PersonalExperience aggregate 会复制 FTS、CRUD、检索、引用、stable-id 与
Agent read path，并使经验无法自然进入现有 Knowledge Memory。另一方面，仅向现有行写入
一段 AI 组织文本又不能表达：哪些是用户断言、哪些是直接观察、哪些只是 Agent 整理或
推断、哪些由已有来源支撑；也不能解决 commit-unknown、undo 和“成功写必须有审计”。

需要一套最小概念扩展，既复用 Knowledge Memory，又不把命令/批准语义混入其核心内容。

## Constraints

1. Personal Experience 必须存入 `knowledge_memory_entries` 且 `kind = experience`。
2. v0.7 不建平行 aggregate、草稿表、批准表、聊天表或事件溯源系统。
3. 草稿 session-only；批准是一次性、hash-bound 且不持久化。
4. 创建、undo、restore 都必须幂等且有原子 write audit。
5. AI 生成文本永远不是独立证据；mutation approval 与事实确认是不同语义。
6. undo 使用 soft-delete/tombstone，v0.7 不 hard purge。
7. 本 ADR 只做概念设计；不包含 SQL、migration 或 schema 实施。

本数据模型继承 ADR-010 的执行边界：WriteCommand 位于 Agent Tool Registry 之外；七个
`READ_ONLY` Tool 与 single-step Agent 不变；草稿 session-only、批准 hash-bound；写仅
Local，Hosted READ_ONLY；无持久聊天或 autonomous multi-step mutation，来源/prompt/模型
内容的 write authority 为零。

## Decision

### 1. Personal Experience 扩展现有 Knowledge Memory

Personal Experience 是 `knowledge_memory_entries` 的一种，固定
`kind = experience`。继续复用现有 stable-id、Knowledge Memory 服务、FTS、搜索、读取、
备份/恢复和 Agent 只读工具。不得新建 `personal_experiences` 主表或第二套读模型。

内容继续落在 Knowledge Memory 的经验字段/结构化内容中；命令、批准 hash、幂等与审计
元数据不得塞入 content 正文，也不得把完整草稿复制进审计表。

### 2. 区分创建方式与内容的认识论来源

记录级 `creation_origin`（概念名）只说明创建方式：`HUMAN` 或
`AGENT_ASSISTED`。它不证明内容真假。

内容区块/声明级 provenance 必须至少区分：

| 标记 | 含义 | 可否被称为确认事实 |
| --- | --- | --- |
| `USER_ASSERTED` | 用户明确陈述或亲手编辑确认的内容；不自动声称有外部证据 | 可表述为“用户陈述”，不能伪称来源已验证 |
| `DIRECT_OBSERVATION` | 用户明确声明为亲眼/现场/测量所得的直接观察；不得由链接或模型推导 | 可表述为“用户直接观察”，仍应保留其用户声明性质 |
| `AGENT_ORGANIZED` | Agent 在不增强事实强度的前提下重组、压缩或结构化用户/来源内容 | 不可；只是组织后的提案文本 |
| `AGENT_INFERRED` | Agent 添加的因果、诊断、归纳或其他超出明示材料的推断 | 不可；必须显式标为推断 |
| `EXISTING_SOURCE_SUPPORTED` | 声明由一个或多个已验证存在的本地来源引用支撑 | 仅可声称“有这些来源支持”，不可超出来源内容 |

一个区块可以同时关联来源引用，但来源链接本身不能把 `AGENT_INFERRED` 静默升级成
`EXISTING_SOURCE_SUPPORTED`；必须有明确的 claim-to-source 关系或保守保持原标记。

用户批准完整草稿只授予 mutation authority，不自动把所有 `AGENT_ORGANIZED` /
`AGENT_INFERRED` 内容提升为 `USER_ASSERTED`。若要确认更强事实（尤其 root cause），UI
必须提供字段级显式确认或让用户亲手改写，并把该动作纳入批准载荷。

示例：用户说“换线以后好了。”，Agent 写“根因是编码器反馈极性错误。”。除非用户明确
确认这一更强根因，或现有来源确实支撑该因果结论，否则后一句必须保持
`AGENT_INFERRED`（或仅作为 `AGENT_ORGANIZED` 的候选措辞），绝不能静默成为用户确认事实。

### 3. schema v13 的最小概念增量

#### 3.1 `knowledge_memory_entries` 扩展

v13 概念上需要：

- 记录级 `creation_origin`（HUMAN / AGENT_ASSISTED）；
- 用户原始结局陈述（沿用/明确 v12 `outcome` 的职责，列冻结时不得无理由再造同义列）；
- root-cause 用户显式确认标记或等价的字段级 provenance；
- 可选 Agent request/run 关联标识，只存 ID，不存 prompt/对话正文；
- 生命周期增加 `deleted` tombstone，保留既有 `active` / `archived`；
- 能承载声明级 origin/provenance 的最小结构。列冻结评审可选择规范化子表或受约束 JSON，
  但必须保持闭合枚举、可验证、可迁移，不能只靠自由文本约定。

现有 title/content/root_cause/lesson/outcome/context_conditions 与搜索影子列必须保持兼容；
v12 行迁移后语义不得被猜测回填，未知 provenance 应显式保守表示。

#### 3.2 来源 / provenance 引用

增加概念上的 `knowledge_memory_sources`，允许一条经验关联多个 document/page/
knowledge_object/evidence 来源。实现列冻结时应使用可受真实 FK 约束的 typed target 列
（而不是重演 `knowledge_object_sources(source_type, source_id)` 的无 FK 多态孤儿问题），
并满足：

- `entry_id` 关联 Knowledge Memory，经验删除墓碑时不物理删除来源关系；
- 创建提交时每个目标必须存在，来源类型在闭合 allowlist 内；
- 同一 entry + 同一 typed target 唯一；
- 每条 claim/区块能关联零到多个 source reference；
- 来源后来被删除时，必须按冻结的 SET NULL/tombstone 语义保留“曾引用何 stable-id”的
  最小快照并显示 missing，不得产生看似仍有效的逻辑孤儿；
- 不保存来源正文副本、绝对路径或敏感指纹载荷。

具体 typed FK 列与删除动作的组合必须在 Phase 1 列冻结评审中一次决定并用迁移/删除测试
证明；本 ADR 冻结的是“真实约束、可显示 missing、不得使用无 FK 裸多态引用”的不变量。

#### 3.3 `experience_command_log`

新增 append-only 持久命令日志，覆盖 create / undo / restore。最小概念字段包括：

- 全局唯一 `idempotency_key`；
- `request_id`、operation、actor、target entry/stable-id；
- `payload_sha256` 与 create 时的 `approved_draft_sha256`；
- 成功结果及首次提交时间；
- `creation_origin` 与必要的最小长度/计数元数据。

该行同时承担 v0.7 的 write audit 与幂等成功结果，避免另建一份必须同步的成功审计表。
它与经验行、来源关系、FTS 可见状态处于同一事务：日志插入失败即 mutation 回滚。失败且
已回滚的尝试只进入净化后的应用日志，不在数据库伪造“已提交”审计行。

命令日志不得保存正文、完整 prompt、模型输出、chain-of-thought、provider reasoning、
密钥、绝对路径或用户完整对话。

### 4. Idempotency and commit-unknown

`idempotency_key` 由 UI 首次构造命令时生成 UUID-style 值；重试保持相同。应用服务在同一
事务内解析唯一键：

- 同键、同 `payload_sha256`：返回首次成功的 target/result，`duplicate_replay=true`；
- 同键、不同 payload：`IdempotencyConflict`；
- 无日志行：执行首写，并在 COMMIT 前写入结果行。

因此 COMMIT 成功但响应丢失后，进程重启或 UI 重试仍能找回原 stable-id，永不创建第二条
经验。命令日志与备份/恢复一起移动；恢复到提交前的备份点时，经验与日志都不存在，语义
仍自洽。

### 5. Tombstone lifecycle

v0.7 undo 把受控创建的经验从 `active` 转为 `deleted`，restore 把 `deleted` 转回
`active`；两者均是独立的显式、幂等、受审计命令。`deleted` 项不得出现在普通 FTS/读取
结果中，但仍保留内容、provenance 和命令审计以便恢复。v0.7 不提供 hard purge，也不把
`archived` 复用为删除，因为“归档但仍存在”与“用户撤销、默认不可见”语义不同。

### 6. Conceptual constraints and indexes

实现列冻结至少应证明：

- `kind = experience` 对受控创建路径是服务层 allowlist；DB 既有 kind CHECK 继续生效；
- origin/provenance、operation、result、status 都是闭合枚举；hash 是 64 个十六进制字符；
- `idempotency_key` 唯一，命令日志只追加；target/result 非空关系符合 operation；
- 来源关系有 entry 索引、各 typed target 索引与 per-entry target 唯一约束；
- 普通读取/FTS 排除 `deleted`，restore 后重新可见；
- 既有 kind/status/updated_at 与 FTS 索引/触发器兼容；
- source、审计、幂等任一约束失败均导致同一事务回滚。

本节不授权 SQL 或迁移实现。

## Why schema v13 is required

复用 Knowledge Memory 只避免第二套领域模型，不表示 v12 已具备安全写语义。v12 缺少：

1. session 批准之外的持久幂等结果，无法闭环 commit-unknown；
2. 成功 mutation 的 append-only write audit；
3. AI-assisted 内容的声明级 origin/provenance；
4. 与 archived 不同的 deleted tombstone；
5. 可表达多来源且具真实约束的 Knowledge Memory 来源关系。

这些都是 v0.7 正确性不变量，不是可选 UI 元数据，因此推荐 schema v13。草稿与批准仍不
入库；v13 只持久化已批准、已提交的用户资产及其最小命令证明。

## Consequences

### Positive

- 经验写后立即复用现有 FTS、搜索、stable-id、Agent read path 和备份恢复。
- provenance 防止 AI 整理/推断被静默包装成用户确认事实。
- 持久幂等 + 原子审计覆盖双击、重试和 commit-unknown。
- tombstone 提供可恢复 undo，无需事件溯源或 hard purge。

### Costs and risks

- v13 需触碰 Knowledge Memory 状态/模型与 FTS 过滤，迁移必须沿 v10-v12 的事务、备份、
  指纹和失败注入纪律。
- 多来源 typed FK 与删除后 missing 表示需要列冻结评审；TD-04 必须先修，避免复制孤儿语义。
- 声明级 provenance 增加 UI 展示和测试责任，但不能降级为自由文本标签。

## Rejected alternatives

### Parallel `personal_experiences` aggregate

拒绝。它会复制 FTS、CRUD、引用、stable-id、搜索和 Agent Tool，并与已有
`knowledge_memory_entries.kind = experience` 直接重叠。

### Persisted drafts or approvals

拒绝。v0.7 没有跨 session 草稿恢复需求；持久批准会形成可重放授权凭据。草稿与批准均
session-only，只有成功命令结果持久化。

### Reuse `archived` as undo

拒绝。archived 是保留且仍存在的知识生命周期；undo 需要默认不可见、可恢复的 deleted
tombstone，二者不能静默混用。

### AI text as evidence after whole-draft approval

拒绝。批准只授权写入，不改变认识论来源。AI 文本只能是 organized/inferred；更强事实需
用户字段级确认或现有来源支持。

### Unconstrained polymorphic `(source_type, source_id)` links

拒绝。TD-04 已证明这种关系可产生 `foreign_key_check` 无法发现的逻辑孤儿。v13 来源关系
必须使用真实 typed FK 或等价的可验证完整性机制。

## References

- `docs/v0.7-personal-experience-agent-architecture-prestudy.md`
- `docs/adr/ADR-010-v0.7-controlled-write-boundary.md`
- `docs/v0.7-write-failure-matrix.md`
- `docs/v0.7-implementation-readiness-plan.md`
- `src/migrations.py`（schema v9-v12、Knowledge Memory FTS）
- `src/models.py`（KnowledgeMemoryEntry、KnowledgeEpistemicBasis）
- `src/knowledge_memory_service.py`
- `src/agent/tools/bootstrap.py`
