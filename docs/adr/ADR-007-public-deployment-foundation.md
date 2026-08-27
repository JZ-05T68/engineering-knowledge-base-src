# ADR-007: v0.6.0 Public Deployment Foundation — Hosted Profile, HTTP Service Boundary, Storage and Security

- **Status:** Accepted
- **Date:** 2026-08-27
- **Deciders:** EKB maintainer（本地单用户工程知识库）
- **Scope:** v0.6.0 Public Deployment Foundation 架构冻结
- **Supersedes:** 无（ADR-006 之后第二个独立 ADR；v0.5.x 使用内联 ADR）
- **Related:**
  - `docs/v0.6.0-public-deployment-entry-architecture.md`（本 Gate 主文档）
  - `docs/v0.6.0-public-deployment-readiness-inventory.md`（Codex Inventory）
  - `docs/adr/ADR-006-agent-foundation.md`（Agent contract，不重开）

## Background

Phase 2 Single-step Agent Foundation 已 CLOSED（Phase 2D Gate PASS / HIGH）。
Codex Public Deployment Readiness Inventory 确认：当前应用是 loopback Streamlit
单用户本地系统，无 auth、无 per-user isolation、无专用 HTTP API、SQLite v12、
AI key server-side、AI 预算默认 unlimited、无 rate/concurrency 边界、存在大量
filesystem write surface 与 Windows 运维假设。直接将当前应用发布公网会同时暴露
匿名写面、跨会话数据共享、成本滥用、路径/文件系统 authority 与私密数据风险。

v0.6.0 的目标是 Agent Foundation + 公网可访问 + 可稳定演示，而不是完整多用户
SaaS 或本地功能全量云化。

## Decisions

### 1. Local / Hosted split

**Decision:** Local Mode 保留完整 Personal EKB（现状，含 import、写工作流、
OCR、backup/restore/export/delete/maintenance、Streamlit UI、Windows launcher）。
Hosted Mode 是一个新增的、独立的、只读的窄 profile，不修改 Local 行为。

**Reason:** Local-first 是产品哲学最高优先级；公网演示不应反噬本地功能。

**Consequences:** Local 默认行为不变；Hosted 是新 server entrypoint 与受控
配置集，不共享 Local 的写面暴露。

### 2. Hosted profile scope

**Decision:** v0.6.0 Hosted = **Public Read-only Single-tenant Agent Demo**：
一个专用单进程 HTTP Agent API + 运维方预置、脱敏、公开授权的 curated demo
corpus + 匿名访问 + abuse controls。

**Reason:** 当前无 identity / 无隔离 / 单用户 SQLite / 大量 filesystem write；
narrow profile 使 Inventory 的 6 个 BLOCKER CANDIDATES 变为不适用，同时满足
比赛公网 Agent 演示。

**Consequences:** upload、全部写类 workflow、backup/export/delete/maintenance、
多用户私有数据在 v0.6.0 Hosted 一律 DENY 或 LOCAL_ONLY。

### 3. HTTP service boundary

**Decision:** Local 保持 Streamlit；Hosted 新增专用 Python HTTP API（FastAPI，
单进程单 worker），最小 endpoints：`POST /v0.6/agent/run`、
`GET /v0.6/sources/{stable_id}`、`GET /health`、`GET /ready`。HTTP 层是 thin
adapter，复用 `SingleStepAgentService.run`，禁止在 controller 重写业务逻辑。

**Reason:** services 已 UI-independent；Streamlit 自带写页面不宜公网；
FastAPI + pydantic 与现有 pydantic-settings 技术栈同族，验证与错误边界成本最低。

**Consequences:** 新增 FastAPI/uvicorn 依赖（Hosted runtime 使用，Local 启动
不依赖）；HTTP DTO 必须是显式投影，不是内部对象无限制序列化。

### 4. Security authority

**Decision:** 未来 frontend 一律视为 UNTRUSTED CLIENT。客户端不能指定 tool、
model、provider、credential、budget、file path。server authority 完全在后端；
Hosted server 模块不 import / 不构造任何写类 service（代码层不可达，不是隐藏
按钮）。

**Reason:** 公网边界与本地信任边界不同；Agent 的 READ_ONLY 由 registry + policy
保证，Hosted 的只读性由“不挂载写面”保证，二者互补。

**Consequences:** HTTP input 白名单仅 `text`（≤120k chars）+ 可选 correlation id；
server 生成 request_id；写面 service 在 Hosted 模块不可达。

### 5. Storage / data-root model

**Decision:** Hosted 采用 `APP_CODE_ROOT`（read-only）与 `DATA_ROOT`（持久可写，
环境变量必填）分离。v0.6.0 Hosted 持久数据只有 `DATA_ROOT/database/knowledge.db`
（+WAL/SHM）与 `DATA_ROOT/logs/`；raw/PDF/PNG/Markdown/quarantine/backups/exports
在 Hosted **NOT PRESENT**。

**Reason:** 7 个 READ_ONLY Agent Tool 全部 DB-backed（含 source fingerprint 从
DB 行重算），最小 demo 不需要文件资产；repo 可写是本地假设，不应进入 Hosted。

**Consequences:** Local 保持 repo-relative 布局；Hosted 必须有持久卷并禁止依赖
可写 repo。

### 6. SQLite decision

**Decision:** v0.6.0 Hosted 保留 SQLite（schema v12），**HARD CONSTRAINT：单进程、
单 worker**；WAL 开启；启动执行既有幂等 migration；不迁移 PostgreSQL。

**Reason:** demo 负载 = 读为主 + audit append；Agent Tool 无文件 I/O；迁移
PostgreSQL 的 scope 与 v0.6.0 收口目标不匹配。

**Consequences:** 多 worker / 多进程共享 Hosted SQLite 被明确禁止；readiness
必须验证 DB 可读且 schema 版本兼容。

### 7. Public demo data policy

**Decision:** demo corpus = **deployment-seeded, sanitized, read-only baseline**
（预构建 demo DB artifact 或幂等 seed），schema v12，non-private。
**禁止把 developer 的 `data/database/knowledge.db`、PDF、PNG、Markdown、backups
部署公网。**

**Reason:** LOCAL PRIVATE DATA MUST NOT BE DEPLOYED BY DEFAULT 是硬安全要求；
生产 DB 内容未经公开授权。

**Consequences:** 部署物必须显式排除 `.env`、生产 DB、个人 PDF/PNG、backups、
logs、审计私密内容、PID/runtime state。

### 8. Server-side secrets

**Decision:** AI provider key 只来自环境 / secret injection，server-side only；
Hosted profile **禁止读取 repo `.env`**（`_env_file=None`），防止误用开发者本地
凭据。key 永不进入 client / logs / response / bundle / DB / backup。

**Reason:** 现状已是 server-side（`SecretStr`），补 Hosted ownership 与启动语义。

**Consequences:** Hosted 缺 key 时进程可启动（`/health` ok），`/ready` 返回
`503 ai_not_configured`，Agent 端点返回结构化 503；不退回本地凭据。

### 9. No private upload by default

**Decision:** v0.6.0 Hosted **UPLOAD = DISABLED**。不实现任意公网用户上传私人
知识库；不自动把本地资料同步到 Hosted。

**Reason:** upload 引入 ownership、跨用户去重、容量、parser 安全、temp 并发、
长任务、保留期等问题，demo 不需要。

**Consequences:** Hosted 无 upload route、无上传存储类；未来启用需独立 ADR。

### 10. Bounded public Agent access

**Decision:** Hosted 必须同时具备四类边界：Agent logical ceiling（已冻结 ≤2）、
finite AI token budget（Hosted 不允许 0=unlimited，否则 NOT READY）、HTTP rate
limit（middleware/edge，默认 10 agent runs/min/IP，env 可调）、concurrency limit
（默认 4 active runs，超限 429 fail fast）。

**Reason:** budget ≠ rate ≠ concurrency ≠ logical ceiling；四者不可互相替代。

**Consequences:** HTTP request body ≤512,000 bytes；Agent text ≤120,000 chars；
超限 fail closed；无分布式队列。

### 11. Deployment / runtime profile

**Decision:** profile 显式化：`EKB_RUNTIME_PROFILE=local|hosted`。禁止
“检测到云环境就猜测 Hosted”的 implicit heuristic。Hosted 专用安全配置缺失时
startup / readiness FAIL，不退回 unrestricted local behavior。

**Reason:** fail-closed 是公网安全底线。

**Runtime/Profile clarification（WP1，maintainer 明确冻结）：**

- `EKB_RUNTIME_PROFILE` 环境变量完全不存在 → `RuntimeProfile.LOCAL`，这是
  Local backward-compatibility default，不要求现有 Streamlit/BAT/VBS/developer
  workflow 新增环境变量。
- 精确值 `local` → LOCAL；精确值 `hosted` → HOSTED。
- 变量存在但为空、纯空白、未知值、大小写变体（如 `LOCAL`、`Local`、`HOSTED`、
  `Hosted`）一律 INVALID / FAIL CLOSED，绝不回退 LOCAL。
- 不 strip-and-guess、不做 alias/fuzzy matching、不推断云环境。
- 只有变量 **ABSENCE** 获得 LOCAL 默认；Hosted entry 必须显式
  `EKB_RUNTIME_PROFILE=hosted`。选中 Hosted 后，缺失/非法 Hosted 配置不得回退 Local。

这是对“Local 默认不变”与“Hosted 显式 opt-in”的细节澄清，不改变其它架构决策。

**Consequences:** Hosted target runtime = Linux（container）；Windows BAT/VBS/
service_manager/diagnostic = LOCAL_ONLY；repository 在 Hosted 只读；持久卷
REQUIRED。

## Consequences Summary

- Hosted v0.6.0 = 最小安全公网 Agent 演示 profile，不是 Local 云化、不是多用户 SaaS。
- Local 功能完整保留；写能力、上传、备份/导出/维护、多用户隔离整体 DEFER。
- 新增 FastAPI thin HTTP boundary，复用 frozen Agent + domain services。
- 存储最小化到单个 SQLite DB（DATA_ROOT，单 worker）+ 日志。
- 公网访问受 rate / concurrency / body / budget 四重边界约束，秘密 server-side。
- schema v13 不引入；Phase 2 Agent contract 不重开。

## References

- `docs/v0.6.0-public-deployment-entry-architecture.md`
- `docs/v0.6.0-public-deployment-readiness-inventory.md`
- `docs/adr/ADR-006-agent-foundation.md`
- `docs/v0.6.0-phase2d-reliability-integration.md`
- `src/agent/response/pipeline.py`、`src/agent/execution/contracts.py`
- `src/config.py`、`src/runtime.py`、`src/migrations.py`、`src/source_fingerprint.py`
