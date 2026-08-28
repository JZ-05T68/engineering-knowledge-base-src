# Engineering Knowledge Base v0.6.0

[简体中文](README.md) | [English](README_EN.md)

**Engineering Knowledge Base（EKB）**

一个本地优先的个人知识系统，用于沉淀、组织、验证和复用长期知识资产。

EKB 面向需要长期积累工程经验的个人用户。它把用户主动导入的资料、人工记录的经验和可核验的来源
组织为本地知识资产，而不是把项目简化成 PDF 摘要器、RAG 工具或通用聊天机器人。

## 定位

通用 AI 可以生成答案，但无法天然保存一个人的长期经验，也不会自动理解某个项目为什么这样决策、
一次故障如何定位，或者某条失败路径为什么不应重复。

EKB 关注长期沉淀以下内容：

- 项目决策及其背景；
- 调试过程与排障路径；
- 错误、失败和返工经验；
- 学习记录与概念理解；
- 可复用的工程实践。

这些内容通过来源、修订、指纹、证据和检索形成可追踪、可验证、可复用的个人知识资产：

```text
资料 → 理解 → 知识对象 → 来源核验 → 知识记忆 → 检索 → 复用 → 工程能力
```

## 核心架构

```text
Source ── Fingerprint ──> Knowledge Object ── Revision ──> 可审计演进
  │                              │
  ├── Document / Page            ├── Relation
  ├── Note / Evidence            └── Knowledge Memory
  │                                      │
  └──────── Provenance ──────────────────┤
                                         v
                       Retrieval（Page / Object / Memory）
```

| 构件 | 作用 |
| --- | --- |
| **Knowledge Object** | 把概念、事实、原理、经验、问题和决策组织为可管理的知识单元。 |
| **Source** | 将知识对象连接到本地文档、页面、笔记或证据。 |
| **Revision** | 以追加式历史记录保存知识对象的变更，而不是静默覆盖演进过程。 |
| **Fingerprint** | 捕获来源的规范化 SHA-256 指纹，用于识别来源有效、变化、缺失或未知状态。 |
| **Evidence** | 保留整页、文字选区或图片区域的来源定位与人工确认状态。 |
| **Knowledge Memory** | 手动记录问题解决、经验和决策，并保留与知识对象或其他本地来源的联系。 |
| **Retrieval** | 在页面资料、知识对象和知识记忆之间提供本地检索与来源回链。 |
| **AI 能力** | 可审计、引用约束、按需触发的可选增强层，不替代本地事实来源和人工确认。 |

## v0.6.0 — Agent Foundation

v0.6.0 在 v0.5.3 可审计 AI 接入之上，建立 Agent Foundation 与托管部署基础：本地单步只读
Agent、冻结的公开 HTTP 合同与容器化打包。**实际公网部署已由维护者于 2026-08-28 决策推迟
出本发布门槛；本版本不包含、也不声称任何公网部署完成。**

当前能力包括：

- **单步只读 Agent**：一次请求内 Decision → 0/1 个 Tool → Final Answer；无循环、无多步
  规划、Agent 自主重试为 0；逻辑模型调用不超过 2 次。
- **7 个 READ_ONLY Tool**：`page_search`、`knowledge_search`、`get_knowledge_object`、
  `get_knowledge_memory`、`inspect_provenance`、`inspect_source_integrity`、`get_evidence`；
  Registry 只允许只读工具，未知或越权工具一律 fail-closed。
- **rag_answer 是 Final Answer Stage 而非 Tool**：最终回答只能基于 Tool 证据生成，并复用
  既有引用校验；未知、伪造、越界引用不显示半成品回答。
- **来源完整性语义**：`inspect_source_integrity` 只读取已捕获的来源指纹状态
  （valid / changed / missing / unknown），不刷新、不重算、不写库；过期来源不会被静默当作
  可靠事实。
- **Hosted HTTP Agent API（WP2）**：`/health`、`/ready`、`POST /v0.6/agent/run`、
  `GET /v0.6/sources/{stable_id}`；公开 DTO 冻结，错误使用封闭文案目录，不暴露内部
  trace、ToolResult、token 用量或模型原始响应。
- **Hosted 安全边界（WP3）**：限流与并发上限、请求体上限、CORS 精确来源、预算拒绝、
  错误净化；密钥只存在于服务端环境，不进入日志或响应。
- **Hosted 存储安全（WP4）**：独立 Hosted SQLite 引导与 schema v12 exact 校验，
  拒绝符号链接与 sidecar；不复用 Local 生产数据库。
- **部署打包（WP5）**：Linux 容器与 non-root（UID/GID 10001）运行时打包，集成测试通过；
  云上实际部署验证保持 PAUSED。
- **本地运行不变**：正式服务固定绑定 `127.0.0.1:8501`；AI 默认手动模式，无 API Key 时
  全部离线基础功能保持不变；Agent 能力不改变任何既有本地工作流。

明确边界：Agent 无写能力、无自主重试、无长期会话记忆；无公网 Agent（WP6A PARTIAL /
PAUSED，WP6B NOT STARTED）；实际公网部署为推迟项（DEFERRED），恢复需维护者明确决策。

## v0.5.3 — 可审计 AI 接入

v0.5.3 在 v0.5.2 Knowledge Foundation 之上，为用户明确选定或检索出的本地知识增加可审计、
引用约束、按需触发的 AI 辅助能力，同时补齐 AI 调用台账、结构化导出和旧备份升级。

该版本能力包括：

- **ContextItem 与 KnowledgeContextPackage**：页面、知识对象、知识记忆和证据统一投影为只读
  ContextItem，并打包为带引用、来源、生命周期和排除信息的上下文包。
- **两个检索范围**：页面资料检索与个人知识检索继续离线可用，默认行为保持不变。
- **Ask AI / RAG Answer**：用户主动提问，AI 只能依据所选 KnowledgeContextPackage 回答。
- **引用运行时校验**：AI 回答中的每个引用都必须属于本次上下文包；未知、伪造、越界和空引用
  一律 fail-closed，不显示半成品回答。
- **只读 AI 整理经验**：按需生成结构化经验候选，只读预览，不自动写入知识资产。
- **AI 调用台账**：ai_calls 记录调用类型、来源、状态、Token 与目标引用；台账只读，不保存完整
  prompt、上下文正文或模型回答。
- **Knowledge Export**：知识对象、来源、关系、修订和知识记忆的结构化无损导出，含 manifest、
  逐文件 SHA-256 与每对象独立 Markdown。
- **AI Ledger Export**：ai_calls 审计元数据独立导出（JSON/JSONL 权威格式），不含正文与密钥。
- **schema v8 旧备份隔离升级**：旧数据库快照通过独立入口升级到当前 schema，原始备份保持不变。
- **schema v12**：知识资产与 AI 台账共存的当前数据库结构。
- **本地运行**：正式服务固定绑定 `127.0.0.1:8501`；无 API Key 时全部离线基础功能保持不变。

明确边界：AI 不自动写知识；Experience Candidate 不是已确认经验；没有 Agent；没有工具调用；
没有长期会话记忆；不会自动扫描私人资料；没有云同步；AI 调用台账不保存完整 prompt、上下文和回答；
AI 是可选层，不是基础功能依赖。

## 个人知识工作流

```text
用户主动导入或记录
        ↓
Document / Page / Note / Evidence
        ↓
Knowledge Object + Source Fingerprint
        ↓
人工复核、修订与关系组织
        ↓
Knowledge Memory
        ↓
Page / Object / Memory Retrieval
        ↓
（可选）用户选定上下文 → Ask AI / AI 整理经验 → 只读结果
        ↓
回到来源核验并复用
```

系统可以提取元数据、渲染页面、检测文本层、标记待复核页面并建议整理路径，但不会自动覆盖原始资料、
修改用户笔记、删除文件或把未经确认的推断写成个人经验。

## 既有核心能力

- 导入 PDF，以 SHA-256 检测重复文件，逐页渲染 PNG 并提取已有文本层；
- 对扫描页显式执行本地单页 OCR，保留“未经人工核验”的边界；
- 浏览文档和页面，维护 Markdown、结构化笔记、标签与项目；
- 使用 SQLite FTS5、jieba 和字段权重执行本地全文检索；
- 创建页面、文字选区和图片区域证据，经人工确认后生成 citation-grounded 提示词包；
- 管理知识对象、知识记忆、来源、关系、修订和来源完整性；
- 创建、验证和恢复本地完整备份；
- 在显式配置时使用可选的页面向量与混合检索；AI 不可用时离线核心功能保持可用。

## 本地优先与信息边界

- 本地文件和 SQLite 是唯一事实来源；用户材料默认存放在本机。
- 正式服务固定绑定 `127.0.0.1:8501`，不暴露到局域网或公网。
- 系统仅处理用户主动导入或明确授权的资料，不抓取私人聊天或未授权第三方材料。
- 原始 PDF 和页面图像不会被自动覆盖或删除。
- 默认 `ai_mode="manual"`；没有 API Key 时应用仍可启动和使用核心功能。
- 不包含注册、登录、账号、密码、OAuth、JWT、角色、管理员或多用户权限系统。
- 不提供云同步；备份位置和备份介质由用户自行控制。

## 发布验证

v0.6.0 发布候选验证（2026-08-28 候选审计）包括：

- 全量 pytest：**2636 passed / 4 skipped，exit 0**（约 11 分钟）；
- Ruff：**PASS**；
- `git diff --check`：**clean**；
- schema v12：Hosted 存储与 Local 路径保持 exact-v12 校验，本候选无 migration；
- 生产数据库 `data/database/knowledge.db`：候选审计期间仅以只读方式记录
  SHA-256 `d116933c70e134622381372fe624ff228b22a8166216d87a550b80f8cece6f98`
  （该值随正常使用变化，发布时由 release_check 重新核对）；
- 已知 warning：Starlette TestClient 依赖 deprecation warning（记录在案）。

`scripts/release_check.py` 未在候选审计期间运行（它会打开正式数据库并创建备份，
需维护者授权的发布窗口执行）；其版本预期已对齐 0.6.0。正式监听验收
（`127.0.0.1:8501`，health HTTP 200）在发布窗口由 release_check 或人工验收完成。

这些数字记录发布候选基线，不代表所有工程领域、语料或查询都已获得同等质量保证。

## 版本演进

以下内容恢复各历史版本的正式范围与当时边界。v0.0.x 依据仓库中已关闭的同名 milestone，
v0.1.0 起同时由 tag、CHANGELOG 和发布记录交叉校验；这些历史条目不是当前 v0.6.0 能力说明。

### v0.0.1 — 初始本地工程知识库 MVP

建立本地 PDF 工作流：保存导入原件、逐页渲染图像、提取文本层、识别待复核页面、维护页面级
Markdown、浏览文档、执行本地检索、生成可追溯证据包，并在 SQLite 中持久化元数据。

### v0.0.2 — 页面级知识管理与后台运行

建立页面级整理能力，以及 Windows 后台启动、停止、状态查看、PID 校验、健康检查和轮转日志工作流。

### v0.0.3 — 连续待复核页面整理

为待处理、草稿、失败、已复核和已跳过页面建立连续队列，加入上一页/下一页、保存并继续、
未保存修改保护和可选快捷键。

### v0.0.4 — 可追溯页面检索与引用证据包

让页面检索可追溯到文档、原文件名、页码、本地来源路径、复核状态和命中上下文；证据包明确
分离来源材料与用户笔记，并对未复核页面给出警告。

### v0.0.5 — 多页面证据收集与引用工作流

引入持久化证据篮，可跨页面收集选区、编辑备注、调整顺序、移除证据、返回来源，并导出单文档
或多文档 Markdown 证据包；来源哈希与生成前校验防止把过期材料当作当前证据导出。

### v0.0.6 — 搜索筛选与状态恢复

扩展本地检索的文档、项目、标签、复核状态、命中字段、笔记和证据篮筛选，加入多项目/多标签
AND 语义、相关度与时间排序、分面计数、Unicode 字面量选项搜索和白名单 URL 状态恢复。

### v0.0.7 — 搜索命中理解与连续阅读

加入按页面或文档分组的结果视图、带来源标记的摘要和字面命中计数、按需页面预览、全局结果与
文档内导航，并恢复搜索、筛选、排序、面板、预览和结果焦点状态，且不把完整结果 ID 写入 URL。

### v0.0.8 — 完整备份、系统诊断与发布收口

完成经验证的本地完整备份、只读诊断、脱敏诊断报告、安全恢复预检和统一发布检查，随后直接
收口到 v0.1.0；仓库没有创建或规划 v0.0.9。

### v0.1.0 — 首次完整人工测试与正式发布

在首次完整人工验收后完成正式发布，包含基于 manifest 的完整备份、SQLite 在线快照与 SHA-256
记录、停服恢复与恢复前备份/回滚、只读完整性诊断、脱敏报告、可用的空状态和统一发布检查入口。

### v0.1.1 — 稳定性与体验补丁

将正式服务固定在 `127.0.0.1:8501`，改进 Windows 诊断，增加从导入结果进入待复核队列的显式入口，
并允许同页不同选区并存而拒绝规范化后的重复选区。schema v4 保持不变，未加入 AI、OCR、Embedding、
语义检索或网络 API。

### v0.1.2 — 批量操作与整理效率

为可见搜索结果和当前复核批次增加有界批量更新，配套预检、显式确认、一次性 action token、
稳定选择范围、增量标签/项目修改和单次 SQLite 事务；明确不提供跨页或“选择全部匹配结果”。

### v0.2.0 — 超长工程文档与非标准页面处理底座

引入隔离的单页处理、空白/短文本/横向/旋转页面确定性诊断、页级失败隔离、文档诊断摘要、
120 页自动化基线和 300 页混合 PDF 验收。schema v4 保持不变，未加入 OCR、Embedding、语义检索
或外部模型 API。

### v0.2.1 — 默认证据篮并发补丁

把“查找或创建默认证据篮”放入同一个短事务，使并发首次访问返回同一证据篮而不会创建重复记录；
schema v4 和产品范围均未改变。

### v0.2.2 — 本地 OCR

加入基于 RapidOCR 和 ONNX Runtime 的显式、离线、单页印刷体 OCR。OCR 文本独立保存、以未经人工
核验的初稿展示并带警告进入本地搜索，绝不覆盖 PDF、页面图像、文本层、人工 Markdown 或复核状态；
手写、公式、结构化复杂表格、旋转校正和批量 OCR 均不在范围内。

### v0.2.3 — v0.2.x 收口

以 PDF 和页面图像原子写入、中断导入恢复、大结果集导航稳定性、数据库与文件资产重复导入零增量，
以及重复探测失败时的可诊断记录完成长文档底座收口；自动化测试记录为 553 项通过。

### v0.2.4 — 发布与部署一致性补丁

统一发布检查、备份与恢复工具、应用版本显示和正式本地部署入口；不包含业务能力、schema 或 AI 变更。

### v0.3.0 — 结构化笔记基础

为文档、页面、文字选区和图片区域笔记建立完整增删改查。文字选区分离来源快照、用户摘录和个人笔记，
图片区域将坐标绑定到原始 PNG 与 SHA-256；schema v5 采用迁移前备份的增量升级，文档删除增加影响预览、
标题精确确认、隔离区和两阶段清理。

### v0.3.1 — 笔记重要程度与视觉映射

为四类结构化笔记加入重点、次重点和一般三级重要程度，支持筛选和自定义徽章背景色；schema v6
继续采用迁移前备份的增量升级，既有笔记默认设为一般。

### v0.3.2 — 跨文档知识聚合与文档删除生命周期

加入按项目或标签组织的只读分页聚合视图，以及重要程度和笔记类型筛选与来源回链。文档删除增加
影响报告、逐操作隔离区 manifest、重启对账、未知状态保守保留和证据移除高风险确认。

### v0.3.3 — 文档管理与数据安全

引入独立文档管理页并集中删除确认流程；服务层标题精确校验在不匹配时保持零副作用，继续沿用
v0.3.2 的隔离区和恢复设计。schema v6 保持不变。

### v0.4.0 — 证据对象与来源模型

把整页、文字选区和图片区域证据统一到共同的来源定位、校验和人工确认语义下；耐久锚点使用来源文本
哈希，或原始 PNG 哈希、尺寸与坐标。schema v7 将既有证据迁移为未确认的文字选区证据。

### v0.4.1 — 基于证据的引用提示词包

只允许人工确认的证据生成提示词包，生成前校验来源，任一来源失效即 fail-closed。文字选区包含来源
正文，整页包含明确标记的当前页面文本，图片区域只包含定位与坐标而不猜测图像内容；用户笔记始终
与来源事实分离，EKB 本身仍不调用 AI。

### v0.4.2 — 提示词新鲜度与过期输出保护

把已生成提示词包绑定到当前问题和已确认证据输入；证据、确认状态、排序、备注、页面文本或来源
有效性变化会使旧包失效并清除，无关的标签、项目、复核状态或未确认证据变化不会误触发失效。
schema v7 和依赖均未改变。

### v0.4.3 — 真实问题验证与 AI 就绪决策门

用真实工程资料和问题验证来源真实性、人工确认、跨文档分离、图片区域边界、过期提示词失效和原始页
追溯。CONDITIONAL GO 仅允许后续开始 AI 集成，不表示当时已经实现或验证 AI 集成。

### v0.5.0 — AI 基础与可选混合检索

加入可选 provider 接口、受控真实调用、页面级 Embedding 持久化与有效性复用、显式索引编排、持久
向量召回，以及通过 RRF 合并关键词与向量候选的可选混合检索。生产与隔离测试的数据、端口、日志、
备份和运行状态保持分离；manual AI mode 和离线回退仍是默认边界。

### v0.5.1 — 检索稳定化

建立冻结评估工作流，明确混合检索回退状态，以真实 Embedding 校准假设，提供只读索引覆盖率，并增加
诚实的弱证据提示。生产环境仍使用等权 RRF，索引完成仍需手动执行，没有加入数值相似度准入阈值，
最终展示结果边界仍未定义；发布记录为 1,475 项测试通过、生产 rollout 成功且零 rollout regression。

### v0.5.2 — Knowledge Foundation

v0.5.2 是一次产品方向重构：EKB 从以页面资料、证据和 RAG 为主要叙事的本地工程知识库，进一步转向
用于长期沉淀、验证、检索和复用个人知识资产的 Knowledge Foundation。RAG 和外部 AI 不再是产品第一
定位；AI 被明确放在知识基础之上，作为可选增强层。

该版本建立 Knowledge Object、Knowledge Memory、类型化 Relation、稳定标识、生命周期和追加式
Revision；通过 Source / Provenance 将知识对象连接到本地 document、page、note 或 evidence，并以
规范化 SHA-256 Source Fingerprint 判断来源有效、变化、缺失、损坏或未知状态。schema v11 为
Knowledge Object 和 Knowledge Memory 建立独立 SQLite FTS5 索引、同步触发器、legacy 回填和确定性
rebuild 路径，从而形成可回到本地来源核验的个人知识检索底座。

v0.5.2 尚未实现 Agent、工具调用、自动经验学习、后台知识改写或云同步；Agent Foundation 从 v0.6.x
才开始进入范围。当前 v0.6.0 的正式能力与边界见上文独立章节。

## Roadmap

| 版本线 | 主题 | 状态 |
| --- | --- | --- |
| **v0.5.x** | Knowledge Foundation | v0.5.3 完成可审计 AI 接入、台账、导出与备份升级。 |
| **v0.6.x** | Agent Foundation | v0.6.0 Agent Foundation 与托管部署基础（WP1–WP5）已完成并进入发布收尾；实际公网部署由维护者决策推迟出发布门槛（WP6A PARTIAL/PAUSED，WP6B NOT STARTED）。v0.6.1 Competition Demo Experience 准备工件已就绪，待 v0.6.0 发布后激活。Agent 自主选择和工具调用从这一阶段才开始。 |
| **v0.7.x** | Personal Experience Agent | 未来规划；尚未实现。从这一阶段才开始使用用户长期经验。 |
| **v0.8.x** | Agent Reliability | 未来规划；尚未实现。处理 Agent 错误行为与可靠性。 |
| **v0.9.x** | Agent Hardening | 未来规划；尚未实现。处理长期运行、成本、上下文、记忆污染、Eval 与工程硬化。 |
| **v1.0.0** | Personal Experience System | 长期方向；尚未实现。完整个人经验系统。 |

详细边界见 [v0.5.x Roadmap](docs/v0.5.x-roadmap.md)。路线图不是当前能力承诺，也不承诺发布日期。

## 限制

- 当前是 Windows 本地、单用户系统，没有多用户协作或权限模型。
- 没有云同步、云账号或托管服务；跨设备备份需要用户自行管理。
- Agent、工具调用和长期会话记忆尚未实现，系统不会自主规划任务或代表用户持续行动。
- 不会自动学习个人经验；知识对象和知识记忆需要用户主动创建、复核或确认。
- Experience Candidate 只是 AI 整理候选，不是已确认经验，也不会自动写入知识库。
- 本地 OCR 面向单页印刷体，不支持手写、公式、复杂表格结构识别或批量 OCR。
- 关键词检索不会自动扩展同义词；可选混合检索不代表全面语义理解，也不替代来源核验。
- 当前搜索展示和批量操作有加载范围限制；超大知识库的深分页性能仍需持续观察。
- 完整备份是本地目录结构，不是加密归档；备份介质安全由用户负责。

## 安装

环境要求：Windows 10/11、PowerShell、Python 3.11，以及支持 FTS5 的 Python SQLite。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

`.env` 是可选的本地配置，不应提交到 Git。完整的新机器恢复流程见
[Windows 恢复与环境重建](docs/windows-recovery.md)。GitHub 不保存用户数据库、导入资料、凭据、日志或缓存。

## 启动与停止

- 启动：双击 `启动工程知识库.bat`；
- 静默启动：双击 `静默启动工程知识库.vbs`；
- 查看状态：双击 `查看运行状态.bat`；
- 停止：双击 `停止工程知识库.bat`。

开发调试时可前台运行：

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

正式地址固定为 <http://127.0.0.1:8501>，健康检查为
<http://127.0.0.1:8501/_stcore/health>。不要改为 `0.0.0.0` 或暴露到外部网络。

## 本地数据与安全

```text
data/
├── raw/            # 原始 PDF；兼容既有本地数据路径
├── pages/          # 页面 PNG
├── markdown/       # 页面 Markdown
└── database/
    └── knowledge.db
```

`data/`、`backups/`、`logs/`、`runtime/` 和本地配置均与应用源码分离并被 Git 忽略。数据库升级使用
纯增量迁移、迁移前备份、事务、完整性检查和外键检查。文档删除需要显式影响预览与确认；系统不会自动
删除原始 PDF 或页面图像。

## 质量检查

```powershell
python -m pytest
python -m ruff check .
git diff --check
```

统一发布检查：

```powershell
.\.venv\Scripts\python.exe scripts\release_check.py
```

发布提交和 tag 收口阶段可使用已验证备份与 stopped-service 模式；任何检查失败都必须在发布前明确处理。

## 文档

- [CHANGELOG](CHANGELOG.md)
- [v0.5.x Roadmap](docs/v0.5.x-roadmap.md)
- [v0.6.0 Release Notes（中文）](docs/v0.6.0-release-notes.md)
- [v0.6.0 Release Notes（英文）](docs/v0.6.0-release-notes-en.md)
- [v0.5.3 Release Notes（中文）](docs/v0.5.3-release-notes.md)
- [v0.5.3 Release Notes（英文）](docs/v0.5.3-release-notes-en.md)
- [Windows 恢复与环境重建](docs/windows-recovery.md)
- [GitHub Releases](https://github.com/JZ-05T68/engineering-knowledge-base-src/releases)

`README.md` 与 `README_EN.md` 是对等的正式项目说明；产品定位、能力、限制和 roadmap 变更必须同步维护。
