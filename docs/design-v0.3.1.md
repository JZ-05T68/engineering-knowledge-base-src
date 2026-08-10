# Engineering Knowledge Base v0.3.1 设计文档 — 笔记语义等级与可自定义视觉映射

## 0. 文档状态

- 状态：**Design Freeze Ready（第二轮，待人工审核）**
- 依据：v0.3.0 已发布生产基线（commit `539c11a`，schema v5，
  `docs/design/v0.3.0-foundation-design.md` §3 将"重点等级"划归 v0.3.1）
- 修订记录：
  - R0（初稿）：首次设计冻结评审 → NOT YET APPROVED
  - R1（本轮）：按第一轮 Design Review 意见修订——
    P0（update API 默认参数隐式重置等级）已关闭；
    P1（v5 备份 restore contract）已完成代码审计并定案；
    D1 语义码改为 primary/secondary/normal；D2 保留等级筛选；
    P2 配色可读性契约修订；P2 索引经 EXPLAIN 实证保留；Q3 配色入口冻结。
- 本文件为 v0.3.1 实施的唯一设计依据；任何实现偏离须先修订本文件并获批准
- 本轮不实现功能代码、不建 migration、不改 schema、不动生产目录

## 1. Current v0.3.0 baseline

- 四类结构化笔记（document / page / text_selection / image_region）统一存于
  `notes` 表（schema v5），归属互斥与锚点组合合法性由 CHECK 约束保证
- 服务层单入口 `src/note_service.py`（全部 notes 写 SQL 仅在此三处：
  INSERT :652、UPDATE :666、DELETE :581）；四类 create 共用 `_insert_note`
- UI 两处入口：阅读页 tab（`src/note_ui.py`）与列表页（`src/note_list_ui.py`）
- 删除：单行 DELETE 单事务 + checkbox 确认；文档删除走
  `document_deletion_service.py` 双路径级联
- 选区重绑 / 区域重框：单事务多字段更新 + preview-confirm 流程
  （preview 与 execute 已绑定，`note_ui.py::_render_rebind_area`）
- 迁移模式（`src/migrations.py`）：`migrate_database` 统一入口、迁移前自动备份、
  单事务、`_core_data_fingerprint` 自证既有数据零改动、失败整体回滚、
  `schema_migrations` 记录版本、无 down-migration
- 备份与恢复（`src/backup_service.py`）：备份覆盖数据库文件 +
  data/raw + data/pages + data/markdown；恢复契约审计见 §10
- 当前库中**不存在**任何 importance / priority / color / preference 字段或机制

## 2. Problem statement

用户在一页/一文档上可积累多条笔记，但 v0.3.0 无法表达"这条比那条重要"。
需要三级业务语义（重点 / 次重点 / 一般），且：

- 等级是**业务语义**，必须作为稳定语义值持久化；
- 颜色只是 presentation，不得反过来用颜色当等级；
- 用户可自定义三级各自的显示颜色，偏好必须落在正式备份边界内；
- v0.3.0 旧笔记升级后必须有确定、合法的默认等级；
- 不得破坏删除、rebind、selection binding、级联等既有冻结语义；
- **既有调用点不得因新参数默认值而静默改写已有等级**（R1 新增约束）。

## 3. Goals

1. 四类笔记统一支持三级语义等级：创建可指定、编辑可修改、展示可见。
2. 语义值与颜色解耦：数据库存语义码，UI 经配置映射到颜色。
3. 用户可修改三级显示颜色并可一键恢复默认；偏好持久化且被正式备份覆盖。
4. schema v5 → v6 迁移：旧笔记默认等级「一般」，全程可回滚、可验证。
5. 列表页支持按等级筛选（单一维度、显式选择）。
6. 测试与 Release Gate 达到 v0.3.0 同等强度。

## 4. Non-goals（冻结排除）

- AI 自动判级 / AI 摘要 / AI 标签 / LangGraph / 工作流
- 跨文档聚合、导出增强、复杂搜索系统、全库自动优先级排序
- 超过三级的等级体系、图标体系扩展
- **批量改级**、多选状态机（本期不做）
- **按等级排序**（排序保持 updated_at 倒序）
- 笔记全文检索、等级进入 page_search FTS
- 跨 schema restore 能力扩展（见 §10 定案）
- 为外部比赛临时加与本主题无关的功能

## 5. Current architecture findings（审计结论）

1. `src/models.py::Note` 为 frozen dataclass，字段与 `notes` 列一一对应；
   增加字段只需在 dataclass 加带默认值的成员，不影响既有构造点之外的调用。
2. `note_service.py` 集中度高：`NOTE_COLUMNS`、`_note_from_row`、
   `_insert_note`、`_apply_update` 是等级支持的全部触点。
3. **写路径全量清单**（R1 复核，用于证明无隐式重置路径）：
   - 创建：`_insert_note`（四个 create_* 共用）；
   - 更新：`_apply_update`（调用方：`update_document_note`、
     `update_page_note`、`update_image_region_note`（三者经 `_update_personal`）、
     `update_text_selection_content`、`rebind_text_selection`、
     `rebind_image_region`）；
   - 删除：`delete_note`（单行 DELETE）；
   - 级联删除：数据库外键（不经服务层 UPDATE）。
   `_apply_update` 只写入调用方给出的 assignments——只要 update 系列在
   `importance is None` 时不把 importance 放进 assignments，就不存在任何
   隐式重置路径；rebind/reframe 的 assignments 清单固定为锚点字段，
   结构上不可能触碰 importance。除上述外全仓无其它 notes 写者。
4. 列表页查询（`list_note_summaries` / `count_notes` / `_list_filters`）
   是"过滤条件集中组装"，加等值过滤为小步扩展。
5. 文档删除预览按 note_type 计数，不读等级；级联删除与等级无关，
   `document_deletion_service.py` 原则上零改动。
6. 备份边界只含数据库 + 三个资料目录；任何放在 `data/` 根的
   JSON 偏好文件都**不在备份内**——这是偏好存数据库的决定性论据。
7. 迁移 `_core_data_fingerprint` 只含既有表，v6 迁移延续同一自证模式。

## 6. Data model design（schema v6 目标）

### 6.1 notes 表新增一列

```sql
ALTER TABLE notes ADD COLUMN importance TEXT NOT NULL DEFAULT 'normal'
    CHECK (importance IN ('primary', 'secondary', 'normal'));
```

- 语义码（冻结，D1）：`'primary'` = 重点，`'secondary'` = 次重点，
  `'normal'` = 一般。不采用 `'key'`：与 database key / primary key /
  widget key / session key 等工程概念冲突过多，不适合作为长期业务枚举
- SQLite 允许带**常量默认值**的 ADD COLUMN；旧行逻辑值即 'normal'，
  无需表重写、无需逐行 UPDATE
- 默认等级（冻结）：旧笔记与未指定的新笔记一律 `'normal'`（一般）

### 6.2 偏好表（新建）

```sql
CREATE TABLE note_display_preferences (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    color_primary   TEXT NOT NULL DEFAULT '#c0392b'
                    CHECK (color_primary   GLOB '#[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    color_secondary TEXT NOT NULL DEFAULT '#b8860b'
                    CHECK (color_secondary GLOB '#[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    color_normal    TEXT NOT NULL DEFAULT '#5a6570'
                    CHECK (color_normal    GLOB '#[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    updated_at TEXT NOT NULL
);
INSERT INTO note_display_preferences (id, updated_at) VALUES (1, <迁移时刻>);
```

- 单行表（`CHECK (id = 1)`），默认值即内置默认配色；
- 颜色规范化（冻结）：服务层写入前统一转为**小写** `#rrggbb`；
  数据库 CHECK 只接受小写十六进制，作为第二层不变量；
- 存数据库的决策理由：① 在正式备份边界内；② 迁移/回滚随库走；
  ③ 本地优先单数据源原则，不引入新的文件格式与并发面。

### 6.3 索引（R1 实证后保留）

```sql
CREATE INDEX idx_notes_importance ON notes(importance, updated_at DESC);
```

保留依据（EXPLAIN QUERY PLAN 实证，12 000 行合成数据、三级分布 10/20/70%）：

- 无索引：`SCAN notes` + `USE TEMP B-TREE FOR ORDER BY`；
- 有索引（ANALYZE 后）：`SEARCH notes USING INDEX idx_notes_importance (importance=?)`，
  过滤路径从全表扫描变为索引搜索；真实列表查询（含 pages JOIN 与
  `ORDER BY notes.updated_at DESC, notes.id DESC`）同样命中该索引做过滤；
- 已知边界：ORDER BY 含 `id DESC`（不在索引内），排序的"right part"仍需
  临时 B 树——索引的价值是**消除全表扫描**而非消除排序；
- 该索引服务于 §11.4 的等级筛选查询形态（`WHERE notes.importance = ?`
  + updated_at 排序 + LIMIT/OFFSET 分页）；不建任何等级相关唯一约束。

## 7. Semantic-level representation

- 数据库只存 §6.1 三个语义码；代码内以 `NoteImportance(StrEnum)` 表达：
  `PRIMARY="primary"`（重点）、`SECONDARY="secondary"`（次重点）、
  `NORMAL="normal"`（一般），`label` 属性给出中文标签
  （仿 `NoteType.label` 既有模式）
- UI 展示 = 中文文本徽章 + 颜色：「重点」「次重点」「一般」文字必须始终出现，
  **禁止仅靠颜色区分**（详见 §8 可读性契约）
- 任何导出/提示文案（删除确认、文档删除预览）不引入等级语义变化

## 8. Color mapping / preference design（R1 修订的可读性契约）

冻结原则：

1. importance 的**文本标签始终存在**，颜色永远不是唯一语义载体；
2. 用户只控制三级 badge 的**背景色**；
3. **前景文字颜色由 UI 根据背景色自动选择**，至少在深色/浅色文字间
   自动切换（按 WCAG 相对亮度阈值计算，纯函数，输入为校验后的
   `#rrggbb`，输出为内置前景常量之一，如 `#1a1a1a` / `#ffffff`）；
4. 配色变化不修改任何业务数据（notes 行不变，见 §16.2 专项测试）；
5. 动态 HTML/CSS 只能使用 service 校验并规范化后的 `#rrggbb` 值；
6. preference 写入时在 service 层校验颜色格式并规范化为小写；
   数据库 CHECK（§6.2）作为第二层不变量；
7. 颜色字符串规范化冻结为**小写**。

读写 API：

- `NoteService.get_display_preferences()` → 单行读取；行缺失（防御）时返回
  内置默认并记录 warning，不阻断页面（与既有降级纪律一致）；
- `NoteService.update_display_preferences(color_primary, color_secondary,
  color_normal)` → 校验 + 小写规范化，单语句 UPDATE 单事务，
  `updated_at` 刷新；非法值抛 `NoteValidationError`，UI 中文报错、无假成功；
- 恢复默认：UI「恢复默认配色」按钮调用同一方法写回三个默认常量。

入口（冻结，Q3 关闭）：**仅列表页「结构化笔记」顶部「显示设置」折叠区**
（三个 `st.color_picker` + 保存 + 恢复默认）。阅读页只消费展示，
不在任何其它页面增加第二配置入口或额外只读提示。

徽章渲染：`st.markdown(..., unsafe_allow_html=True)` 输出带背景色与
自动前景色的 span（有 `pages/3_检索资料.py` 样式注入先例）。

## 9. Migration strategy（v5 → v6）

完全沿用 v5 迁移形态（`src/migrations.py::_apply_version_six`）：

1. 前置：`current_version == 5` + 既有自动备份成功；
2. 单事务 `BEGIN IMMEDIATE`：
   - `ALTER TABLE notes ADD COLUMN importance ...`（§6.1）
   - `CREATE TABLE note_display_preferences ...` + 插入默认行（§6.2）
   - `CREATE INDEX idx_notes_importance ...`（§6.3）
   - `INSERT INTO schema_migrations(version, applied_at) VALUES (6, ?)`
3. `_core_data_fingerprint` 前后一致，否则 `MigrationError` 回滚；
4. 提交后 `PRAGMA foreign_key_check`（既有 `migrate_database` 尾检）；
5. 失败：整体回滚，原库与自动备份双保留；无 down-migration；
6. 降级路径（冻结，仿 v0.3.0 §19 口径）：schema v6 的库 v0.3.0 拒绝打开；
   降级 = 用 v0.3.0 恢复升级前自动备份；降级丢失 v0.3.1 的等级与配色设置，
   该事实写入升级提示与运行说明。

## 10. v5 backup restore contract（R1 审计结论与定案）

### 10.1 代码审计事实（v0.3.0 基线）

- `validate_backup()`（`src/backup_service.py:651-655`）对 schema 版本做
  **严格相等**校验：`manifest.schema_version != expected_schema_version`
  即拒绝；调用方三处（维护页恢复预检 `pages/10_系统维护.py:112`、
  离线恢复 `scripts/restore_backup.py:117`、内部 `_validate_layout` 路径）
  全部传 `SCHEMA_VERSION` 常量；
- 应用版本则宽松：`_is_compatible_patch_backup` 接受同 major.minor 系列的
  更早补丁版本；**schema 版本从无跨版本恢复先例**；
- v0.2.4 → v0.3.0 即按同一契约执行（"Schema 5 程序拒绝直接恢复
  Schema 4 备份"，v0.3.0 设计 §19 冻结）；
- restore 为离线操作（正式恢复要求服务停止），迁移发生在
  `Database.__init__` → `migrate_database` 的启动路径上。

### 10.2 问题 A：技术上能否 restore v5 → 自动迁移 v6？

**可以。** restore 本身是文件级恢复 + 路径重定位，启动时
`migrate_database` 会自动把 v5 库迁移到 v6（含迁移前自动备份）。
唯一的硬性拦截是 `validate_backup` 的 schema 相等守卫——
这是**有意设计的同 schema 恢复契约**，不是技术限制。

### 10.3 定案（选项 C）

**v0.3.1 维持同 schema 恢复契约：不直接恢复 v5 备份。**

- 性质说明：这是 v0.2.x → v0.3.0 以来**既有 restore contract 的延续**，
  不是 v0.3.1 新增限制；跨 schema restore（§10.2）属于 restore 契约的
  功能性扩展，超出本期范围，如未来需要应单独立项设计
  （含校验放宽、恢复后自动迁移、配套测试）；
- 对向后兼容的影响（如实表述，**不宣称完全兼容旧备份**）：
  v5 正式备份数据零丢失、零格式破坏，但需要一次两阶段操作才能回到服务；
- 用户操作路径：① 用 v0.3.0 实例/恢复脚本恢复 v5 备份
  （schema 相等，契约内）；② 再由 v0.3.1 启动完成 v5→v6 自动迁移；
- Release Gate 验证：专项测试断言 v0.3.1 的 restore 路径对 v5 备份给出
  明确中文拒绝（不误报成功、不产生半个恢复现场）；
  升级演练覆盖"v0.3.0 恢复 → v0.3.1 迁移"两阶段路径。

## 11. UI interaction design

### 11.1 创建（阅读页 tab，四类一致）

- 每个创建表单增加 `st.selectbox("重要程度", options=三级, index=2（一般）)`，
  key 复用既有作用域规则（`note_create_imp_{scope}_{owner_id}` 等）；
  保存成功后随既有 `_queue_key_clear` 复位到「一般」。

### 11.2 展示

- 阅读页卡片与列表页卡片的 caption 行追加等级徽章（§8 契约）；
- 徽章为纯展示，不是按钮、不提供快捷改级。

### 11.3 编辑等级

- 每条笔记的编辑区增加等级 selectbox，默认当前值；
- 「保存修改」一次性提交 内容+等级（沿用既有单事务 UPDATE 语义；
  **未改动的等级字段不写入**——见 §12.2 契约）；
- 等级修改刷新 `updated_at`（与既有 update 语义一致；
  已知副作用：列表按 updated_at 排序，改等级会把笔记顶到最前——接受并记录）。

### 11.4 列表页筛选（D2 冻结保留）

- 类型筛选旁增加「等级筛选」selectbox：全部等级 / 重点 / 次重点 / 一般；
- 与文档、类型筛选 AND 组合；分页签名（`_FILTER_SIG_KEY`）纳入等级维度，
  切换筛选重置到第 1 页；空筛选结果显示既有空态文案
  （"当前筛选条件下没有结构化笔记。"）；
- 明确排除：等级排序、自动优先级排序、批量改级、多选状态机。

### 11.5 配色设置

- 列表页「显示设置」折叠区（§8）；保存/恢复默认后 `st.rerun`，
  全列表徽章即时生效（含自动前景色）。

## 12. Service/API changes（R1 修订后的 create/update 契约）

### 12.1 CREATE 契约

四个 `create_*` 增加可选参数 `importance: str = "normal"`：

- 未指定 → `'normal'`（一般）；
- 显式指定 → 经 `_validate_importance` 校验后落库；
- 非法值 → `NoteValidationError`，不产生任何写入。

### 12.2 UPDATE 契约（P0 修订，冻结）

`update_document_note` / `update_page_note` / `update_image_region_note` /
`update_text_selection_content` 增加参数 **`importance: str | None = None`**：

- `importance is None` → **保留数据库中的已有等级**
  （importance 不进入 UPDATE assignments，业务含义固定为
  "preserve existing importance"）；
- 显式指定（含从 primary/secondary 改回 'normal'）→
  校验后与新内容同事务同语句写入；
- 非法值 → `NoteValidationError`，**整个 update 回滚，笔记所有字段不变**
  （不是"仅 importance 不变"）；
- **禁止**把 update 的默认参数写成 `"normal"`（P0 根因：
  legacy-style 调用会静默把已有等级重置为 normal）。

### 12.3 无隐式重置路径证明

依据 §5.3 写路径全量清单：UPDATE 仅经 `_apply_update`，
其 SQL assignments 完全由调用方显式构造；§12.2 契约下
`importance is None` 不产生 assignment；rebind/reframe 的 assignments
固定为锚点七/八字段，结构上不含 importance；DELETE 与级联不写行。
结论：除显式指定外不存在任何改写 importance 的路径。

### 12.4 其它服务变更

1. `_validate_importance(value)`：非 str / 非三值 → `NoteValidationError`；
2. `get_display_preferences()` / `update_display_preferences(...)`（§8）；
3. `list_notes / list_note_summaries / count_notes / _list_filters`：
   增加 `importance: NoteImportance | str | None` 等值过滤
   （非法值抛 `NoteValidationError`，与 `note_type` 过滤同形）；
4. `NOTE_COLUMNS`、`_note_from_row`、`_insert_note`：加 `importance`；
5. `src/models.py`：`NoteImportance` 枚举；`Note.importance: str = "normal"`；
   偏好 dataclass `NoteDisplayPreferences`（frozen, slots）；
6. 徽章前景色纯函数（按 §8.3 亮度阈值），可独立单测。

明确不改：`note_geometry.py`、`document_deletion_service.py`、
`pdf_service / ocr / evidence / search`、`database.py`、
`pages/2_浏览资料.py` 与 `pages/11_结构化笔记.py`。

## 13. State-management implications

- 等级 selectbox 全部使用显式 key，沿用 `_queue_key_clear` /
  `_apply_pending_key_clears` 复位模式；无新增跨 rerun 状态；
- 编辑表单等级默认值来自当次渲染读取的 `note.importance`，
  等级变化计入「● 有未保存修改」dirty 判定；
- rebind/reframe 流程不读不写等级，preview-execute 绑定语义不受影响；
- 列表页筛选签名含等级，避免切筛选后页码越界（既有保护已覆盖）。

## 14. Error semantics

- 非法等级：`NoteValidationError` + 中文提示，整个写操作回滚，无假成功；
- 非法颜色：同上（`color_picker` 前端先约束一层）；
- 偏好行缺失/异常：读取方返回内置默认并记 warning，不阻断页面；
- 迁移失败：见 §9.5；升级后 `foreign_key_check` 必须 0 违规；
- v5 备份恢复：明确中文拒绝，无假成功、无半个恢复现场（§10.3）。

## 15. Data invariants

1. `notes.importance ∈ {'primary','secondary','normal'}` 恒成立
   （服务层 + CHECK 双保证）；
2. legacy-style update（不传 importance）后等级与库中原值恒等
   （§12.2/§12.3，自动化测试钉死）；
3. 颜色偏好恒为小写合法 `#rrggbb` 三值；缺行时逻辑默认；
4. 等级/配色写路径不得触碰 pages 文本列、PDF、PNG、Markdown、
   evidence_items（迁移有指纹自证）；
5. 重复笔记语义不变：同来源多条笔记可各自独立设级，无唯一约束；
6. 删除任何笔记/文档不得留下等级相关残留（等级随行删除，无外部引用）；
7. 配色变更不产生任何 notes 行变更。

## 16. Testing plan（R1 更新）

### 16.1 服务层（tests/test_note_service.py 扩展）

- 四类 create：默认 = 一般；显式三级落库；非法等级拒绝；
- **P0 契约专项（冻结，四类参数化）**：
  1. primary 笔记 → legacy-style update 不传 importance → 仍为 primary；
  2. secondary 笔记 → legacy-style update 不传 importance → 仍为 secondary；
  3. 显式传 'normal' → 可从 primary/secondary 改回 normal；
  4. 非法 importance → 整个 update 回滚，笔记所有字段（含内容）原样不变；
- rebind / reframe 后等级保持不变（选区、区域各一）；
- 删除：带等级笔记删除正常；同页其他笔记等级不受影响；
- 文档删除级联：带三级笔记的文档删除后无孤儿、fk_check 通过；
- 写失败注入（`_fail_write_commits` 模式）：改等级回滚零部分写入；
- 偏好：默认行存在；改色成功并规范化为小写；非法颜色拒绝；
  恢复默认；缺行读取回退默认。

### 16.2 配色契约专项（R1 新增）

- 极亮背景（如 `#ffffff`）→ badge 前景自动为深色；
- 极暗背景（如 `#000000`）→ badge 前景自动为浅色；
- 前景色选择为纯函数单测（阈值两侧、恰在阈值）；
- 非法颜色（`red`、`#fff`、`#gg0000`、大写未规范化输入）行为符合契约
  （大写合法输入被规范化为小写，非法格式被拒绝）；
- **修改配色不修改任何 Note 行**（改色前后 notes 全表逐行相等）；
- preference row 缺失时安全退回默认值。

### 16.3 迁移（仿 test_schema5_notes 新增 schema6 套件）

- v5→v6 既有数据指纹零变化（含四类完整锚点样本，含三级样本行）；
- 旧行等级 = 一般；失败注入整体回滚（版本停在 5、新列/新表/新索引不存在）；
- 迁移前自动备份保留；integrity / fk 检查通过；
- v0.3.1 restore 路径对 v5 备份明确中文拒绝（§10.3）。

### 16.4 UI（AppTest 各文件扩展）

- 创建：默认等级、指定等级、保存后复位；
- 展示：三徽章文本出现、配色生效、改配色后全列表即时生效
  （含前景自动切换的可断言输出）；
- 编辑：改等级保存、不传等级的保存保留原等级、取消保留、
  失败保留草稿；dirty 标记含等级变化；
- 筛选：等级×文档×类型组合计数正确、分页重置、空态文案；
- 显示设置：保存、恢复默认、非法值提示、唯一入口
  （阅读页无配色编辑控件）；
- 回归：v0.3.0 全部既有测试不得改动语义地通过。

### 16.5 人工门禁（v0.3.1 §gate，仿 v0.3.0 §18）

升级演练（v0.3.0 生产副本 → v0.3.1 迁移 → 旧笔记显示「一般」）、
四类设级、legacy 编辑不改级、显式改级、配色修改/恢复默认、
极亮极暗配色可读性、筛选、重启持久化、降级路径文案、
v5 备份两阶段恢复演练。

## 17. Release Gate

与 v0.3.0 同构：Ruff 全量、pytest 全量（新增用例一并计入）、
AppTest、迁移/删除/选区/区域/配色专项、v0.3.0 回归、
正式 `release_check.py`（版本收口时 EXPECTED_VERSION 升 0.3.1）、
§10.3 restore 契约专项、人工门禁逐项记录后 PASS / FAIL / INCOMPLETE。

## 18. Implementation phases（固定顺序）

1. `models.py`：`NoteImportance`、偏好 dataclass、`Note.importance`、
   前景色纯函数；
2. `migrations.py`：`_apply_version_six`（§9）+ `SCHEMA_VERSION = 6`；
3. 服务层（§12 全部）及 16.1/16.2/16.3 自动化测试；
4. 阅读页 UI（创建/编辑/徽章）及 AppTest；
5. 列表页 UI（徽章/筛选/显示设置）及 AppTest；
6. 全量回归 + 人工门禁 + 版本收口（config/release_check/CHANGELOG/README）；
7. 发布与部署（仿 v0.3.0 蓝绿流程；current 入口仅切转发目标）。

## 19. Risks / open questions

- R1 配色可访问性：用户可任选背景色，但前景自动切换（§8.3），
  文本标签始终在场——按修订后契约关闭；
- R2 改等级刷新 `updated_at` 改变列表顺序 → 已记录为接受行为；
- R3 索引价值边界：仅消除全表扫描、不消除排序（§6.3 已实证并记录）；
- Q1（语义码命名）：**已关闭**，冻结为 primary/secondary/normal（D1）；
- Q2（列表筛选去留）：**已关闭**，保留单维等值筛选（D2）；
- Q3（配色入口位置）：**已关闭**，仅列表页「显示设置」（§8）；
- 当前无剩余 open questions。

## 20. Explicit frozen decisions（R1 更新版）

1. 三级语义码：`'primary'`（重点）/ `'secondary'`（次重点）/ `'normal'`（一般）；
2. 默认等级：一律「一般」（旧笔记 + 未指定新笔记）；
3. 数据库只存语义码；颜色仅为 presentation；徽章必含中文文本；
4. CREATE：`importance: str = "normal"` 可选参数；
   UPDATE：`importance: str | None = None`，**None = 保留已有等级**；
   非法值整个 update 回滚；禁止 update 默认参数为 "normal"；
5. 配色偏好存数据库单行表 `note_display_preferences`（正式备份边界内）；
   颜色规范化为**小写** `#rrggbb`；service 校验 + DB CHECK 双层不变量；
6. 用户只控背景色；前景文字颜色按背景亮度自动深/浅切换（纯函数）；
   动态 CSS 只使用校验后值；
7. schema v5 → v6：一列 + 一表 + 一索引 + schema_migrations，
   单事务、迁移前备份、指纹自证、失败回滚、无 down-migration；
8. `idx_notes_importance` 经 EXPLAIN 实证保留（§6.3 查询形态与边界）；
9. v5 备份 restore：维持既有同 schema 契约，v0.3.1 不直接恢复 v5 备份；
   用户路径 = v0.3.0 恢复 → v0.3.1 迁移；不得宣称"完全兼容旧备份"；
10. rebind / reframe / 删除 / 文档级联 / 证据 / FTS：零改动；
11. 本期包含列表页等级筛选；排除等级排序、自动优先级排序、
    批量改级、多选状态机；
12. 配色编辑入口仅列表页「显示设置」；阅读页只消费展示，无第二入口；
13. 修改等级刷新 `updated_at`；列表排序保持 updated_at DESC；
14. 不实现 §4 列出的任何排除项；不因"顺手"扩范围。
