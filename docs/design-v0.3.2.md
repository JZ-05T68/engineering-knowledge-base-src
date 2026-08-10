# Engineering Knowledge Base v0.3.2 设计文档 — 导入文档生命周期删除冻结与跨文档聚合基线

## 0. 文档状态

- 状态：**Design Freeze Ready（待人工审核）**
- 依据：v0.3.1 已发布生产基线（peeled commit
  `189912c24083454c7eab8d21ba23d59355552d4e`，schema v6，
  `docs/design-v0.3.1.md` §4 将"跨文档聚合"顺延至后续版本）
- 开发分支：`kimi/v0.3.2-cross-document-aggregation`（直接基于上述 commit，
  独立 worktree，不含任何旧工作现场内容）
- 本文件为 v0.3.2 实施的唯一设计依据；任何实现偏离须先修订本文件并获批准
- 本轮（S1）只冻结设计；不实现 S2 删除代码，不实现跨文档聚合代码，
  不改 schema，不动生产目录

## 1. Goals

1. 冻结"删除已导入文档"的数据语义：它是 **document lifecycle deletion**，
   即永久删除一个已导入 EKB 的 document 及以它为根、受其约束的全部
   关联数据与派生数据，而不是磁盘层面的 `os.remove(source.pdf)`。
2. 冻结用户笔记处理策略：四类笔记与源 document 生命周期绑定，
   经充分披露与明确确认后随文档永久删除；不制造无源笔记。
3. 为证据篮（evidence_items）损失建立独立于普通删除确认的高风险确认模型。
4. 为 deletion quarantine 的两个 crash window 设计可判定、可恢复、
   fail-closed 的 reconciliation 机制（含 per-operation manifest/journal）。
5. 消除裸 `Database.delete_document()` 被误用作业务删除入口的可能（定原则，
   具体处置在 S2 实施前经调用点审计后决定）。
6. 定义 v0.3.2 "跨文档聚合"的产品语义与数据边界：基于现有
   notes / evidence / tags / projects 的动态只读聚合，不新增物化聚合表。
7. 冻结聚合 × 删除一致性模型：任何聚合视图在文档删除后不得出现悬空引用。

## 2. Non-goals（冻结排除）

v0.3.2 **不引入**以下任何一项，全部列入 future considerations（§21）：

- Trash / 回收站
- soft delete / `deleted_at` 标记
- Restore / 撤销删除
- detached / orphaned archival notes（"删除文件但保留无源笔记"）
- 批量删除（一次删除多个 document）
- 物化 aggregation 表 / 聚合结果缓存
- AI 自动聚合 / AI 摘要 / 自动主题聚类
- 对 tags / projects / evidence_baskets 本体的删除语义变更
- schema 大版本重构（S2 目标为 **零 schema 变更**；若实施中发现必须变更，
  须先修订本文件并获批准）

## 3. Existing v0.3.1 baseline（审计结论）

以 v0.3.1（commit `189912c`，schema v6）为唯一事实来源：

- **数据模型**：`documents`（sha256 UNIQUE 内容寻址）为根；`pages`、
  四类笔记共用 `notes` 表（`note_type` + CHECK 互斥）、`evidence_items`、
  标签/项目关联表均经外键指向 documents/pages，除
  `import_records.document_id ON DELETE SET NULL` 外全部
  `ON DELETE CASCADE`；FTS5 `page_search` 由 `pages_fts_*` 触发器同步；
  每条业务连接显式 `PRAGMA foreign_keys = ON`（`src/database.py:101-118`）。
- **现有删除实现** `src/document_deletion_service.py`（455 行）：
  `preview_document_deletion()` 只读影响预览（四类笔记分别计数、证据项、
  FTS、关联表、import_records、文件清单、缺失文件、路径异常）；
  `delete_document()` 六阶段：路径异常中止 → 登记文件原子移入
  `data/.deletion-quarantine/` → 单事务 `DELETE FROM documents`
  （CASCADE + FTS 触发器）→ 同事务 10 项残留校验 + `foreign_key_check`
  → 提交后销毁隔离区 → 空目录清理。任一失败：DB 回滚 + 文件移回原位。
- **现有 UI**（`pages/2_浏览资料.py:944-1018`）：危险区域 expander +
  影响 metric + checkbox + 手输完整文档标题的双确认；路径异常禁用按钮；
  明确无批量删除入口。
- **已知缺口**（v0.3.2 要收口的全部内容）：
  1. crash window：DB 提交成功后销毁隔离区前进程死亡，文件永久滞留
     `.deletion-quarantine/`，无任何对账机制；
  2. crash window：文件已入隔离区但 DB 未提交时进程死亡，文档在 UI 中
     显示为文件缺失，无自动恢复；
  3. evidence_items 随文档静默级联，仅在预览中显示一个总数；
  4. `src/database.py:318 delete_document()` 是裸级联删除公开 API，
     无文件处理、无残留校验，生产代码虽无调用方但入口仍然存在；
  5. 隔离区目录命名（`{doc_id}-{timestamp}/` + 序号前缀）不包含足以支持
     可靠恢复判定的持久化元数据（无 manifest）。
- **跨文档聚合**：仓库中零实现、零 stub；`docs/design-v0.3.1.md` §4 列为
  Non-goal 顺延；`docs/design/v0.3.0-foundation-design.md` §30 将 v0.3.2
  方向描述为"知识组织关系"，与本文件 §16 的聚合定义兼容。

## 4. Imported document lifecycle

一个 imported document 的完整生命周期：

1. **导入**：上传字节流 → SHA-256 查重 → 原子写入
   `data/raw/{sha256}_{stem}.pdf` → 逐页渲染
   `data/pages/{document_id}/page_NNNN.png` → 文本层/OCR 入 `pages` 列 →
   jieba 分词副本经触发器入 `page_search` → 写 `import_records`。
2. **使用**：用户围绕 document/page 建立四类笔记（含 importance）、
   标签、项目归属、证据篮条目；页面 Markdown 落
   `data/markdown/{document_id}/page_NNNN.md`。
3. **删除（本文件 §5 冻结语义）**：以 document 为根的整棵数据树经
   preview → 确认 → 隔离 → 单事务级联删除 → 校验 → 销毁 永久移除；
   `import_records` 保留审计；tags / projects / evidence_baskets 本体保留。
4. **再导入**：删除后 sha256 唯一约束释放，同名/同内容文件可作为全新
   document 重新导入（新的 document_id，不复活任何旧数据）。

关键事实：原始 PDF 按内容寻址（sha256 前缀），页面产物按 document_id
目录寻址；同一 document 的全部派生数据在数据库中均可经
documents/pages 两级外键到达，在文件系统中均可经 `documents.source_path`、
`pages.image_path`、`pages.markdown_path` 三个登记路径到达。
**凡不在此登记闭包内的文件，删除流程一律不触碰。**

## 5. Deletion semantics（冻结）

1. 删除对象是 **document 及其生命周期闭包**，不是 PDF 文件本身。
   UI 与文案使用"**永久删除导入文档及关联数据**"层级的表述
   （具体措辞实现阶段调整），禁止让用户误以为只是清理磁盘文件。
2. 删除是**永久**的：无 Trash、无 soft delete、无 Restore。
3. 删除以**单文档**为粒度：无批量入口。
4. 删除必须是**显式多级确认**后的有意行为（§9）。
5. 删除必须**事务一致**（§10）：提交后不允许存在 orphan page / note /
   evidence / FTS 行 / dangling FK；失败时不允许"DB 删一半、文件删一半"。
6. 文件系统侧采用 **quarantine 补偿模型**（§11）：DB 提交前文件随时可
   完整恢复；DB 提交后文件才允许物理销毁。
7. 删除必须**可审计**:`import_records` 保留（SET NULL）;crash recovery
   依赖持久化 manifest（§11.2）而非猜测。
8. 共享本体（tags、projects、evidence_baskets）**永不**因删除单个
   document 被删除，仅关联行消失。

## 6. Impact matrix（冻结）

删除 `documents.id = X` 时的完整影响矩阵：

| 对象 | 存储位置 | 删除时行为 | 机制 |
|---|---|---|---|
| document 行 | `documents` | 永久删除 | 单条 DELETE（服务层） |
| 页面行 | `pages` | 永久删除 | FK CASCADE |
| FTS 行 | `page_search` | 永久删除 | `pages_fts_delete` 触发器 + 残留校验兜底 |
| 文档笔记 | `notes`（note_type=document） | 永久删除 | FK CASCADE |
| 页面笔记 | `notes`（note_type=page） | 永久删除 | FK CASCADE（经 pages） |
| 文字选区笔记 | `notes`（note_type=text_selection） | 永久删除 | FK CASCADE（经 pages） |
| 图片区域笔记 | `notes`（note_type=image_region） | 永久删除 | FK CASCADE（经 pages） |
| importance 数据 | `notes.importance` 列 | 随笔记行永久删除 | 同行 |
| 选区/区域锚点 | `notes.source_*` / `notes.region_*` 列 | 随笔记行永久删除 | 同行 |
| 等级颜色偏好 | `note_display_preferences` | **不受影响**（全局单行表，不属于 document 生命周期） | — |
| 文档-标签关联 | `document_tags` | 关联行永久删除 | FK CASCADE |
| 页面-标签关联 | `page_tags` | 关联行永久删除 | FK CASCADE |
| 标签本体 | `tags` | **保留**（可能被其它文档引用） | — |
| 文档-项目关联 | `project_documents` | 关联行永久删除 | FK CASCADE |
| 页面-项目关联 | `project_pages` | 关联行永久删除 | FK CASCADE |
| 项目本体 | `projects` | **保留** | — |
| 证据条目 | `evidence_items`（含摘录快照、user_note） | 永久删除（§8 特殊保护） | FK CASCADE |
| 证据篮本体 | `evidence_baskets` | **保留**（仅失去条目） | — |
| 导入记录 | `import_records` | **保留**，`document_id` 置 NULL（审计） | ON DELETE SET NULL |
| 原始 PDF | `documents.source_path` 登记路径 | 隔离 → 提交后物理销毁 | quarantine（§11） |
| 页面 PNG | `pages.image_path` 登记路径 | 隔离 → 提交后物理销毁 | quarantine |
| 页面 Markdown | `pages.markdown_path` 登记路径 | 隔离 → 提交后物理销毁 | quarantine |
| OCR 文本 | `pages.ocr_text` 列（无独立缓存文件） | 随 pages 行删除 | FK CASCADE |
| 空目录 | `data/pages/{id}/`、`data/markdown/{id}/` | 仅当为空时 rmdir；含未登记文件则保留并警告 | 服务层 |
| 未登记文件 | 上述目录内未登记文件 | **绝不触碰** | 路径安全校验 |
| 历史备份 | `backups/` | **不受影响**（备份恢复语义不变） | — |
| 聚合视图 | 动态只读查询（§16，v0.3.2 新增） | 删除后自然消失，无残留行 | §18 |

## 7. Notes deletion semantics（冻结）

四类笔记（document / page / text_selection / image_region）继续与源
document 生命周期绑定。**本版不保留 detached note。**

理由（冻结）：

1. text_selection 笔记携带对源文本区间的 SHA-256 锚点与逐字快照，
   image_region 笔记携带对源 PNG 内容的 SHA-256 锚点与像素几何；
   其价值在于"可验证的引用"。
2. 删除源文档却保留这些笔记，等于主动制造永远无法再验证的 citation——
   与 EKB evidence-grounded / citation-grounded 的数据模型直接冲突。
3. 无源 document/page 笔记还会推翻现有 notes 表的归属互斥 CHECK 约束与
   全部读取路径的"源必然存在"假设，成本与风险远超收益。

因此：**用户选择永久删除文档时，经 §9 的充分披露与明确确认后，其关联
笔记随之永久删除。** 披露义务：impact preview 必须分别展示四类笔记数量，
不允许只显示 notes 总数（note_type 四类分行展示）。

## 8. Evidence basket special handling（冻结）

`evidence_items` 不是普通关联行：它可能包含用户手工整理的摘录快照
（`user_excerpt`、`source_excerpt` 快照列）、`user_note` 批注与上下文，
是跨文档知识整理的成果，损失不可逆且单看"删除一个 PDF"不易预见。

冻结如下：

1. 普通删除确认链（§9 第 1–4 步）始终需要，证据数量不得只埋在普通
   metric 或一句笼统"关联数据"文案里。
2. **当关联 evidence_items > 0 时**，S2 UI 必须在普通确认之外增加一个
   **独立的高风险确认项**，明确文案语义为：

   > 此文档还有 N 条证据篮条目，这些摘录、快照或用户批注也将永久删除。

   用户必须显式确认该项（独立 checkbox 或等价显式动作），删除按钮才可启用。
3. evidence_items = 0 时不显示该项，不增加无谓摩擦。
4. 证据篮本体（`evidence_baskets`）保留；删除后篮子可能变空，这是合法状态。
5. 聚合视图（§16）中源自该文档的条目随删除消失，不得显示快照残影（§18）。

## 9. Confirmation model（冻结）

永久删除一个 document 必须依次经过：

1. 用户主动进入危险删除区域（expand​er 收起态为默认）；
2. 展示 impact preview：文档标题、文件名、页数、**四类笔记分别计数**、
   evidence_items 计数（突出显示）、标签/项目关联计数、import_records
   计数、磁盘文件清单与总大小、缺失文件与路径异常警告；
3. checkbox 确认"我已知晓此操作不可撤销"；
4. 手输完整文档标题（精确匹配才通过）;
5. 若 evidence_items > 0：额外的独立高风险确认项（§8.2）;
6. 以上全部满足，删除按钮才启用；任一条件不满足按钮禁用；
7. 存在路径异常（path_anomalies）时按钮禁用，删除整体不可发起。

一次点击立即删除、Enter 键意外触发、批量勾选删除均禁止。
删除成功后清空相关确认态并显式 rerun；确认态不跨文档复用。

## 10. DB transaction model（冻结）

1. 数据库阶段的删除在**单个事务**内完成：
   `DELETE FROM documents WHERE id = ?` + 全部级联 + FTS 触发器同步 +
   10 项残留校验（pages、四类笔记、evidence_items、page_search、
   document_tags、page_tags、project_documents、project_pages、
   import_records 引用）+ `PRAGMA foreign_key_check`，任一校验非零即
   整体回滚。
2. 所有连接必须经 `Database._connection()`（或等价封装）以获得
   `PRAGMA foreign_keys = ON`；**禁止**用裸 `sqlite3.connect` 执行删除。
3. 事务粒度不得跨越文件系统操作：文件移动/销毁在事务外，用 quarantine
   补偿（§11），不允许"DB 提交与文件销毁假装原子"。
4. 删除服务是唯一业务级删除入口（§15）。

## 11. Filesystem quarantine model（冻结方向，S2 实施细化）

### 11.1 现状与不足

v0.3.1 的隔离区为 `.deletion-quarantine/{doc_id}-{timestamp}/`，文件加
序号前缀移入。它支持"移回"但不携带足以可靠判定恢复方向的持久化元数据。

### 11.2 per-operation manifest/journal（冻结要求）

S2 起，每一次删除操作使用独立目录：

```text
data/.deletion-quarantine/op-{uuid4}/
├── manifest.json          # 原子写入（临时文件 + flush + os.replace）
└── files/                 # 被隔离文件，保留序号前缀
```

manifest 至少包含：

- `operation_id`（uuid4，与目录名一致）;
- `document_id`;
- `created_at`、`app_version`;
- `files[]`：每项含 `original_path`、`quarantine_path`、`size_bytes`、
  `sha256`（用于恢复校验与冲突判定）;
- `phase`：仅作诊断提示，**不作为判定依据**（见 §13 判定规则）。

manifest 写入纪律：先写 `manifest.json.tmp-{uuid}` → flush/fsync →
`os.replace` 原子落位；任何更新走同一原子替换路径；**禁止原地追加写**。
manifest 在"开始移动第一个文件之前"必须已完整落盘。

### 11.3 不变量

- 隔离区只保存登记闭包内的文件（§4）；未登记文件永不进入隔离区。
- 同一 original_path 在一次操作中只出现一次（按 resolved path 去重）。
- 销毁隔离区只允许发生在 DB 删除已提交之后（Case 2，§13）。

## 12. Crash windows（冻结枚举）

以操作时间轴枚举全部崩溃点：

- **W0** preview 之后、第一个文件移动之前：无副作用，无需恢复。
- **W1** 部分文件已入隔离区、尚未移动完（manifest 已存在）。
- **W2** 全部文件已入隔离区、DB 事务未开始。
- **W3** DB 事务进行中（未提交）：连接死亡即回滚，DB 无变更；
  文件在隔离区。
- **W4** DB 提交成功后、隔离区销毁前：DB 已删，文件在隔离区。
- **W5** 隔离区销毁进行中：部分文件已删、目录可能残留。

W1/W2/W3 归并为 **Case 1**（DB 中 document 仍存在）;
W4/W5 归并为 **Case 2**（DB 中 document 已不存在）。
判定不依赖崩溃发生的确切时刻，只依赖重启后可观测状态（§13）。

## 13. Recovery / reconciliation state machine（冻结）

### 13.1 触发时机

应用启动时（服务工厂初始化路径，如 `src/runtime.py`）对
`.deletion-quarantine/` 执行一次只读扫描 + 按需对账；结果（恢复/销毁/
警告）写入日志，并在系统维护页展示未决项。对账不得依赖 Streamlit 会话。

### 13.2 判定规则（对每个 op 目录）

1. 读取并解析 manifest。缺失/损坏/字段不全 → **fail-closed**（§14）。
2. 以 `document_id` 查询数据库（只读）:
   - **document 存在 → Case 1**：删除未提交。执行恢复：按 manifest 逐项
     把文件从 quarantine 移回 `original_path`；逐项校验存在性与
     `size_bytes`（sha256 抽检或全检由 S2 实测性能决定并记录）。
     全部恢复成功后移除 op 目录。任一文件恢复失败 → 保留现场 +
     maintenance warning，不继续销毁、不跳过该文件。
   - **document 不存在 → Case 2**：删除已提交。执行完成：
     物理销毁该 op 目录。销毁失败保留并警告，下次启动重试。
   - **document 存在性无法判定**（数据库不可读/锁超时等）→
     **fail-closed**，本次启动跳过该 op，warning。
3. 恢复路径冲突（`original_path` 已存在文件）：比较 size/sha256——
   一致视为已恢复（幂等），不一致 → **fail-closed**，
   禁止覆盖任何一方。
4. **幂等**：对账可重复执行；已完成的 op 目录不重复处理；
   恢复/销毁动作本身可安全重入。
5. **多 op 共存**：逐个独立处理，单个 op 失败不阻塞其它 op 的对账。

### 13.3 禁止行为（冻结）

- 禁止"启动时看到 quarantine 就清空"。
- 禁止在 Case 1 判定下销毁任何隔离文件。
- 禁止在 Case 2 判定下向数据库回写任何内容。
- 禁止以 manifest 的 `phase` 字段替代数据库事实做判定。

## 14. Ambiguous recovery / fail-closed policy（冻结）

以下任一情况，对该 operation **不执行任何自动动作**:

- manifest 缺失、损坏、JSON 无法解析、必填字段缺失；
- manifest 中路径非法（含 `..`、逃逸数据目录、指向数据根本身、
  符号链接）;
- document 状态无法判定；
- original_path 与 quarantine_path 同时存在文件且内容（size/sha256）冲突；
- 无法证明该 operation 属于哪个 document;
- 多个 manifest 声称同一 document_id 或同一 original_path。

fail-closed 的处置统一为：

1. **保留现场**：不删隔离区、不覆盖源位置、不改数据库；
2. 记录 maintenance warning（日志 + 系统维护页可见），包含
   operation_id、原因、涉及路径；
3. 该 op 进入"人工修复"队列，后续版本可提供显式的人工处置入口
   （本版只报告，不提供 UI 自动修复按钮）。

原则：**状态不明确时，保留数据的代价永远低于误删用户数据的代价。**

## 15. Database.delete_document API boundary（冻结原则）

原则（冻结）:

> document 的业务级永久删除只能通过 `document_deletion_service` 的
> 安全删除入口执行。

`src/database.py:318 delete_document()` 当前是无文件处理、无残留校验的
裸级联删除，且命名看起来像一个正常业务 API——这是必须消除的误用面。

S2 实施前先完成调用点审计（含 tests，如 `tests/test_database.py` 的直接
调用、历史兼容用途），然后在以下处置中择一并记录理由：

- 删除该 API（调用点改为 deletion service 或测试内联 SQL）;
- 私有化（改名 `_delete_document` 并注明仅服务层内部使用）;
- 改为显式 unsafe/internal primitive（如 `unsafe_delete_document_rows`，
  docstring 标明绕过文件与校验）。

无论择哪条，**不得继续保留一个命名如正常业务 API 的裸
`delete_document()`**。

## 16. Cross-document aggregation definition（冻结定义）

### 16.1 现有能力盘点（不是本版要重做的事）

| 现有能力 | 本质 | 局限 |
|---|---|---|
| 全库笔记列表（pages/11） | 平铺浏览 + importance 筛选 | 无跨文档组织语义，文档只是过滤维度 |
| 证据篮 | 人工逐条收集 + 快照冗余 | 纯手工，无自动归集；条目是副本而非视图 |
| 项目 / 标签 | 人工打标分组 | 只关联到文档/页面，不聚合笔记与证据内容 |
| 搜索 | 关键词命中 | 会话态，无组织沉淀 |

### 16.2 v0.3.2 的"跨文档聚合"

定义（冻结）:

> 以既有的组织轴（项目、标签、importance）为入口，把分散在**多个
> document** 中的笔记与证据**动态聚合**为可浏览、可追溯的统一视图；
> 每个聚合条目保持到源 document/page 的可验证引用（引用，不是快照拷贝）。

它回答的问题是："关于这个主题，我跨所有资料积累了哪些笔记和证据？"——
而不是"把全库笔记再列一遍"。

判定一个功能是否属于本版聚合范围的标准：

1. 输入跨越 ≥2 个 document 的笔记/证据；
2. 分组轴来自已有关系数据（项目/标签/重要性），不是新建一套分类体系；
3. 输出是**视图**（可追溯引用），不是副本、不是物化行；
4. 删除源文档后条目自然消失（§18）。

### 16.3 明确不属于本版聚合的内容

- AI 自动聚类 / 自动主题发现 / 摘要生成；
- 跨文档笔记合并、去重、改写；
- 新的持久化组织实体（如"主题表")——本版不建。

## 17. Aggregation data sources（冻结）

聚合只允许基于以下现有数据源做**动态只读查询**:

- `notes`（四类笔记 + importance + 锚点，经 pages/documents 关联标题与
  页码）;
- `evidence_items` + `evidence_baskets`（已有跨文档结构与快照字段）;
- `tags` / `document_tags` / `page_tags`;
- `projects` / `project_documents` / `project_pages`。

冻结约束：

1. **不新增物化聚合表、不新增聚合缓存**。理由：删除 document 后由
   FK cascade 自然消失，避免维护派生存储，避免 stale aggregation rows;
   先验证产品语义，物化与否留给未来版本评估。
2. 聚合查询必须复用 `Database._connection()` 连接纪律。
3. 聚合读路径不得写任何表（含"最近访问"类隐式写）。
4. 性能门槛在 S3 设计细化时量化（预期数据规模下单次聚合查询应在
   交互可接受范围内；如需索引，作为独立决策记录）。

### 17.1 S3 纳入规则（R1 补充冻结）

1. **关联继承沿用搜索层既有 effective 语义，不是新增语义**。页面级搜索
   过滤早已把页面视为命中某 tag/project 当且仅当"页面直接关联 OR 所属
   document 直接关联"（`src/database.py` 搜索过滤与 facet 的
   `effective_tags`/`effective_projects` 先例）。聚合沿用同一规则：
   - document 级 note：仅由 `project_documents`/`document_tags` 直接
     关联命中；
   - page/text_selection/image_region note 与 evidence item：页面直接
     关联或其所属 document 直接关联均命中（与页面搜索一致）。
2. **evidence 无 importance**：importance 过滤为非 all 时 evidence 不
   出现；不伪造等级、不默认 normal。note_type 过滤同理只作用于 notes。
3. 统一条目模型只为浏览与排序；note 与 evidence 的差异保留
   （`note_type`/`importance` 对 evidence 为 None，`user_note`/
   `basket_id` 对 note 为空）。
4. 去重：同一 `(source_kind, source_id)` 在结果中只出现一次；轴匹配用
   EXISTS/IN 而非 JOIN 展开，从 SQL 层面消除多路径重复。
5. 排序：importance 优先（primary→secondary→normal，evidence 居末），
   再按时间倒序，再以 document/page/条目 identity 稳定收尾；
   不依赖数据库未定义顺序。
6. 聚合只读：不写任何表、不建新表、不复制 document_title 到任何
   持久化位置；删除 document 后结果自然消失（§18）。

## 18. Aggregation × deletion consistency model（冻结）

1. 聚合是纯动态查询 → 文档删除后其笔记/证据行已不存在 →
   聚合视图**结构性不可能**出现指向已删文档的条目。这是选择
   "不物化"的核心收益，冻结为架构不变量。
2. 删除的 impact preview 在聚合落地后必须能回答："该文档的内容当前
   出现在哪些聚合视角下"（例如：N 条笔记将出现在标签 T1/T2 的聚合
   视图中）。实现上复用 preview 的计数体系扩展，不新建统计管道。
3. 聚合 UI 中每条目点击跳转的目标（阅读页/笔记列表/证据篮）必须以
   数据库当前事实渲染；跳转目标在渲染与点击之间被删除的竞态，由目标页
   现有的"文档不存在"错误语义兜底，聚合层不做乐观缓存。
4. 测试锁定：删除后立即刷新聚合视图无残留、无异常（§20 场景 11）。

## 19. Planned implementation stages（固定顺序）

- **S0（已完成）**：独立 worktree + 分支 + 基线验证
  （pytest 899 项全绿、ruff 全绿）。
- **S1（本轮）**：本设计文档冻结。不改代码。
- **S2 删除加固**：
  - per-operation manifest + reconciliation state machine（§11–§14）;
  - 证据篮独立高风险确认 + 四类笔记分列 preview（§8、§9）;
  - `Database.delete_document` 处置（§15，先调用点审计）;
  - 危险操作文案升级为"永久删除导入文档及关联数据"。
  - 不修改：删除主流程六阶段骨架、schema、聚合任何内容。
  - 验收：既有删除测试全绿 + §20 场景 1–9、12–25 新增测试全绿；
    ruff 全绿；手工走查确认链。
- **S3 聚合数据层**：动态只读聚合查询服务（§16、§17），纯服务层 +
  测试。不改 UI、不写新表。验收：空库/单文档/多文档/删除后一致性
  测试全绿。
- **S4 聚合 UI**：聚合视图入口与条目跳转。不改数据层语义。
  验收：UI 测试 + 手工浏览验证。
- **S5 聚合 × 删除联动**：preview 聚合影响扩展（§18.2）+ §20 场景
  10–11 测试。
- **S6 回归与 release gate**：全量 pytest、ruff、
  `scripts/release_check.py`、手工测试计划与结果文档。

## 20. Test matrix（冻结）

删除机制测试场景（在既有 `test_document_deletion_service.py` /
`test_document_deletion_ui.py` 基础上扩展）:

**既有语义锁定：**

1. 删除没有任何笔记的文件；
2. 删除存在文档级笔记的文件；
3. 删除存在页面级笔记的文件；
4. 删除存在文字选区笔记的文件；
5. 删除存在图片区域笔记的文件；
6. 同一文件同时存在四类笔记（preview 分列计数断言）;
7. 删除取消（确认链任一环节未完成 → 零写入）;
8. 删除过程中发生异常（移动失败 / DB 失败 / 提交失败 → 回滚 + 文件复位）;
9. 删除后重新导入同名/同内容文件（新 document_id，无旧数据复活）;
10. 删除后搜索无残留命中（FTS 同步）;
11. 删除后跨文档聚合视图无残留条目（S5);
12. 删除后刷新/重启 Streamlit，UI 状态一致；
13. 删除后数据库重载（新连接）状态一致；
14. 两个不同内容文件同名 filename（互不影响）;
15. 源文件在 EKB 外部被人工移走后的删除（missing file 路径）;

**crash recovery 新增：**

16. crash after first file moved but before all files quarantined(W1);
17. crash after all files quarantined but before DB DELETE(W2);
18. crash during DB transaction before commit(W3);
19. crash immediately after DB commit(W4);
20. crash while destroying quarantine(W5);
21. corrupt manifest（fail-closed，保留现场 + warning）;
22. missing manifest（fail-closed）;
23. source path 与 quarantine path 同时存在文件（冲突 → fail-closed;
    内容一致 → 幂等完成）;
24. recovery executed twice（幂等，无二次副作用）;
25. multiple unfinished deletion operations coexist（独立处理，互不阻塞）。

另需覆盖：evidence_items > 0 时未完成独立确认则按钮禁用；
evidence_items = 0 时不显示额外确认项；裸连接 FK 关闭路径被
工程纪律排除（代码评审项，非自动化项）。

## 21. Future considerations（明确不做，仅记录）

- Trash / soft delete / Restore：若未来引入，§5–§14 的硬删除引擎是其
  "清空回收站"子集，本版冻结语义不成为障碍；
- detached archival notes：与 §7 的 citation-grounded 理由冲突，
  重启讨论需先推翻该论证；
- 物化聚合表 / 聚合缓存：待 S3–S5 实测性能后再评估；
- 批量删除：需先有多选影响预览与逐文档确认链设计；
- fail-closed 队列的人工处置 UI;
- evidence basket 单条删除的确认策略与其它入口对齐（观感问题，非本版）。

## 22. Explicitly frozen decisions（汇总）

1. 删除 = document lifecycle deletion，永久、单文档、多级确认、
   事务一致、quarantine 补偿。
2. 四类笔记随源文档删除；不保留 detached note（§7 论证冻结）。
3. evidence_items > 0 必须有独立高风险确认项；四类笔记 preview 分列计数。
4. `import_records` 保留（SET NULL）;tags / projects / evidence_baskets
   本体永不随文档删除。
5. crash recovery 以数据库事实为判定依据，manifest 为恢复数据载体；
   任何不明确状态 fail-closed；禁止启动即清空隔离区。
6. 业务级删除唯一入口为 document_deletion_service;
   裸 `Database.delete_document` 必须在 S2 消除其业务 API 外观。
7. 跨文档聚合 = 基于现有 notes/evidence/tags/projects 的动态只读视图，
   无物化表；删除后聚合条目结构性不可残留。
8. S2 目标零 schema 变更；任何 schema 变更须先修订本文件并获批准。
9. v0.3.2 不引入 Trash / soft delete / Restore / 批量删除 / detached
   notes / 物化聚合。
