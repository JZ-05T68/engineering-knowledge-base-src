# S4-11 Codex 人工测试核验记录

- 执行时间：2026-07-29 17:00–17:12（Asia/Shanghai）
- 结论：PASS
- 严重级别：无 P0/P1/P2/P3；有 3 条非阻塞 OBSERVATION
- S4-12：允许进入，但保持 NOT STARTED

## 版本门禁

- 工作区：`D:\ekb-v0.2.2`
- 分支：`kimi/v0.2.3-large-manual-test`
- HEAD：`97545312c73861fab24a0dab9f910b0c6d322a7d`
- 开始与结束 `git status --short`：均为空

## 隔离环境

- core：`D:\ekb-s4\v0.2.3-s4-20260727\runs\core`
- 数据库：`D:\ekb-s4\v0.2.3-s4-20260727\runs\core\data\database\knowledge.db`
- 实际监听：`127.0.0.1:8502`
- `EKB_DATA_DIR`、`EKB_RAW_DIR`、`EKB_PAGES_DIR`、`EKB_MARKDOWN_DIR`、`EKB_DATABASE_DIR`、`EKB_DATABASE_PATH`、`EKB_BACKUPS_DIR`、`EKB_LOGS_DIR`、`EKB_LOG_PATH`、`EKB_RUNTIME_DIR`、`EKB_PID_PATH` 全部指向 core。
- 8501：开始、执行和结束均无监听；正式数据未接触。

## 前置保护与基线

- `documents=1`、`pages=8`、`page_search=8`
- PDF 为 8 页，SHA-256：`5e0c902ef526564b103c1a5916232f6387a9e55aac77b7d72546db296c881fa7`
- 8 张 PNG 均存在。
- S4-09：page_id=4 为 reviewed；Markdown 包含 B 标记、不含 A 标记；文件 SHA-256：`652399abc8bbe84a076aa304d20ff5b28aaa30f8b59e36265eae716c7a054d7a`
- S4-10：文档标签 `S4-10-DOC-TAG-20260729` 存在；项目 `S4-10-PROJECT-20260729` 为 active；`document_tags=1`、`page_tags=0`、`project_documents=0`、`project_pages=1`，唯一页面项目关系为 project_id=1 → page_id=6，page_id=5 未串入。
- 默认证据篮：id=1，名称 `默认证据篮`；初始 `evidence_items=0`。

## 目标与搜索

- 搜索词：`S4-ORDINARY-8-000006`
- 目标：document_id=1、page_id=6、PDF 第 6 页
- 精确选区：`SCALE S4-ORDINARY-8 PAGE 6 TOKEN S4-ORDINARY-8-000006`
- extracted_text SHA-256：`953446b146c01976764c268fafc6b17e087b380a84ca3b7aefac11369edd0510`
- PNG SHA-256：`337a661b73ef35c8e0719b67baac0cd1938479b243a26b0d35fb3a2885617455`
- UI 返回 8 个页面，目标页按相关度排第 1；按测试边界只操作该目标结果。
- 目标卡显示 ordinary-8 第 6 页、待处理、页面项目和文档标签正确，命中片段包含预期原文。

## 加入证据篮

- UI 反馈：`证据已持久化加入证据篮。`
- 篮子计数：0 → 1
- 新增 item id=1，basket_id=1，document_id=1，page_id=6，page_number=6，position=1
- `text_kind=original_material`，不是 `user_excerpt`
- `source_text_sha256=953446b146c01976764c268fafc6b17e087b380a84ca3b7aefac11369edd0510`
- `selection_sha256=b66f8c39e631e33f058045b1e0fde47fc584bdc79c77baa4faff94a2d979ca51`
- source locator 包含 document_id=1、page_id=6、page_number=6 和正确 PDF SHA-256。
- 初始备注经产品既有 NFKC 规则落库为 `S4-11 初始备注:来自 ordinary-8 的代表性原始材料。`

## 证据篮与备注更新

- UI 显示 1 条 evidence，ordinary-8 第 6 页，可信度 `已匹配原始材料`，项目正确；标签处显示的是直接页面标签，因此为 `未添加标签`，与 `page_tags=0` 一致。
- UI 反馈：`证据备注已保存。`
- item id 保持 1，条目数保持 1。
- 最终备注经 NFKC 落库为 `S4-11 最终备注:原始材料、来源页和证据包回溯测试。`
- selection、document_id、page_id、text_kind、source locator、hash、position 和 added_at 均未改变。

## Markdown 证据包

- 生成方式：证据篮 UI 预览并点击 `保存 Markdown 文件`
- 文件：`D:\ekb-s4\v0.2.3-s4-20260727\records\s4-11-codex\engineering-evidence-package.md`
- 大小：1453 bytes
- 编码：UTF-8
- SHA-256：`de030fa610d96ca1169e26353c134e875016a46dc04cc665fa58fcbd285e67ad`
- 生成时间：`2026-07-29T17:08:14+08:00`
- 内容包含正确文档、文件名、第 6 页、项目、pending 状态、来源定位、PDF/PNG 路径、精确选区、原始材料可信度、最终备注和系统上下文；只包含 1 条目标证据。

## 来源回跳与再次进入

- 通过 `返回原始页` 进入 ordinary-8 第 6 / 8 页（PDF 页码 6）。
- 页面原图可见，图中文字含唯一 token；状态待处理、Markdown 为空。
- 文档标签仍为 `S4-10-DOC-TAG-20260729`；页面项目仍为 `S4-10-PROJECT-20260729`。
- 再次进入证据篮后仍只有 item id=1；最终备注、选区、页码和来源类型保持，无重复条目或旧 session 串位。

## 最终不变量与完整性

- `documents=1`、`pages=8`、`page_search=8`
- `tags=1`、`projects=1`、`document_tags=1`、`page_tags=0`、`project_documents=0`、`project_pages=1`
- `evidence_baskets=1`、`evidence_items=1`
- PDF SHA-256 未变：`5e0c902ef526564b103c1a5916232f6387a9e55aac77b7d72546db296c881fa7`
- page_id=6 PNG SHA-256 未变：`337a661b73ef35c8e0719b67baac0cd1938479b243a26b0d35fb3a2885617455`
- page_id=6 extracted_text SHA-256 未变：`953446b146c01976764c268fafc6b17e087b380a84ca3b7aefac11369edd0510`
- page_id=6 仍为 pending、Markdown 为空、updated_at 不变。
- page_search 内容与 pages 的三个搜索镜像列逐行一致；内容 SHA-256：`3010feba7e3022d14625f30a200573ac5d820261e922ee276fe5746b85981e56`
- `integrity_check=ok`
- `foreign_key_check=[]`
- 孤立 evidence item：0
- evidence selection 仍可在来源 extracted_text 中精确匹配。

## OBSERVATION

1. 唯一搜索词经当前分词语义返回 8 页，但目标 page_id=6 排名第 1；测试规范明确允许多结果，本轮只操作目标卡。
2. 浏览器下载后端先写入系统 Downloads，本轮立即将唯一新文件移动到指定 records 目录；下载真实完成且文件可打开。
3. 浏览器控制释放连接时，应用日志记录一次 Windows asyncio `ConnectionResetError [WinError 10054]`；发生在测试 UI 全部完成后的客户端断连时刻，无 app.exception、空白页、数据回滚或服务失效，未做重启复现。浏览器控制台另有重复的 sidebar `textColor` 空值 warning，无 error。

## 结束状态

- 隔离浏览器已关闭。
- 本轮 Streamlit 进程均已停止。
- 8501–8512 均无监听。
- core 数据库、唯一证据、最终备注、S4-09/S4-10 数据、截图、Markdown 证据包均保留。
- 仓库无代码、测试、配置、迁移或正式文档修改。
- 无 commit、merge、push、tag。
- S4-12 保持 `NOT STARTED`，不得自动继续。
