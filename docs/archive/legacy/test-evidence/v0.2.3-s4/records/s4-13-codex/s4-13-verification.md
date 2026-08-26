# Engineering Knowledge Base v0.2.3 S4-12 / S4-13 核验记录

- 执行工具：Codex
- 执行日期：2026-07-29（Asia/Shanghai）
- S4-12 独立审核：`APPROVED PASS`
- S4-13 最终结论：`FAIL`
- 严重级别：`P0`
- S4-14：`NOT STARTED / BLOCKED`

## A. S4-12 独立审核

- 原始记录目录：`D:\ekb-s4\v0.2.3-s4-20260727\records\s4-12-kimi`
- 版本门禁：分支 `kimi/v0.2.3-large-manual-test`，HEAD `97545312c73861fab24a0dab9f910b0c6d322a7d`，工作树干净；8501～8512 无监听。
- D06：`invalid-content.pdf`，135 bytes，SHA-256 `3a8ef01046249585b41bcc7cb3b06009ebadcf81e6385d384c09cf67d883c1ed`，前 16 字节 `53342d313220494e56414c4944205044`；普通 UTF-8 文本，不以 `%PDF-` 开头。
- errors raw 副本与 D06 同大小、同哈希，位于 errors 隔离目录；未覆盖 inputs，也未进入 core 或正式目录。
- UI 证据：文件名显示正确；导入记录明确显示失败、0 页和底层错误；浏览入口仍可用，无成功提示、白屏或 app.exception。
- errors 数据库：documents=1（failed，page_count=0，错误非空），import_records=1（failed，错误非空），pages=0，page_search=0；无 processing、重复失败记录、孤立页面或孤立 FTS。
- 文件系统：PNG=0，Markdown=0，本轮备份=0；仅保留 invalid-content raw；日志包含同一时刻的无效 PDF 错误，无持续重试。
- `PRAGMA integrity_check=ok`，`PRAGMA foreign_key_check=[]`。
- P3 维持：UI 暴露 errors 隔离路径和 PyMuPDF 文本，但没有 traceback、秘密或正式资料路径；应用仍可继续使用。
- core：documents/pages/page_search=1/8/8，page_id=4 仍为 reviewed，Markdown 为 B 版本，标签/项目/关系不变，evidence_items=1；完整性正常。
- 正式数据和备份时间戳均早于本轮，8501 未使用。
- S4-12 正式关闭，结论：`APPROVED PASS`。

## B. S4-13 执行结果

### 版本、环境与样本

- 工作区：`D:\ekb-v0.2.2`
- 分支/HEAD：`kimi/v0.2.3-large-manual-test` / `97545312c73861fab24a0dab9f910b0c6d322a7d`
- errors：`D:\ekb-s4\v0.2.3-s4-20260727\runs\errors`
- 实际监听：`127.0.0.1:8502`；8501 未使用。
- 11 个路径型 `EKB_*` 变量全部指向 errors 的 data/raw/pages/markdown/database/backups/logs/runtime。
- D01：`D:\ekb-s4\v0.2.3-s4-20260727\inputs\ordinary-8.pdf`
- D01：4625 bytes，8 页，SHA-256 `5e0c902ef526564b103c1a5916232f6387a9e55aac77b7d72546db296c881fa7`，前 16 字节 `255044462d312e370a25c2b5c2b60a25`；与 core raw 完全一致。

### 初始基线

- documents=1（failed=1）
- pages=0，page_search=0
- import_records=1（failed=1）
- raw=1（invalid-content）
- PNG=0，Markdown=0
- tags/projects/全部关系/evidence_items 均为 0
- 数据库 176128 bytes，SHA-256 `7d338d423f6933d34f40f7acdcee52abf2f261e322bebe79275d8bb26df02b6f`
- `integrity_check=ok`，`foreign_key_check=[]`

### 首次导入

- UI 原文：`导入完成。ordinary-8：共 8 页，已处理 8 页，文本页 8 页，待复核 8 页。`
- 结果：新增 completed 文档 id=2；新增 pages=8、page_search=8、PNG=8、ordinary-8 raw=1、completed import record=1。
- 页面 id 为 1～8，PDF 页码为 1～8；PNG 均非空，raw 哈希与 D01 一致；无 processing。
- 第 1 页和第 8 页均通过产品入口正常打开。
- S4-12 failed 文档和 failed import record 未改变。

### 第二次导入与零增量核验

- UI 重复提示原文：`该文件已经导入：ordinary-8（编号 2），未生成重复数据。`
- documents：2 → 2
- pages：8 → 8
- page_search：8 → 8
- raw：2 → 2
- PNG：8 → 8
- Markdown：0 → 0
- tags/projects/全部关系/evidence_items：均 0 → 0
- ordinary-8 文档 id、页面 id 1～8、页面状态、updated_at、extracted_text、FTS、raw/PNG 路径、大小、mtime 和 SHA-256 全部不变。
- 没有第二个同 SHA-256 文档，没有第二套页面或 PNG，没有覆盖或重新渲染。
- **import_records：2 → 3，不满足零增量。**
- 新增 id=3：filename=`ordinary-8.pdf`，status=`completed`，document_id=2，total_pages=8，error_message=`该文件已经导入`。
- 因规范明确将“新增 import record”列为 FAIL，且这是持久业务数据污染，判定 `P0`；发现后立即停止 I 组，不做修复、不复测、不继续代表性搜索。

### 保护、完整性与结束状态

- errors 最终：documents=2（failed=1、completed=1），pages=8，page_search=8，import_records=3；无 processing、孤立 page 或孤立 FTS。
- errors：`integrity_check=ok`，`foreign_key_check=[]`。
- S4-12 failed 文档、错误、raw 和 failed import record 保持。
- core 数据库 SHA-256 前后均为 `e37abdbde081752313e2d8bc8b146db567c2704eea6b3aa1c139d9429ae9f67c`；S4-09～S4-11 关键计数和内容不变。
- 正式数据库最后修改时间仍为 2026-07-26 07:55:17，正式备份最新时间仍为 2026-07-26 08:56:47。
- 浏览器已关闭；本轮 Streamlit PID 已停止；8501～8512 无监听；无本轮 Python/Streamlit 残留。
- 最终 Git 工作树干净，分支和 HEAD 不变；无 commit、merge、push 或 tag。
- 未修改代码、配置、测试、迁移或项目文档；因此未运行 ruff 或 pytest。
- 未出现修复—复测原地打转。
- S4-14 保持 `NOT STARTED / BLOCKED`。
