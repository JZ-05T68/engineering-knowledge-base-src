# Engineering Knowledge Base v0.0.2

一个本地优先、单用户的个人工程知识管理系统。它把 PDF 资料转化为可长期整理、检索和复用的页面级知识资产：**文档 → 理解 → 检索 → 复用 → 工程能力**。

项目默认只监听 `127.0.0.1`，核心功能可离线使用，不需要账号、VPN、云存储、API Key 或付费服务。系统不包含注册、登录、权限、OAuth、JWT 或云同步。

## v0.0.2 功能

- 导入 PDF，使用 SHA-256 检测相同内容；同名但内容不同的文件可分别导入。
- 原 PDF 保存到 `data/raw/`，每页以清晰 PNG 保存到 `data/pages/<文档编号>/`。
- 提取 PDF 文本层；扫描、手写、文本不足或失败页面进入待复核列表。
- 每页拥有独立 Markdown 笔记，支持编辑、预览、保存状态、清空和持久化。
- 双栏阅读器同时显示可滚动/缩放的页面原图与 Markdown 编辑区。
- 文档和页面均可关联可复用标签及本地项目。
- 文档可按名称、导入时间、更新时间、标签、项目和导入状态筛选。
- SQLite FTS5 搜索文档标题、文件名、提取文本、OCR 字段、Markdown、标签和项目，并直接跳转命中页面。
- 记录导入状态、进度、总页数、已处理页、文本页、待复核页、失败页和错误。
- 单页失败不会撤销其他已经完成的页面。
- 一键后台启动/停止、重复启动检测、PID 身份校验、本机健康检查和轮转日志。
- 可选的 Windows 当前用户登录后自动启动；优先使用任务计划程序，受系统策略限制时回退到当前用户“启动”文件夹。

v0.0.2 预留了 OCR 文本字段，但不接入云端 OCR，也不直接调用任何大模型 API。外部 AI 提示词仍是用户主动复制的纯文本。

## 环境要求

- Windows 10/11 与 PowerShell
- Python 3.11
- Python 自带 SQLite 支持 FTS5

项目依赖均列在 `requirements.txt`。v0.0.2 没有新增第三方依赖，继续使用 Streamlit、PyMuPDF、Pillow、pydantic-settings、jieba、rapidfuzz、pytest 和 ruff。

## 首次安装

在项目根目录打开一次 PowerShell：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

可按需复制 `.env.example` 调整渲染 DPI、文本阈值或端口：

```powershell
Copy-Item .env.example .env
```

监听地址被限制为 `127.0.0.1`；不要改为 `0.0.0.0`。

## 启动与停止

### 日常一键启动

双击项目根目录的：

```text
启动工程知识库.bat
```

脚本会自动定位项目、检查 `.venv`、PID、进程、健康端点和端口，在后台启动服务并打开 `http://127.0.0.1:8501`。窗口关闭、浏览器关闭或 VS Code 关闭后，服务仍继续运行。

如需完全静默启动，可双击：

```text
静默启动工程知识库.vbs
```

### 停止

双击：

```text
停止工程知识库.bat
```

停止器只终止 PID 文件中经过解释器身份校验的本项目进程，不使用 `taskkill /IM python.exe`，不会按名称误杀其他 Python 程序。

双击 `查看运行状态.bat` 可区分未运行、正在启动、正常运行、端口被其他程序占用和异常退出。

开发调试时仍可手动前台运行：

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

## Windows 登录后自动启动

自动启动默认关闭，不会强制启用。

- 双击 `启用开机自启.bat`：优先创建当前用户 `ONLOGON` 计划任务 `EngineeringKnowledgeBase`，不请求管理员权限；若本机策略拒绝，则创建当前用户启动文件夹入口。
- 双击 `关闭开机自启.bat`：删除计划任务或启动文件夹入口。

完全移除前请先运行 `关闭开机自启.bat`。也可手动检查：

```powershell
schtasks.exe /Query /TN EngineeringKnowledgeBase
```

回退入口位于：

```text
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\EngineeringKnowledgeBase.cmd
```

## 本地目录

| 路径 | 用途 |
| --- | --- |
| `app.py` | Streamlit 首页 / 仪表盘 |
| `pages/` | 导入、文档阅读、检索、待复核、标签、项目、导入记录和运行说明 |
| `src/migrations.py` | 带自动备份的正式 SQLite 迁移 |
| `src/database.py` | SQLite、FTS5、标签、项目、导入记录数据访问 |
| `src/pdf_service.py` | PDF 哈希、逐页文本提取和 PNG 渲染 |
| `src/document_service.py` | 导入、笔记、复核、重试和显式删除流程 |
| `scripts/service_manager.py` | 后台启停、状态、PID、健康检查和自启动管理 |
| `data/raw/` | 原始 PDF（沿用 v0.0.1 路径以保持兼容） |
| `data/pages/` | 按文档编号保存的页面 PNG |
| `data/markdown/` | 按文档编号保存的页面 Markdown |
| `data/database/knowledge.db` | SQLite 主数据库 |
| `data/database/backups/` | 自动迁移备份与手动备份 |
| `logs/` | 应用、服务管理和启动控制台轮转日志 |
| `runtime/` | 当前服务 PID 记录；异常退出后的过期记录会自动识别 |

数据目录、日志目录和运行目录互相分离。`data/` 中的用户材料、数据库、日志和 PID 均被 Git 忽略。

## 数据库升级、备份与恢复

### 升级

启动 v0.0.2 时，程序读取 `schema_migrations`。检测到 v0.0.1 数据库后会先使用 SQLite 在线备份 API 创建：

```text
data/database/backups/knowledge.v1.<时间戳>.db
```

随后在事务中升级到 schema v2。失败会回滚，原库和迁移前备份都保留；不得通过删除 `knowledge.db` 升级。

### 手动备份

先运行 `停止工程知识库.bat`，再复制整个 `data/` 目录到另一个本地磁盘。最稳妥的备份包含数据库、原 PDF、页面图片和 Markdown，而不只是单个数据库文件。

### 恢复

1. 停止工程知识库。
2. 将当前 `data/` 目录改名保留，不要直接覆盖或删除。
3. 把完整备份恢复为项目根目录下的 `data/`。
4. 启动工程知识库，确认首页计数、页面图片和笔记。

如果只恢复数据库迁移备份，也必须确保对应的 `data/raw/`、`data/pages/` 和 `data/markdown/` 文件仍在原相对路径；否则元数据存在但原图会显示缺失。

## 删除行为

- 清空笔记只处理所选页面笔记，不改变原 PDF、页面图片或提取文本。
- 删除标签只删除标签及关联。
- 删除项目只删除项目及关联。
- 删除文档需要输入文档标题二次确认；只清理该文档自己的记录、原 PDF、页面 PNG 和 Markdown，不会删除其他文档。
- 关键关联和元数据修改使用 SQLite 事务。

## 常见故障排查

| 现象 | 处理方式 |
| --- | --- |
| 提示虚拟环境不存在 | 按“首次安装”创建 `.venv`，不要复制其他机器的虚拟环境 |
| 8501 端口被占用 | 运行 `查看运行状态.bat`；关闭占用程序，或在 `.env` 设置未占用的 `EKB_PORT` |
| PID 文件过期 | 再次启动或查看状态会自动识别并清理；PID 在 `runtime/` |
| 服务异常退出 | 查看 `logs/server-console.log` 和 `logs/service-manager.log` |
| 数据库不可写/迁移失败 | 停止服务，检查 `data/database/` 权限；保留主库并查看 `backups/`，不要删库重建 |
| PDF 损坏、为空或受密码保护 | 导入页会显示中文错误；原始输入不会覆盖其他资料 |
| 单页处理失败 | 在“待复核页面”查看失败原因并重试，其他页面不受影响 |
| 页面图片缺失 | 从完整 `data/` 备份恢复对应 `data/pages/<文档编号>/` |
| 笔记保存失败 | 页面会显示“保存失败”；检查数据库与 `data/markdown/` 写权限 |
| 计划任务 Access denied | 启用脚本会自动使用当前用户启动文件夹，不需要管理员权限 |

健康检查地址为 `http://127.0.0.1:8501/_stcore/health`，只返回简单状态，不包含用户资料。

## 质量检查

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest
```

## 当前限制

- 不执行复杂 OCR 或手写识别；OCR 字段为未来本地可选方案预留。
- 搜索是 SQLite FTS5 与本地元数据匹配，不包含向量数据库、Embedding 或语义大模型检索。
- Streamlit 使用内置 `/_stcore/health`，没有引入第二个后端或网络服务。
- Windows 计划任务受系统策略控制；启动文件夹是普通用户回退方案。
