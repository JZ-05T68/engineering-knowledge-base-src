# EKB 仓库级架构 / 可靠性 / 技术债审计 — 2026-08-29

- **基线：** `main` @ `7e7a2938c0f42346e27dcc164bd5ee1437125f52`（= origin/main，工作树干净，单 worktree，无 stash 操作）。
- **模式：** 只读审计。生产代码改动 = 0；真实 AI 调用 = 0（completion/embedding/rerank 均为 0）；未触碰 Zeabur/云控制台/部署。
- **冻结边界：** v0.6.1 Competition Demo（`docs/v0.6.1-competition-demo-freeze.md`）完整保留，本审计未建议解冻。
- **方法：** 全仓静态阅读（src 36.3k 行 / tests 49.5k 行 / docs 170+ 篇 / git 历史 205 commits）+ 五路子系统深查（存储、AI/RAG/Agent、Hosted/配置、Demo/UI、测试/文档/版本）+ 关键结论逐条人工复核。
- **配套文档：** `docs/technical-debt-backlog-post-v0.6.1.md`（技术债清单）、`docs/v0.7-readiness-audit.md`（v0.7 前置矩阵）。

---

## Executive Summary

**架构健康度：HEALTHY_WITH_DEBT。v0.7 就绪度：CONDITIONAL_READY。P0 = 0。**

这是一个纪律性远超其规模直觉的代码库。冻结清单声称的关键性质全部被证据支撑：
演示与生产零耦合（import 扫描 + socket 哨兵 + DTO superset 测试机械强制）、
Agent 全链 fail-closed（单一引用校验器、2 次模型调用硬上限、0 自主重试）、
存储层数据安全工程化（版本边界事务迁移 + 迁移前备份 + 指纹不变量 + 校验-换位恢复）、
Hosted 纵深防御（profile 门卫 + deny-by-default 打包 + 封闭词表日志）。
债务是真实的但**没有一项是 P0**，且没有一项要求解冻比赛演示。

最重要的三个发现都在"过程与测试"而非产品代码里：

1. **发布门禁已与冻结树互斥**（R-01）：`scripts/release_check.py:449-467` 要求每个
   页面的 `page_title` 含 `v0.6.0`，而冻结的 `pages/0_知识Agent.py:603` 页面标题无版本号
   —— 冻结后门禁从未重跑，若现在重跑会 FAIL。这是流程漂移，不是产品缺陷。
2. **测试套件无环境隔离**（R-02）：`tests/conftest.py` 只有 1 个 fixture，无套件级
   env/数据根隔离；本机 `.env` 含真实 AI key（`EKB_AI_MODE=api`），3 个测试文件构造
   `Settings(...)` 未传 `_env_file=None`，静默吸入真实凭据（仅入内存，未打印、未入快照）。
3. **活文档与现实矛盾**（R-03）：`docs/v0.5.x-roadmap.md:42` 仍写 v0.6.x
   "尚未开始，尚未实现"，而 v0.6.0 已 RELEASED。

产品代码侧最高优先的是 R-04（删除单条 note/evidence 后多态来源链接成孤儿，
`PRAGMA foreign_key_check` 不可见）——v0.7 的 Agent 记忆写入会放大它，
应在 v0.7 开工前修复。

## Repository Architecture Map

| 子系统 | 主文件 | 职责 | 对外接口 | 依赖 | 数据 | 风险 |
| --- | --- | --- | --- | --- | --- | --- |
| UI / Streamlit | `app.py`, `pages/*`, `src/*_ui.py` | 演示与编排；本地 only | Streamlit 页面 | `src.runtime` 单例工厂 | 经服务层读写 | 中（pages/4 1575 行；阻塞调用） |
| 配置 / 运行档案 | `src/config.py`, `src/runtime_profile.py`, `src/hosted_config.py` | LOCAL/HOSTED 显式 profile；staging 子模式 | `runtime_settings()` / `load_hosted_settings()` | pydantic-settings | env/.env（Hosted 禁 dotenv） | 低 |
| 组合根 | `src/runtime.py` | `lru_cache` 单例工厂；AuditedAIProvider 组装；日志 | `application_*()` | 全部服务 | 进程级 | 低 |
| 导入 / 处理 | `src/pdf_service.py`, `document_service.py`, `ocr_*`, `batch_*` | PDF→PNG/文本/OCR，多事务可续传 | `DocumentService` | Database, PdfService, OCR | data/raw,pages,markdown | 中（多事务） |
| 存储 / SQLite | `src/database.py`(3734), `src/migrations.py`(2092) | schema v12；FTS5 影子列；事务 | `Database` | sqlite3, jieba | data/database/knowledge.db | 中（god-module，但内部纪律好） |
| 检索 | `src/search_service.py`, `search_*.py`, `knowledge_search_service.py`, `src/ai/hybrid_search.py`, `vector_recall.py` | lexical FTS + 可选向量 RRF | `SearchService`, `HybridSearchService` | Database, provider | 读 | 低 |
| 知识对象 / 记忆 | `knowledge_object_service.py`, `knowledge_memory_service.py`, `source_fingerprint.py` | KO/记忆 CRUD、修订链、指纹状态机 | 服务类 | Database (`knowledge_transaction`) | v10/v12 表 | 中（来源孤儿 R-04） |
| 证据 / 溯源 | `evidence_basket_service.py`, `evidence_service.py`, `knowledge_context*.py` | 证据篮、ContextItem 投影、上下文打包 | 服务类 + packager | Database, models | 读+写 | 低 |
| AI 供方层 | `src/ai/provider.py`(591), `qwen_client.py`(481) | vendor-neutral 契约 + Qwen 适配 + 审计/预算 | `CompletionProvider` 等 | urllib | ai_calls 账本 | 低（边界治理 R-05） |
| RAG | `src/ai/rag_answer_service.py`, `rag_prompt_builder.py` | 引用校验、grounded 语义 | `RagAnswerService` | provider, packager | 读 | 低 |
| Agent | `src/agent/**`（decision/execution/response/tools） | 单步只读 Agent；7 Tool；最终答案 | `SingleStepAgentService` | registry→adapters→服务 | 内存 trace | 低 |
| Hosted API | `src/hosted_api/*`, `src/hosted/*` | FastAPI 薄边界；限流/并发/体积/预算四重门 | `POST /v0.6/agent/run` 等 | agent 组合根 | DATA_ROOT 只读 DB | 低 |
| Demo | `src/demo/*`, `src/demo_ui.py`, `src/agent_client.py` | 确定性演示；Mode1/Mode2 | `MockDemoClient`, `HostedAgentClient` | hosted_api 契约 | 无 DB | 低（冻结） |
| 备份 / 服务管理 | `src/backup_service.py`(1283), `deletion_recovery.py`, `document_deletion_service.py`, `scripts/service_manager.py` | 校验备份、隔离区删除、Windows 服务 | CLI/UI | Database, os | backups/, quarantine | 低 |
| 发布 / CI | `scripts/release_check.py` | 手动发布门禁 | CLI | pytest/ruff | - | 中（R-01/R-07；无 CI） |

依赖方向总体健康：无 domain→UI、无 production→demo（仅 `agent_client.py:28` 一处
故意的 `DemoHTTPError` 命名反向，冻结内）、无 hosted→Streamlit、无 demo→生产 DB、
无 storage→presentation、Agent 契约不触具体 provider（`test_agent_execution.py:757` 强制）。
三处轻微反向：`AILedgerService` 触碰 `Database._connection` 私有连接
（`ai_ledger_service.py:65,91,128,138,239`，有 noqa）；`src/demo.contracts` 传递性加载
hosted readiness 模块（import 层面，无副作用）；`scripts/*` 直接构造裸 `QwenProvider`（R-05 先例）。

## Top Findings

1. **R-01 [P1] 发布门禁与冻结树互斥** — `release_check.py:449-467` 遍历 `app.py`+`pages/*.py`，
   要求 `page_title=`/`st.title(` 行含 `v0.6.0`；`pages/0_知识Agent.py:603` 为
   `page_title="知识 Agent · 工程知识库"`（无版本），v0.6.1 版本行经 HTML div 渲染（`:621`），
   过滤器看不到。冻结证据（2685 passed）是 pytest 证据，不是门禁证据；门禁自演示冻结后未重跑，
   现在重跑必 FAIL。另 `release_check.py:957` 描述仍是 "v0.5.3"。**不影响演示正确性；赛后修门禁即可。**
2. **R-02 [P1] 测试无套件级环境隔离** — `tests/conftest.py`（27 行）仅有阻断
   `application_settings` 的 autouse fixture；`test_navigation_ui.py:37`、`test_v008_ui.py:25`、
   `test_release_check.py:137` 构造 `Settings(...)` 未传 `_env_file=None`，
   而 `Settings.model_config` 指向 `PROJECT_ROOT/.env`（`config.py:54-57`），本机 `.env`
   含 `EKB_AI_MODE/EKB_AI_API_KEY`（真实凭据）。路径均被 tmp_path 覆盖（无生产数据风险），
   凭据只入内存未打印，但测试结果依赖机器环境，违背"测试不读真实凭据"的意图。
   对照组（正面证据）：`test_hosted_api.py:44-60` 与 `test_demo_contract.py:53-63`
   已有完整的 env 清除 + socket/dotenv 哨兵。
3. **R-03 [P1] 活文档 roadmap 漂移** — `docs/v0.5.x-roadmap.md:21,42`：v0.5.x 仍标
   "当前版本线"、v0.6.x 标"未来规划；尚未开始，尚未实现"；与 `README.md:336`
   （v0.6.0 RELEASED/CLOSED）和 CHANGELOG v0.6.0 章节矛盾。历史冻结文档（release notes、
   gate 记录）无需改；这是活文档。
4. **R-04 [P2] 多态来源孤儿链接** — `knowledge_object_sources` 无 FK（设计使然，
   `migrations.py:905-907`）；文档级删除有完整清理（`document_deletion_service.py:369-406`），
   但 `note_service.delete_note`（`note_service.py:631-649`）与
   `evidence_basket_service.remove_item/clear`（`evidence_basket_service.py:303-314`）
   不清理 `source_type='note'/'evidence'` 行 → `search_knowledge`（`database.py:2989-3017`）
   会报出指向已删对象的锚点；`foreign_key_check` 无法发现。v0.7 记忆写入会放大。
5. **R-05 [P2] AI 审计/预算边界靠约定维持** — `AuditedAIProvider.wrapped` 是公开属性
   （`provider.py:381`），`QwenProvider` 可自由构造，`scripts/ai_smoke_test.py:100` 等
   已有裸构造先例（绕过账本/预算）。当前生产组合根全部经审计包装
   （`runtime.py:151-158`、`hosted/ai_runtime.py:63-70`；决策与最终答案调用分别带
   `source_feature=agent_decision/agent_final_answer`），但写能力（v0.7）到来前必须把
   绕过面收紧，否则只有约定在防守。
6. **R-06 [P2] Agent 错误分类依赖中文子串** — `final_answer.py:150-185` 用
   `"引用校验失败"`/`"空上下文"`/`"预算"` 匹配异常消息决定错误码；预算守卫消息在
   `runtime.py:192-193` 与 `hosted/ai_runtime.py:38-45`。改文案 → 预算错误静默降级为
   `PROVIDER_UNAVAILABLE`。已在异常上携带类型化字段的路上，改造成本低。
7. **R-07 [P2] 发布门禁不变量缺口** — pytest 子进程继承真实 `.env`（AI-off 不变量未强制）；
   容器测试 `RUN_HOSTED_CONTAINER_TESTS=1` opt-in，门禁不设置也不断言；无 secret/依赖扫描。
8. **R-08 [P2] 依赖声明与环境漂移** — `requirements.txt:18` 声明 `httpx2`，
   实际安装 httpx 0.28.1 → 收集期出现 `StarletteDeprecationWarning`；
   冻结文档的 "0 warnings" 记录（`v0.6.1-competition-demo-freeze.md:144` 等 3 处）对未来重跑已不成立。
   另：`rapidfuzz` 声明但零导入（requirements.txt:12）；pytest/ruff 混入运行时依赖文件。
9. **R-09 [P2] 备份并发写无测试** — 备份实现本身稳健（SQLite backup API + 全量校验，
   `backup_service.py:750-766`），但没有任何测试在"写事务进行中"执行 `create_backup`。
10. **R-10 [P2] pages/4 无守护初始化** — `pages/4_检索资料.py:81` 页面导入即调
    `application_coverage_service().coverage_summary()`，早于守护初始化块（`:458-475`）；
    DB 缺损时用户看到原始异常屏，而其他页面均 try/except+`st.stop()`（`app.py:22-34`、`pages/3:57-68`）。

## Risk Register

严重度定义：P0=正确性/安全/数据安全缺陷（本审计为 0）；P1=会在下一步造成实际损失；
P2=明确风险，有具体触发路径；P3=局部债务；OBSERVATION=记录在案不行动。

| ID | 标题 | 领域 | 严重度 | 可能性 | 影响 | 证据 | 现有缓解 | 建议动作 | 建议里程碑 | 需解冻 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| R-01 | 发布门禁与冻结演示页互斥 | 发布 | P1 | 高（重跑即触发） | 中 | `release_check.py:449-467,957`；`pages/0_知识Agent.py:603,621` | 冻结后未重跑门禁 | 赛后更新门禁对 demo 页的版本检查 | 赛后/v0.7 前 | 否 |
| R-02 | 测试无环境隔离；真实 .env 被吸入 | 测试 | P1 | 高 | 中 | `tests/conftest.py:16-27`；`test_navigation_ui.py:37`、`test_v008_ui.py:25`、`test_release_check.py:137`；`config.py:54-57` | 路径均 tmp 覆盖；无打印 | 为 3 处补 `_env_file=None` 或加 autouse env 消毒 fixture | v0.7 前 | 否 |
| R-03 | roadmap 活文档与发布现实矛盾 | 文档 | P1 | 已发生 | 低 | `docs/v0.5.x-roadmap.md:21,42` vs `README.md:336` | 无 | 更新两行状态 | v0.7 前（docs-only） | 否 |
| R-04 | 多态来源孤儿链接 | 存储 | P2 | 中 | 中 | `note_service.py:631-649`；`evidence_basket_service.py:303-314`；`migrations.py:905-907` | 文档级删除有清理+残留校验 | note/evidence 删除路径补清理（事务内） | v0.7 前 | 否 |
| R-05 | AI 审计/预算可被绕过（治理） | AI | P2 | 低（恶意）/中（顺手） | 中 | `provider.py:381`；`scripts/ai_smoke_test.py:100` 等 | 生产组合根全部经 AuditedAIProvider | 收紧 wrapped 可见性或加能力门 | v0.7 前 | 否 |
| R-06 | Agent 错误分类靠中文子串 | Agent | P2 | 中 | 中 | `final_answer.py:150-185`；`runtime.py:192`；`hosted/ai_runtime.py:38` | 测试钉住当前文案 | 异常携带类型化字段，移除子串匹配 | v0.7 前 | 否 |
| R-07 | 发布门禁不变量缺口（AI-off/容器/secret） | 发布 | P2 | 中 | 中 | `release_check.py` 全量清单（tests 报告 §C8） | pytest 套件内哨兵覆盖 hosted/demo | 门禁强制 manual 模式跑 pytest；加 secret 扫描 | v0.7 | 否 |
| R-08 | 依赖声明与环境漂移（httpx2/httpx 等） | 依赖 | P2 | 已发生 | 低 | `requirements.txt:9-19` vs 安装环境；3 处 "0 warnings" 文档记录 | hosted 文件有闭包测试 | 对齐 requirements 与 venv；拆 dev 依赖 | v0.7 前 | 否 |
| R-09 | 备份并发写无测试 | 存储/测试 | P2 | 低 | 中 | tests 报告 §A4（缺失项） | 备份用 backup API 天然一致 | 补一条"写事务进行中 create_backup"测试 | v0.7 | 否 |
| R-10 | pages/4 无守护初始化 | UI | P2 | 低 | 中 | `pages/4_检索资料.py:81` vs `:458-475` | 无（他页均有守护） | 移入守护块 | 赛后 | 否 |
| R-11 | Agent 检索 lexical-only，弱于交互式 hybrid | RAG/Agent | P3 | 高（已存在） | 低-中 | `adapters/page_search.py:85` vs `runtime.py:208-236` | 文档声明为有意边界 | v0.7 评估把 hybrid 接入 tool 或明确接受 | v0.7 | 否 |
| R-12 | PDF 导入多事务，崩溃留半成品 | 导入 | P3 | 低 | 低 | `document_service.py:324-409` | 可续传 + import_records | 保持现状或补恢复 UI 提示 | v0.8 | 否 |
| R-13 | create_tag 竞态未映射 IntegrityError | 存储 | P3 | 低 | 低 | `database.py:838-851` vs `:925-926` | UNIQUE 兜底 | 对齐 create_project 的映射模式 | v0.8 | 否 |
| R-14 | 写路径无重试/退避；长事务可阻塞 30s | 存储/UI | P3 | 低 | 低 | `database.py:130-133`；`document_deletion_service.py:357-487` | WAL 读写不互斥 | 保持现状（单用户） | 接受 | 否 |
| R-15 | legacy 升级 CLI 复制未验证关闭的 WAL sidecar | 备份 | P3 | 低 | 中 | `legacy_backup_upgrade_service.py:89-94` | 拒绝生产 DB 固定路径 | 增加源关闭/只读校验 | v0.8 | 否 |
| R-16 | ToolResultContextMapper 宽容解析+合成状态+anchor_id==0 | Agent | P3 | 中 | 低 | `tool_context.py:129-131,273-274,238-318` | 上游 adapter 已校验形状 | 修复 0 值 bug；收紧解析 | v0.7 | 否 |
| R-17 | 工具 timeout_seconds 声明未执行 | Agent | P3 | 低 | 低 | 工具定义 vs `executor.py:218` | 每步架构天然有界 | 声明删除或接线 deadline | v0.8 | 否 |
| R-18 | 重复助手：审计脚手架/指纹警告x4/_iso_or_none x5/连接策略x3 | 可维护性 | P3 | 中（漂移） | 低 | `provider.py:394-520`；`knowledge_read.py:174-188` 等；`evidence_basket_service.py:539-560` | 测试覆盖各自路径 | v0.7 动到对应区域时顺手合并 | v0.8 | 否 |
| R-19 | production 传输层依赖 demo 包（DemoHTTPError） | Demo 边界 | P3 | 低 | 低 | `agent_client.py:12-14,28` | 冻结清单声明有意 | 冻结解除后更名/搬移 | v0.8 | 否 |
| R-20 | 演示/真实链路文案常量重复无交叉测试 | Demo | P3 | 中 | 低 | `fixtures.py:35,38` vs `hosted_api/contracts.py:103`、`final_answer.py:43` | catalog 内部一致性校验 | 加跨模块常量比较测试（不动冻结文案） | v0.7 后 | 否 |
| R-21 | 版本字面量散布（pages/4 的 "0.5.3"；门禁硬编码） | 配置 | P3 | 已发生 | 低 | `pages/4_检索资料.py:371,1300`；`release_check.py:696-698` | 无 | 换成 settings.app_version 引用 | v0.7 前 | 否 |
| R-22 | Mode 1 Hosted 服务无托管启动路径 | 服务管理 | P3 | 高（默认即如此） | 低 | `agent_client.py:39`；service_manager 无 hosted 命令 | UI 明示"服务不可用" | service_manager 增加 hosted 子命令（LOCAL_ONLY） | v0.8 | 否 |

OBSERVATION（不单列行动）：database.py god-module（五代 schema 同类，内部纪律好）；
FTS 影子列无运行时漂移检测；迁移驱动 `current_version` 在 v4-v9 后不更新、WAL 在迁移失败后仍持久
（`migrations.py:76,96-107`）；CI 完全缺失；Hosted 环境变量未进 README/.env.example；
`qwen_client.py:334` 把传输异常文本插进用户消息（当前构造上安全）；UI 在 provider 构造失败时
静默回退 Mock（`rag_answer_ui.py:71-81`，输出有标注）；`DECISION_PROVIDER_FAILED` 吞掉
manual 模式与真实故障的区别（hosted readiness 门缓解）；`#N` 引用正则对偶发 `#12` 严格拒绝
（有意）；容器镜像内惰性 `PIL` 导入缺 Pillow（当前不可达，`evidence_basket_service.py:929`）；
死代码候选 `scripts/phase3_embedding_calibration.py`（HIGH 信心）、`experiments/` 迁移候选（LOW）。

## Architecture Boundaries（依赖方向审计结论）

- **干净（有测试强制）**：Agent 模块不 import UI/provider 具体实现
  （`tests/test_agent_execution.py:757`）；hosted 镜像不含写类服务
  （`test_hosted_api.py:476`）；demo 包禁导入清单（`test_demo_contract.py:52-63`）；
  Hosted 配置禁 dotenv（`hosted_config.py:79-83`）。
- **三处轻微反向（均为有意/低危）**：`agent_client → demo.contracts`（R-19）；
  `AILedgerService → Database._connection`（私有连接，noqa）；`demo → hosted readiness`
  传递 import（无副作用）。
- **无循环概念依赖**：UI→runtime→services→database 单向；hosted 与 local 仅共享
  contracts/services；demo 单向继承生产 DTO（`DemoAgentRunResponse(AgentRunResponse)`，
  `demo/contracts.py:70-74`）。

## RAG vs Agent

**复用正确，边界清晰，一处有意分叉。**

- **引用：一套实现。** `rag_answer_service._validate_answer_citations`
  （`rag_answer_service.py:161-200`）同时服务 RAG 与 Agent（FinalAnswerStage 委托，
  `final_answer.py:140-147`）；`#N` 编号只在 `KnowledgeContextPackager.build` 一处铸造
  （`knowledge_context_packager.py:261-263`）。不存在两套引用系统。
- **上下文表示：共享主干。** 两路都汇入 `ContextItem → KnowledgeContextPackager →
  KnowledgeContextPackage.to_markdown() → RagPromptBuilder`（共用 `GROUNDING_RULES`）。
  分叉在投影层：RAG 用 `ContextItemProjector`（真实 DB 状态），Agent 用
  `ToolResultContextMapper`（从 ToolResult 字典投影，合成 `status="active"`、
  `updated_at=None`，宽容解析，R-16）。未来若两条链各自演化投影规则，才会出现
  "两个 AI 产品"——目前被共享 packager 挡住。
- **检索：真实分叉（R-11）。** 交互页可走 hybrid（lexical+vector RRF），
  Agent 的 `page_search` 只用 lexical `SearchService`。这是声明过的边界
  （adapter docstring），但对"Agent 回答质量不应低于搜索页"的期望是隐患，v0.7 应显式决策。
- **答案生成规则**：共用 grounding 规则，RAG 追加规则 6-9；手动 prompt_builder 三兄弟
  （`prompt_builder/evidence_prompt_builder/knowledge_prompt_builder`）是"手动 AI 模式"
  的平行路径，属模式重复而非代码重复，可接受。

## Local vs Hosted

**隔离由代码与测试双面强制，DEFERRED 状态与现实一致。**

- Profile 显式：`EKB_RUNTIME_PROFILE` 仅"缺席"默认 LOCAL；空/未知值 FAIL CLOSED
  （`runtime_profile.py:38-60`；ADR-007 §11）。每个入口（`config.py:135`、
  `hosted_config.py:269`、`hosted_api/app.py:97` 等）先过 `require_runtime_profile`。
- 数据根互斥：Hosted `data_root` 解析后若在源码树内即拒绝（`hosted_config.py:231-247`）；
  Local `get_settings()` 强制 127.0.0.1:8501（`config.py:16-17,62`，Literal 类型级+守卫+
  config.toml+service_manager 四重）。生产路径不可能被测试使用（测试全部 tmp_path）。
- Hosted 只读：单进程/单 worker 硬约束（`hosted/storage.py:34-48`，`WEB_CONCURRENCY`
  校验 `server.py:26-31`）；种子 DB 三重校验（sha256+schema v12+sanitization，
  `storage_validation.py:272-327`）；运行期 readiness 反复校验 inode/WAL/kb_uuid。
- 未发现"Hosted 触 Local 数据"或"Local 设置泄入 Hosted"路径。
  已知声明的两个 UI 集成缺口（权威 `#N` 映射、per-source integrity）位置与文档一致
  （`hosted_api/contracts.py:93-106` 缺字段；`demo/contracts.py:59-81` demo 侧富化），
  冻结清单如实记录，不需在 v0.6.1 修。

## Demo vs Production

**机械强制隔离，反向冒充双双被阻断。**

- demo 包零生产 DB/AI/网络依赖（import 扫描 `test_demo_contract.py:385-392` +
  socket/urlopen 哨兵）；fixtures 唯一来源是 Python 代码，JSON 只是受测试同步的快照
  （`test_demo_contract.py:404-423`）。
- 模式选择仅 UI radio，默认 Mode 2 mock（`pages/0_知识Agent.py:612-638`）；
  `DemoAgentRunResponse.mode: Literal["mock_demo"]` 类型级强制 + 真实 DTO
  `extra="forbid"`（`hosted_api/contracts.py:22`）→ mock 不能冒充真实，真实也不会被标成演示。
- 完整性措辞不泄漏：真实链路无 integrity 字段，Mode 1 显示通用说明
  （`demo_ui.py:281,404-405`，测试保护 `test_demo_ui.py:282-290`）。
- 未来风险：开发者可能对着 demo-only 字段（`citations_detail`、`integrity_state`、
  `demo_note`）实现——由 DTO superset 测试 + 冻结清单"需后端评审"条款缓解；
  R-20（文案常量重复）是同一族的小风险。

## Storage / Migration / Backup

**这是全仓最强的部分。**

- 迁移：v1→v12 顺序硬编码链；每个 `_apply_version_N` 自带事务 + 版本行提交；
  v4 起数据指纹前后校验；迁移前自动经 SQLite backup API 备份并验证；失败 = 回滚到
  最后版本边界 + 保留备份 + `MigrationError` 上抛、启动中止（`app.py:31-34`）。
  失败注入钩子生产惰性；回滚有测试（`test_database.py:354-466`）。
  v13 可安全加入：机制已验证，注意影子列分词器是 FTS 索引的单点真相
  （`database.py:3083-3089`，改分词必须配重建迁移）。
- FTS：三张 external-content FTS5 表 + 9 触发器；jieba 预分词进影子列；
  所有运行时写路径成对更新（逐条核对无漂移）；查询参数化，非法 MATCH 降级空结果。
- 并发：单进程多线程，短连接；`busy_timeout=30000`；写-写冲突靠 SQLite 串行化 +
  `BEGIN IMMEDIATE`（knowledge/basket/batch 三处关键 check-then-insert 已覆盖；
  `create_tag` 例外，R-13）。
- 备份/恢复：备份 = backup API 快照 + 完整性/FK/版本校验 + 资产链接核验 + 原子发布 +
  staging 全量验证（`backup_service.py:173-265`）；恢复 = 双重验证 + 路径 rebase +
  预恢复备份 + 换位 + 换位后复核 + 自动回滚（`:267-381`）。对当前单用户模型绰绰有余；
  对 v0.7 可写 Agent 依然够用（可写频率低，且迁移前备份已并入机制）。
- Windows：symlink/junction/reparse 拒绝贯穿备份/删除/恢复；打开文件使删除中止回滚；
  恢复拒绝在服务运行时执行。R-15 是唯一残留（legacy CLI 的 WAL sidecar）。

## AI / Budget / Audit

- 供方抽象：能力级 Protocol（Completion/Embedding/Rerank）+ 冻结 DTO + 两类错误
  （`AIUnavailableError`=不可用 vs `AIExecutionError`=已尝试且失败，带 error_class）。
  DashScope 细节封闭在 `qwen_client.py`；仅两个组合根 import 之。
- 审计：`AuditedAIProvider` 记 hash-only 账本（无正文、无 key；错误摘要先过
  bearer/sk- 消毒 `ai_ledger_service.py:43-47,328-339`）；预算 preflight 在网络调用前
  （拒绝也记账 `status=rejected`）。账本写失败不影响业务调用。
- 缺口：R-05（结构上可绕过）；R-06（错误分类子串）；预算为非原子 preflight
  （并发可小幅超限，hosted docstring 已承认；单用户低危）；v0.7 多调用者出现后
  `complete/embed` 55 行重复审计脚手架应合并（R-18）。
- 结论：审计/预算架构在"更多 Agent 调用"出现时保持自洽——前提是 R-05 先收紧。

## Agent Architecture

- 单步：1 decision + 0/1 tool + 1 final answer，≤2 次模型调用（测试硬上限
  `test_agent_final_answer.py:608-624`）；autonomous retry = 0（`executor.py:383`；
  `retryable` 仅是元数据）；决策解析严格（无代码栅栏剥离、重复键拒绝、NaN 拒绝、
  字段集精确）；失败矩阵全覆盖且原始异常不出栈（detail 只带类型名）。
- 七工具全部只读（registry `Phase1ReadOnlyPolicy` 注册期拒绝非 READ_ONLY；
  hosted 组合根断言 7 工具集合）；adapter 参数硬化（未知参数拒绝、长度界、
  stable_id 严格解析）。未发现经只读 adapter 可达的隐藏写调用。
- 最终答案：grounded 仅在"模型回答+引用校验通过"时为真；无证据=确定性中文空态、
  0 模型调用；重复引用去重、伪造引用整体拒绝、0 引用拒绝。
- 已知债：R-06、R-11、R-16、R-17。

## Hosted API

- 面：`POST /v0.6/agent/run`（业务失败=200+结构化 failed，不泄内部细节）、
  `GET /v0.6/sources/{stable_id}`（仅 stable_id/type/title/label；`safe_display_text`
   拒绝路径形/控制字符/超长）、`/health`、`/ready`（503 封闭词表）。
- 门：四重边界齐备——rate（10/60 per min/IP，桶上限 1 万 fail closed）、
  并发（信号量 4，第 5 个 429）、体积（512,000 bytes 实际 ASGI 层）、预算（hosted 禁 0=unlimited）。
  XFF 仅在可信代理网段后采纳（右→左首个非可信跳）；CORS 精确 allowlist 无凭据；
  OpenAPI 全关；DTO `extra=forbid, frozen, hide_input_in_errors`；422 不序列化 pydantic 错误。
- 契约稳定：错误码封闭目录 + `_STATUS_FALLBACK_CODES`；两个已知缺口（#N 权威映射、
  per-source integrity）即前述。无需 v0.6.1 修复。

## Security / Privacy

- 本地：唯一网络面 = 127.0.0.1:8501（四重强制）；动态 HTML 全部经 `html.escape(quote=True)`
  （17 处调用点核对 + `text_utils.highlight_html` 只注字面 `<mark>`）；
  日志 UTF-8 轮转、不含 prompt/key（`qwen_client` 零日志调用）。
- Hosted：封闭词表日志（formatter 只放行枚举事件，不 format args/traceback）；
  key 仅 SecretStr→Authorization 头；`.dockerignore` deny-by-default + 再排除
  （`.env*/data/backups/logs/**.db/**.pdf` 等，顺序有测试）；非 root 容器；umask 077。
- 隐私分级：**今日安全**——本地数据不出机器、审计 hash-only、演示语料 synthetic
  （demo UUID 与生产 KB UUID 不碰撞，实测校验）；**若公网恢复**——路径披露面已封
  （safe_display_text/closed errors），但需重审 rate 阈值与 corpus 授权（WP5 手动门）；
  **若多用户出现**——单 SQLite、无 identity、无隔离，需重新立项（当前边界明确拒绝）。
- 唯一新发现：R-02 的真实 key 进测试进程内存（本地单用户下无泄漏路径，仍是卫生问题）。

## Test Effectiveness

2685 passed + 4 skipped 与文档一致（collect = 2689）。**质量总体高于数量印象**：
fail-closed 行为有对抗测试（伪造引用、预算拒绝不打网络、调用计数不变量）、
迁移失败回滚有专测、删除隔离区 44 测试、hosted 门禁语义测试齐全。

- 金字塔：单元/服务+真 SQLite tmp ~1570；Agent 273；Hosted+Demo ~235；
  AppTest ~402（121 次 from_file）；脚本守卫 197；端到端发布门禁 0（手动脚本）。
- 缺口（高价值）：R-09（备份并发写）、容器层 opt-in 且被一切"全量回归"数字排除、
  FTS 运行期漂移无检测、R-02（隔离）。
- 弱断言样例：`test_navigation_ui.py:174-208`（15+ 全文案断言）、
  `test_demo_ui.py:86-96`（冻结文案钉死——对冻结是特性，对回归是盲区）、
  `test_document_service.py:88-127`（Fake 复刻生产分支）、
  过时测试名 `test_current_application_version_is_v050`（`test_config.py:19`）。
- 脆弱候选：`test_service_manager_staging.py:37-44`（真实 sleep(60) 子进程）、
  `test_evidence_basket_service.py:274-317`（Barrier 未全路径处理）、
  简单 `datetime.now()` 两处。无顺序依赖（cache_clear 纪律良好）。

## Configuration / Version Drift

**五层版本语义现行事实**（建议以此为准写进维护文档）：

| 层 | 值 | 真相源 |
| --- | --- | --- |
| 发布语义版本 | 0.6.0（RELEASED，tag v0.6.0） | git tag + `config.py:61` + CHANGELOG |
| 开发里程碑 | v0.6.1 Competition Demo（进行中，未发布） | 冻结清单 + `demo/contracts.py:41 DEMO_VERSION` |
| app_version | "0.6.0"（v0.6.1 期间有意不动） | `config.py:60-61`；`release_check.py:43 EXPECTED_VERSION` |
| API 路径版本 | `/v0.6` | `hosted_api/app.py:155,195` |
| schema 版本 | v12 | `migrations.py:13 SCHEMA_VERSION` |

发现的不一致：R-01（门禁 vs demo 页）、`release_check.py:957` 描述 "v0.5.3"、
`release_check.py:696-698` 硬编码 `v0.6.0` 字面量（与 EXPECTED_VERSION 重复）、
`pages/4:371,1300` 硬编码 `app_version="0.5.3"`（传给 KnowledgeContextPackager 的
元数据，匹配任何已声明版本）、`test_config.py:19` 测试名过时。
治理建议：app_version 是唯一发布真相；demo 层版本只存在于 demo contracts；
禁止文档发明 config 里不存在的版本号；发布时先改 `EXPECTED_VERSION` 一处，
字面量引用全部改为 `settings.app_version`。

## Documentation Drift

- **活文档需修**：`v0.5.x-roadmap.md:21,42`（R-03）。
- **历史冻结文档中的单行失真（不改文档，记录在案）**：
  `v0.6.0-release-closure-inventory.md:95` "release_check 仍以 v0.5.3 为基线"已反向失真
  （代码已 0.6.0）；冻结清单/final-audit/handoff 三处 "0 warnings" 对未来重跑不成立（R-08）。
- **一致（正面）**：WP6A PARTIAL/PAUSED、公网 DEFERRED、Mode 1/Mode 2 语义、
  2685/4 测试计数、`127.0.0.1:8501` 在 README/README_EN/CHANGELOG/冻结清单四处一致；
  README 声称 "PowerShell" 但启动器实际全为 cmd+Python（微小失真）；
  Hosted 环境变量只记录在 WP 文档与 Dockerfile，README/.env.example 缺席（OBSERVATION）。

## Dependency Health

- `requirements-hosted.txt`：17 项全精确 pin，且有"精确闭包"测试
  （`test_hosted_packaging.py:68-99`）+ Dockerfile 只装它 → 漂移不可能静默影响镜像。
- `requirements.txt`：测试工具（pytest/httpx2/ruff）混入运行时文件、`rapidfuzz` 零导入、
  `httpx2` 未 pin 且与实际环境不符（R-08）。fastapi/uvicorn 对 Local 进程非必需
  （一致性取舍，接受）。
- 无未声明依赖（Docker 内第三方 import 仅 jieba + fastapi 栈，均声明）；
  容器内惰性 `PIL` 导入为潜在坑（当前不可达）。
- 未做任何依赖升级（遵审计边界）。

## Release / CI

- 门禁覆盖面广（版本一致性/端点/端口隔离/ruff/全量 pytest/DB 完整性/schema 不变量/
  README 双语 parity/导出格式常量/写探针/备份创建），但：R-01（与冻结树互斥）、
  R-07（AI-off 不强制、容器 opt-in、无 secret 扫描）、zero-skip PASS 要求与 Windows
  symlink skip 冲突（本机最高只能 WARNING）。
- CI：**不存在**（无 .github/、tox、pre-commit）。单机发布流程是维护者的有意选择，
  但这是 v0.7 前最大的过程风险——所有不变量都依赖人记得跑脚本。
  是否引入 CI 是维护者决策（本审计不擅自创建）。

## v0.7 Readiness

详见 `docs/v0.7-readiness-audit.md`（17 项前置矩阵）。结论 **CONDITIONAL_READY**：
数据模型/服务层写路径/迁移/备份/安全边界 READY；批准门、写 Tool 契约、
持久化写审计（schema v13）、undo 语义 MISSING（需 ADR，非架构障碍）；
开工前先修 R-04/R-05/R-06 并收紧发布门禁（R-01/R-07）。

## Priority Roadmap

- **v0.7 之前（赛后即可，全部不触冻结面）**：R-01 门禁兼容 demo 页 + 清理 "v0.5.3" 描述；
  R-02 测试 env 隔离；R-03 roadmap 两行；R-04 来源孤儿清理；R-05 审计边界收紧；
  R-06 类型化错误分类；R-08 依赖/环境对齐；R-21 版本字面量治理；R-10 pages/4 守护。
- **v0.7 期间**：写能力 ADR（批准门/幂等/undo/审计字段）；schema v13；R-07 门禁强化；
  R-09 备份并发测试；R-11 Agent 检索分层决策；R-16 mapper 收紧。
- **推迟到 v0.8**：R-12 导入原子性、R-13/R-15 存储卫生、R-17 超时接线、R-18 助手合并、
  R-19 DemoHTTPError 搬移、R-22 hosted 托管启动、UI 阻塞/大渲染治理、死代码清理。
- **明确不做 / 接受**：单步架构上限（ADR 决策）、`#N` 正则严格性（有意 fail-closed）、
  UI Mock 回退（有标注）、busy_timeout 即全部写重试（单用户）、database.py 体积
  （v0.7 动到时顺势拆分，不为拆而拆）、Windows-only service_manager（LOCAL_ONLY 设计）。

## Accepted Tradeoffs（明确接受的取舍，勿再"修复"）

1. 演示 fixtures 与真实链路的文案常量在冻结期内重复（R-20）——冻结优先，赛后加比较测试。
2. `AuditedAIProvider.wrapped` 公开（R-05）在"脚本需要做付费探针"的现实下是便利性取舍
   ——v0.7 写能力前必须重审，但现在不动。
3. hosted API 业务失败返回 200 + 结构化 failed（而非 4xx/5xx）——降低枚举攻击面，
   已测试钉住。
4. 容器测试 opt-in——避免无 Docker 环境的全量回归不可跑；代价是"全量数字"不含容器层。
5. Streamlit 单进程承载 demo 页与管理页——比赛冻结的结构；演示页零 DB 调用已实测。
6. 无 CI——单机单人发布的现实；以 release_check + 冻结清单补偿。

## Final Recommendation

以 **HEALTHY_WITH_DEBT** 收官。比赛演示保持冻结，本审计不构成任何解冻理由。
赛后第一周做三件小事：修 R-01（门禁）+ R-02（测试隔离）+ R-03（roadmap 两行），
让流程重新可信；v0.7 立项时先写写能力 ADR 并修 R-04/R-05/R-06。
不需要大重构：god-module 拆分只在 v0.7 写路径实际触碰 knowledge 区域时顺势进行。

---

## Remediation Status Addendum（2026-08-29 整改包，保留原发现不改写）

| 发现 | 状态 | 整改内容 |
| --- | --- | --- |
| R-01 / TD-02（发布门禁与冻结树互斥） | RESOLVED | `scripts/release_check.py` 引入 `EXPECTED_VERSION`（0.6.0 发布版）与 `ACTIVE_MILESTONE_VERSION`（0.6.1 里程碑）双维度策略；`MILESTONE_PAGES={"0_知识Agent.py"}` 白名单页必须含里程碑版本行、免于标题含发布版要求；新增页面 `app_version="..."` 字面量防回退规则；README parity/parser 描述改引用 `EXPECTED_VERSION`。冻结页面本身零改动。锁定测试 8 项，含真实冻结树整树 PASS。 |
| R-02 / TD-01（测试无环境隔离） | RESOLVED | `tests/conftest.py` 新增套件级 autouse `_isolate_developer_environment`：清除 `EKB_*`/`DASHSCOPE_*` 环境变量 + 中和 `Settings` 隐式 dotenv 源；opt-in 通道显式保留（`monkeypatch.setenv` / `Settings(_env_file=...)`）；3 处直接构造点补 `_env_file=None`。回归锁：`tests/test_test_environment_isolation.py`（含 secret-safety 断言：真实 `.env` 有密钥时普通测试仍为 manual + 空 key，证明隔离不可能意外启用真实 AI）。 |
| R-03 / TD-03（roadmap 活文档漂移） | RESOLVED | `docs/v0.5.x-roadmap.md` 两行状态更新：v0.5.x → 已完成（CLOSED）；v0.6.x → v0.6.0 RELEASED/CLOSED + v0.6.1 进行中（演示冻结）。v0.7+ "尚未开始" 保持不变（仍然真实）。 |
| TD-08（版本字面量治理，顺带） | RESOLVED | `pages/4_检索资料.py` 两处 `app_version="0.5.3"` → `src.__version__`；门禁 `app_version=` 字面量规则防回退。 |
| TD-09（pages/4 无守护初始化，顺带） | RESOLVED | 覆盖率初始化移入 try/except + 中文错误 + `st.stop()`；AppTest 故障注入测试锁定。 |
| 追加：README_EN `schema v12` 能力词漂移（门禁运行暴露） | RESOLVED | 官方 `release_check.py --expect-service-stopped` 全量运行（20/21 PASS、2704 passed/0 skipped、Version consistency PASS）暴露唯一 FAIL：`README_EN.md:106` 为 `Schema v12`（大小写漂移，中文版为小写），parity 检查要求字面 `schema v12`。已对齐为小写，`readme_parity_check` 单项复验 PASS。该漂移先于本包存在（门禁自 v0.6.0 收口后未再运行）。 |

边界确认：比赛冻结面（`pages/0_知识Agent.py` 语义、`src/demo/**`、`src/demo_ui.py`、
Mode 1/Mode 2 行为、A/B/C 场景、引用与完整性措辞）零改动；全局 `app_version` 仍为
0.6.0；Hosted API 仍为 `/v0.6`；schema 仍为 v12；公网部署仍 DEFERRED；真实 AI 调用 0。
R-04 及其余 P2/P3 项未在本包范围内，见技术债清单。
