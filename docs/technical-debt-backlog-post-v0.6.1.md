# Technical Debt Backlog — post-v0.6.1

- **日期：** 2026-08-29　**基线：** `main` @ `7e7a293`（v0.6.1 演示冻结树）
- **来源：** `docs/repository-architecture-risk-audit-2026-08-29.md`（总审计）的风险登记册。
  本清单只收录**有具体触发路径或明确下游需求的项**；纯观察项只在总审计记录，不转化为债务。
- **总原则：** 比赛冻结期内不动任何冻结面（`pages/0_知识Agent.py`、`src/demo_ui.py`、
  `src/demo/**`、`src/agent_client.py` 语义、`src/hosted_api/contracts.py`、A/B/C 场景语义）。
  下表所有项均可**在不解冻的前提下**启动，除了标注"依赖冻结解除"的 TD-13。
- **规模：** XS≈半小时 / S≈半天 / M≈1-3 天 / L≈1-2 周 / XL≈更大。

| ID | 标题 | 严重度 | 目标版本 | 规模 |
| --- | --- | --- | --- | --- |
| TD-01 | 测试环境隔离：真实 .env 不得进入测试进程 | P1 | v0.7 前 | S |
| TD-02 | 发布门禁与冻结演示页互斥修复 | P1 | v0.7 前（赛后） | S |
| TD-03 | roadmap 活文档状态修正 | P1 | v0.7 前 | XS |
| TD-04 | 多态来源孤儿链接清理 | P2 | v0.7 前 | S |
| TD-05 | AI 审计/预算边界收紧（防绕过） | P2 | v0.7 前 | S |
| TD-06 | Agent 错误分类去子串化 | P2 | v0.7 前 | S |
| TD-07 | 依赖声明与环境对齐 | P2 | v0.7 前 | S |
| TD-08 | 版本字面量治理 | P3 | v0.7 前 | S |
| TD-09 | pages/4 无守护初始化修复 | P2 | v0.7 前（赛后） | XS |
| TD-10 | 发布门禁不变量补强（AI-off/容器/secret） | P2 | v0.7 | M |
| TD-11 | 备份并发写测试 | P2 | v0.7 | S |
| TD-12 | Agent 检索分层决策（lexical vs hybrid） | P3 | v0.7 | M |
| TD-13 | ToolResultContextMapper 收紧 + anchor_id==0 | P3 | v0.7 | S |
| TD-14 | schema v13 + 写审计落地 | P2 | v0.7 | L |
| TD-15 | 写能力 ADR + 批准门 + undo 语义 | P2 | v0.7 | L |
| TD-16 | 助手函数合并（指纹警告x4 / _iso_or_none x5 / 审计脚手架 / 连接策略x3） | P3 | v0.8 | M |
| TD-17 | 存储卫生：create_tag 映射 + legacy WAL 守卫 | P3 | v0.8 | S |
| TD-18 | 导入管线原子性/恢复提示 | P3 | v0.8 | M |
| TD-19 | 工具超时：删除装饰性声明或接线 deadline | P3 | v0.8 | S |
| TD-20 | Mode 1 Hosted 服务托管启动 | P3 | v0.8 | S |
| TD-21 | UI 阻塞与大渲染治理 | P3 | v0.8 | M |
| TD-22 | 死代码清理窗口 | P3 | v0.8 | XS |
| TD-23 | CI 引入决策 | P2 | 维护者决策 | M |

---

## 明细

### TD-01 测试环境隔离：真实 .env 不得进入测试进程
- **Evidence:** `tests/conftest.py:16-27`（仅 1 个 fixture，无 env 消毒）；
  `tests/test_navigation_ui.py:37`、`tests/test_v008_ui.py:25`、`tests/test_release_check.py:137`
  构造 `Settings(...)` 未传 `_env_file=None`；`src/config.py:54-57` 指向 `PROJECT_ROOT/.env`；
  本机 `.env` 含 `EKB_AI_MODE` 与真实 `EKB_AI_API_KEY`（已被审计实测确认存在）。
- **Why it matters:** 测试结果依赖机器环境；真实凭据进入测试进程内存；
  与 AGENTS.md"密钥不得泄漏进测试快照"的精神不一致；未来任何断言/日志改动都可能造成首次真实泄漏。
- **Suggested owner type:** 维护者 + 编码代理
- **Dependency:** 无（可立即做，不触冻结面）
- **Do-not-do notes:** 不要为此改动 `Settings.model_config` 的生产默认值；
  不要在测试里打印/断言 key 内容；优先补 `_env_file=None` + 一个 autouse 的 env 清毒 fixture
  （参照 `tests/test_hosted_api.py:44-60` 的既有模式），并同步修正过时测试名
  `test_current_application_version_is_v050`（`tests/test_config.py:19`）。

### TD-02 发布门禁与冻结演示页互斥修复
- **Evidence:** `scripts/release_check.py:449-467` 要求每个 `pages/*.py` 的
  `page_title=`/`st.title(` 行含 `v0.6.0`；`pages/0_知识Agent.py:603` 页面标题无版本号，
  v0.6.1 版本行经 HTML div 渲染（`:621`）不在过滤器视野内 → 现在重跑门禁必 FAIL；
  `release_check.py:957` 描述仍是 "工程知识库 v0.5.3 统一发布检查"。
- **Why it matters:** 门禁是唯一发布权威；它若在冻结树上静默失败，会诱使维护者绕过门禁发布 v0.6.1。
- **Suggested owner type:** 维护者 + 编码代理
- **Dependency:** 比赛演示冻结解除后执行（改的是 scripts/，不是冻结面本身，但时机放在赛后最稳妥）
- **Do-not-do notes:** 不要为了让门禁通过而给冻结页面加版本号（那才是解冻）；
  正确方向是让门禁理解 demo 页的版本行位置（如把 `PAGE_VERSION_LINE` 纳入扫描或对 demo 页白名单化），
  同时把 `:957` 描述、`:696-698` 硬编码字面量改为引用 `EXPECTED_VERSION`/`settings.app_version`。

### TD-03 roadmap 活文档状态修正
- **Evidence:** `docs/v0.5.x-roadmap.md:21`（v0.5.x 仍标"当前版本线"）、`:42`
  （v0.6.x 标"未来规划；尚未开始，尚未实现"）vs `README.md:336`（v0.6.0 RELEASED/CLOSED）+ CHANGELOG。
- **Why it matters:** 唯一发现的活文档级与现实矛盾；新协作者（比赛队友）会先读 roadmap。
- **Suggested owner type:** 维护者
- **Dependency:** 无
- **Do-not-do notes:** 只改这两行状态；不要重写历史 release notes / gate 记录（历史冻结文档）；
  `v0.6.0-release-closure-inventory.md:95` 的反向失真单行记录在案即可，不改历史文档。

### TD-04 多态来源孤儿链接清理
- **Evidence:** `knowledge_object_sources` 无 FK（`src/migrations.py:905-907`，设计使然）；
  `note_service.delete_note`（`src/note_service.py:631-649`）与
  `evidence_basket_service.remove_item/clear`（`src/evidence_basket_service.py:303-314`）
  删除时不清理 `source_type='note'/'evidence'` 行；文档级删除有完整清理可对照
  （`src/document_deletion_service.py:369-406`）；悬空锚点出现在 `search_knowledge`
  （`src/database.py:2989-3017`），`PRAGMA foreign_key_check` 无法发现。
- **Why it matters:** 知识完整性静默漂移；v0.7 Agent 写记忆会成倍增加此类链接，
  届时再修要处理存量+增量两层。
- **Suggested owner type:** 维护者 + 编码代理
- **Dependency:** 无（demo 路径不经过这些服务；不触冻结面）
- **Do-not-do notes:** 不要给该表补 FK（需 v13 级表重建，超出本项范围）；
  在现有事务边界内补 DELETE + 残留校验（对照文档删除路径的 12 项残留检查模式）；
  补一条"删除 note/evidence 后 search_knowledge 无悬空锚点"的回归测试。

### TD-05 AI 审计/预算边界收紧（防绕过）
- **Evidence:** `AuditedAIProvider.wrapped` 公开属性（`src/ai/provider.py:381`）；
  `QwenProvider` 可自由构造且 `scripts/ai_smoke_test.py:100`、`ai_embedding_experiment.py:138`、
  `ai_real_query_probe.py:322` 等已有裸构造先例（绕过账本/预算做付费探针）。
- **Why it matters:** 目前只有约定在防守"每次真实调用必须过审计/预算"；
  v0.7 引入写能力后，一条绕过路径 = 一次不可审计的副作用调用。
- **Suggested owner type:** 维护者 + 编码代理
- **Dependency:** TD-15（写能力 ADR）之前完成
- **Do-not-do notes:** 不要破坏 scripts 的付费探针工作流（它们是 staging 隔离的合法工具）；
  方向是让"绕过"显式化——例如探针脚本走专门的未审计构造函数并打醒目标记，
  或 `wrapped` 改私有 + 提供受控出口；加一条"生产组合根之外构造裸 QwenProvider 需 lint/测试告警"的守卫测试。

### TD-06 Agent 错误分类去子串化
- **Evidence:** `src/agent/response/final_answer.py:150,162,180-185` 用
  `"引用校验失败"`/`"空上下文"`/`"无来源上下文"`/`"预算"` 子串匹配决定
  `CITATION_INVALID`/空态/`BUDGET_EXCEEDED`；被匹配的消息文本在
  `src/runtime.py:192-193`、`src/hosted/ai_runtime.py:38-45`。
- **Why it matters:** 改一条中文文案 → 预算错误静默降级为 `PROVIDER_UNAVAILABLE`；
  v0.7 错误种类增多后不可维护。
- **Suggested owner type:** 维护者 + 编码代理
- **Dependency:** 无
- **Do-not-do notes:** 不要改变对外错误码语义或用户可见文案（有测试钉住）；
  在 `RagAnswerError`/`AIUnavailableError` 上加类型化字段（如 `reason`），
  子串匹配保留为最后回退；改完跑 `test_agent_final_answer.py` 全组。

### TD-07 依赖声明与环境对齐
- **Evidence:** `requirements.txt:18` 声明 `httpx2`，venv 实装 httpx 0.28.1
  → collect 期出现 `StarletteDeprecationWarning`；冻结文档三处 "0 warnings" 记录
  （`docs/v0.6.1-competition-demo-freeze.md:144`、`final-audit:157`、`handoff:87`）
  对未来重跑不成立；`requirements.txt:12` `rapidfuzz` 零导入（死依赖）；
  pytest/httpx2/ruff 混入运行时依赖文件（无 requirements-dev.txt）。
- **Why it matters:** 声明与环境的差异会让"可复现发布"变成口号；警告计数漂移会污染下一次冻结证据。
- **Suggested owner type:** 维护者 + 编码代理
- **Dependency:** 无
- **Do-not-do notes:** 不要批量升级依赖（审计边界）；先弄清 venv 里 httpx 是谁装进来的
  （TestClient 传输层真实需求是什么版本），要么装声明的 httpx2 要么改声明，二选一对齐；
  拆 dev 依赖只做"移动条目"，不改版本约束；rapidfuzz 移除前确认无隐藏动态导入
  （审计已 grep 过 src/pages/scripts 均无）。

### TD-08 版本字面量治理
- **Evidence:** `pages/4_检索资料.py:371,1300` 硬编码 `app_version="0.5.3"`
  （传入 `KnowledgeContextPackager` 元数据）；`release_check.py:696-698` 硬编码
  `v0.6.0` 与 `:43 EXPECTED_VERSION` 重复；页面标题版本散布
  （`app.py:18-19`、`pages/3:41`、`pages/4:78`、`pages/5:42`、`pages/12:35` 为 v0.6.0，
  `pages/0:35` 为冻结的 v0.6.1）。
- **Why it matters:** Ask-AI 导出包的版本元数据指向不存在的版本；每次发版要改多处字面量。
- **Suggested owner type:** 维护者 + 编码代理
- **Dependency:** 与 TD-02 同批做
- **Do-not-do notes:** `pages/0_知识Agent.py:35` 的 `PAGE_VERSION_LINE="v0.6.1…"` 是冻结清单
  明示的合法例外，不要动；其余页面统一改引 `settings.app_version`。

### TD-09 pages/4 无守护初始化修复
- **Evidence:** `pages/4_检索资料.py:81` 在模块级调用
  `application_coverage_service().coverage_summary()`，早于守护初始化块（`:458-475`）；
  对照 `app.py:22-34`、`pages/3_浏览资料.py:57-68` 均有 try/except+`st.stop()`。
- **Why it matters:** DB 缺损/损坏时检索页给用户原始异常屏，违背"中文错误信息"工程规则。
- **Suggested owner type:** 编码代理
- **Dependency:** 无（页面不在冻结面）
- **Do-not-do notes:** 只把该调用挪进守护块或包 try/except；不要顺手重构该页（1575 行，另立 TD-21）。

### TD-10 发布门禁不变量补强（AI-off / 容器 / secret）
- **Evidence:** `scripts/release_check.py` pytest 子进程继承真实 `.env`
  （AI-off 不变量未强制）；容器测试 `RUN_HOSTED_CONTAINER_TESTS=1` opt-in
  （`tests/test_hosted_packaging.py:151`）且门禁既不设置也不断言；无 secret/依赖扫描阶段；
  zero-skip PASS 要求与 Windows symlink skip 冲突（本机最高 WARNING）。
- **Why it matters:** "2685 passed" 不能证明"0 真实 AI 调用"和"镜像可跑"；
  这是 v0.7（写能力）前必须补的过程不变量。
- **Suggested owner type:** 维护者 + 编码代理
- **Dependency:** TD-01、TD-02 之后
- **Do-not-do notes:** 不要在门禁里引入网络扫描或外部服务依赖；
  AI-off 用"pytest 以 EKB_AI_MODE=manual + 空 key 环境运行 + ai_calls 表计数断言"实现；
  secret 扫描先只扫工作区文件模式（.gitignore 生效性），不引入重型工具。

### TD-11 备份并发写测试
- **Evidence:** 备份实现用 SQLite backup API（`src/backup_service.py:750-766`，理论上一致），
  但无任何测试在写事务进行中执行 `create_backup`；现有最近的是 WAL sidecar 排除测试
  （`tests/test_backup_service.py:152-160`）。
- **Why it matters:** 这是"备份必一致"承诺目前唯一的未验证区；v0.7 写能力提升写频率。
- **Suggested owner type:** 编码代理
- **Dependency:** 无
- **Do-not-do notes:** 测试要点：写事务挂起（BEGIN IMMEDIATE 未提交）时备份应成功且快照
  不含未提交数据、校验通过；不要为通过测试而改备份实现。

### TD-12 Agent 检索分层决策（lexical vs hybrid）
- **Evidence:** `page_search` adapter 只用 lexical `SearchService`
  （`src/agent/tools/adapters/page_search.py:85`）；交互页可用 hybrid RRF+向量
  （`src/runtime.py:208-236`）；hybrid 对 Agent 不可达。
- **Why it matters:** "Agent 回答有依据"的质量下限取决于检索下限；
  用户在搜索页看到的证据可能优于 Agent 所用证据，形成体验落差。
- **Suggested owner type:** 维护者（决策）+ 编码代理（实施）
- **Dependency:** 先做 ADR 式决策（是否让 Agent 工具消费 hybrid），再实施
- **Do-not-do notes:** 不要默认接 vector——AI 关闭时必须保持 0 网络调用、行为与今天一致
  （degrade to lexical-only 已是既有语义）；决策记录进 v0.7 ADR 而非悄悄改 adapter。

### TD-13 ToolResultContextMapper 收紧 + anchor_id==0
- **Evidence:** `src/agent/response/tool_context.py:273-274`
  （`_int_or_none(raw.get("anchor_id")) or _int_or_none(raw.get("source_id"))`
  把合法 `0` 当缺失）；`:129-131,170-177` 合成 `status="active"/"现行"`、`updated_at=None`；
  `:238-318` 双词表宽容解析、畸形条目静默 `continue`。
- **Why it matters:** 与全仓 fail-closed 风格相悖；0 值 bug 是真实错误（id 从 1 起算则暂无实害，
  但契约上 `id>=0`）。
- **Suggested owner type:** 编码代理
- **Dependency:** 建议在 v0.7 动 Agent 响应层时一并做（避免两次触碰）
- **Do-not-do notes:** 不要突然收紧到 raise——先只修 0 值 bug 并加测试；
  状态合成改为显式 `NOT_APPLICABLE` 语义时要同步 `demo` 快照测试预期。

### TD-14 schema v13 + 写审计落地
- **Evidence:** ADR-006 Decision 8/9：`agent_runs/agent_steps/tool_calls` 三表草案已冻结、
  执行推迟；现执行 trace 仅内存（`src/agent/execution/contracts.py:160-166`）；
  `ai_calls` 账本已就绪（`src/database.py:2580-2683`）。
- **Why it matters:** v0.7 写能力的审计前提；迁移机制已验证（v10-v12 模式 + 回滚测试）。
- **Suggested owner type:** 维护者 + 编码代理
- **Dependency:** TD-15（ADR 先行）；TD-04（先清孤儿，避免新表引用悬空对象）
- **Do-not-do notes:** 严格按 v10-v12 既有模式（版本边界事务 + 迁移前备份 + 数据指纹 + 失败注入测试）；
  遵守最小审计数据原则（不存正文/全文列）；不要顺手把别的 schema 愿望塞进 v13。

### TD-15 写能力 ADR + 批准门 + undo 语义
- **Evidence:** ADR-006 Decision 1（写类 Tool 需独立 ADR）；全仓无批准流；
  `ToolSideEffect` 机制在位（`src/agent/tools/registry.py:63-70`）；
  人类写路径已事务化（`src/knowledge_memory_service.py:82-177`）。
- **Why it matters:** v0.7 的核心新设计；AGENTS.md 自动化边界（修改用户笔记需显式确认）是硬约束。
- **Suggested owner type:** 维护者（ADR 作者）
- **Dependency:** TD-05；建议在 TD-14 前
- **Do-not-do notes:** 不要在 ADR 落地前写任何写 Tool 代码；单步架构不动（批准 = run 间两段式，
  不引入循环）；Agent 发起的 delete 必须走人审（AGENTS.md 禁自动删除）。

### TD-16 助手函数合并
- **Evidence:** 指纹状态→中文警告映射 4 处（`knowledge_read.py:174-188`、`provenance.py:139-153`、
  `source_integrity.py:168-183`、`knowledge_context_packager.py:299-310`）；
  `_iso_or_none` 5 处（各 adapter）；`AuditedAIProvider.complete/embed` ~55 行审计脚手架重复
  （`provider.py:394-520`）；连接策略 3 处（`database.py:128-142`、`evidence_basket_service.py:539-560`、
  `batch_service.py:492-520`）。
- **Why it matters:** 每组都是"一处改动、他处漂移"的温床（尤其指纹警告映射——工具输出一致性直接可见）。
- **Suggested owner type:** 编码代理
- **Dependency:** 无；建议在 v0.7/v0.8 触碰对应文件时顺手做，不专程改
- **Do-not-do notes:** 每次只合并一组并跑该组测试；不做"大扫除"式 PR；
  连接策略合并时保持 `foreign_keys=ON` + `busy_timeout` 语义不变。

### TD-17 存储卫生：create_tag 映射 + legacy WAL 守卫
- **Evidence:** `create_tag` check-then-insert 未包 `BEGIN IMMEDIATE` 且未映射
  `IntegrityError`（`src/database.py:838-851`，对照 `create_project:925-926`）；
  `legacy_backup_upgrade_service.py:89-94` 复制 `-wal/-shm` 前不验证源已关闭，
  且只按固定相对路径检查生产 DB（`:77-80`）。
- **Why it matters:** 前者并发下裸 sqlite3.IntegrityError 直接进 UI；
  后者可产生撕裂数据的旧备份升级结果。
- **Suggested owner type:** 编码代理
- **Dependency:** 无
- **Do-not-do notes:** create_tag 照 create_project 的既有映射模式；
  legacy 守卫应解析 settings 数据库路径后再比较（不只固定相对路径），并拒绝只读探测失败时继续。

### TD-18 导入管线原子性/恢复提示
- **Evidence:** `document_service.py:324-409`：文档创建/逐页/统计为多个事务；
  崩溃留 PROCESSING 半成品（可续传 `:343-351`，`import_records` 记录尝试）。
- **Why it matters:** 用户重启后看到"卡在处理中"的文档，无 UI 解释。
- **Suggested owner type:** 编码代理
- **Dependency:** 无
- **Do-not-do notes:** 不要为原子化而重构成巨型事务（长事务会拖垮 30s busy_timeout 下的其他会话）；
  目标是恢复路径的用户可见性（续传提示/清理建议），不是事务合并。

### TD-19 工具超时：删除装饰性声明或接线 deadline
- **Evidence:** 七个工具定义均声明 `timeout_seconds=30.0`，但执行器不消费
  （`src/agent/execution/executor.py:218` 只传 run_id/request_id；
  `ToolContext.deadline_epoch_ms` 字段存在未被赋值）。
- **Why it matters:** 声明给人以"有超时保护"的错觉；实际只受 provider timeout（30s）与服务层天然界限约束。
- **Suggested owner type:** 编码代理
- **Dependency:** v0.7 写能力前应解决（写操作更需要真超时）
- **Do-not-do notes:** 二选一：接线（executor 计算 deadline 传入 ToolContext，adapter 自行尊重）
  或删除声明并文档化"超时由 provider 层承担"；不要保留无效声明。

### TD-20 Mode 1 Hosted 服务托管启动
- **Evidence:** `src/agent_client.py:39` 默认 `http://127.0.0.1:8000`；
  `scripts/service_manager.py` 无 hosted 子命令；无 .bat 引用 `src.hosted.server`。
- **Why it matters:** UI 提供的模式其前置条件没有任何脚本供给；演示日手工拉起是隐性操作负担。
- **Suggested owner type:** 维护者
- **Dependency:** 无（hosted server 是 LOCAL_ONLY 的可选进程，与比赛冻结无冲突，但时点自选）
- **Do-not-do notes:** service_manager 增加 `start-hosted/stop-hosted/status-hosted` 时必须沿用
  PID 身份校验模式（可执行文件比对，杜绝误杀）；hosted 进程保持显式
  `EKB_RUNTIME_PROFILE=hosted`，禁止静默回退。

### TD-21 UI 阻塞与大渲染治理
- **Evidence:** 混合搜索/Ask-AI/OCR 同步阻塞（`pages/4:259`、`src/rag_answer_ui.py:130`、
  `pages/5:351`）；Mode 1 逐引用串行拉源元数据（`pages/0:292-298`，最坏 N×8s）；
  大渲染无界（`pages/4:1216` 全文、`:1291-1293` 全 prompt、`:364-365` 知识结果不分页）；
  每结果会话键累积（`:1054-1059,1307-1312`）。
- **Why it matters:** 单用户下是"体验债"非"故障"；但演示机器或大 PDF 下会放大。
- **Suggested owner type:** 编码代理
- **Dependency:** Mode 1 串行拉取在冻结解除前不动（页面冻结）
- **Do-not-do notes:** 不要引入异步改造（Streamlit 脚本模型不适合）；优先加渲染上限与分页、
  超时反馈文案；知识结果分页照 page 视图 10/页既有模式。

### TD-22 死代码清理窗口
- **Evidence:** `scripts/phase3_embedding_calibration.py` 零引用（HIGH 信心死代码）；
  `experiments/streamlit_image_coordinates_compat/` 已被 closure inventory 标注"归档/实验"；
  `scripts/ai_embedding_experiment.py`、`ai_smoke_test.py` 仅靠守卫测试存活（MEDIUM/LOW）。
- **Why it matters:** 仓库导航成本；付费探针脚本混在正式脚本里加重 R-05 的"裸构造"观感。
- **Suggested owner type:** 维护者
- **Dependency:** 无
- **Do-not-do notes:** AGENTS.md 数据管理规则只禁自动删用户数据，脚本删除需维护者逐个确认；
  删除前把决策记录进对应 phase 文档；移动 experiments/ 时保留 `note_geometry.py:4` 的引用注释一致性。

### TD-23 CI 引入决策
- **Evidence:** 无 `.github/`、tox、pre-commit；唯一自动化门禁是手动
  `scripts/release_check.py`（`pyproject.toml` 仅 ruff/pytest 配置）。
- **Why it matters:** 所有不变量（含 v0.7 后的写安全）依赖"人记得跑脚本"；
  这是 v0.7 前最大的过程风险，但也可能违背单机单人开发现实。
- **Suggested owner type:** 维护者（决策）
- **Dependency:** TD-02/TD-10 之后（先把门禁修到能在冻结树上跑过）
- **Do-not-do notes:** 若引入，最小形态 = GitHub Actions 跑 `ruff + pytest`（不含容器、不含真实 key）；
  私有源仓库注意 runner 上不落 `.env`；不引入则把"发布前必跑 release_check"写进
  `docs/repository-maintenance.md` 作为补偿。

---

## 明确不列为债务的观察项

以下已在总审计记录为 OBSERVATION，**故意不转化**为债务：database.py 体积（v0.7 触碰时顺势拆）、
FTS 运行期漂移检测缺失、迁移驱动 `current_version` 写法、WAL 迁移失败后持久、
`DemoHTTPError` 命名反向（冻结声明有意）、`#N` 正则严格性（有意 fail-closed）、
UI Mock 回退（有标注）、`DECISION_PROVIDER_FAILED` 吞 manual 区分（hosted 门缓解）、
`qwen_client` 异常文本插值（构造上安全）、Hosted env 未进 README（WP6B 重启时一并补）、
Streamlit XSRF 未显式 pin、bat 无 BOM、README "PowerShell" 措辞。
