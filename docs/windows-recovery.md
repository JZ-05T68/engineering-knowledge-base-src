# Windows 恢复与环境重建

本文用于在全新 Windows 电脑上从 GitHub 恢复可运行的 EKB 软件环境。GitHub 是代码、
结构、脚本、依赖和配置方法的灾备基线，不是私人知识资产或凭据的备份介质。

## 前置条件

- Windows 10/11；
- Git；
- Python 3.11（安装时建议启用 `py` launcher）；
- 可访问源码仓库的 GitHub 凭据。

## 重建步骤

```powershell
git clone git@github.com:JZ-05T68/engineering-knowledge-base-src.git
Set-Location engineering-knowledge-base-src
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

按需编辑本地 `.env`。AI 默认为 `manual`，不需要 API Key；只有用户决定启用可选 Qwen
能力时，才在本机 `.env` 或环境变量中重新填写自己的 Key。不得从 GitHub 恢复或提交真实
Key、Token、密码或其他凭据。

运行自动初始化与验证：

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest
.\启动工程知识库.bat
```

浏览器打开 `http://127.0.0.1:8501`，或检查
`http://127.0.0.1:8501/_stcore/health` 返回正常状态。也可运行 `查看运行状态.bat`。
停止服务使用 `停止工程知识库.bat`，完全静默启动可使用 `静默启动工程知识库.vbs`。

首次运行会安全初始化本地 SQLite 结构。生产实例固定使用 `data/` 与 8501；隔离 staging
使用 `staging-data/` 与 8502。不要把 staging 指向 production 数据，也不要把服务绑定到
`0.0.0.0` 或局域网地址。

## BAT 用途

| 文件 | 用途 |
|---|---|
| `启动工程知识库.bat` | 通过仓库本地 `.venv` 和 service manager 启动 production |
| `停止工程知识库.bat` | 安全停止由 service manager 管理的实例 |
| `查看运行状态.bat` | 检查 PID、进程、端口和健康端点 |
| `启用开机自启.bat` | 为当前 Windows 用户启用登录后启动 |
| `关闭开机自启.bat` | 关闭上述自启动 |
| `静默启动工程知识库.vbs` | 无控制台窗口启动同一 service manager |

## 不由 GitHub 恢复的内容

以下目录或文件由运行时生成或包含用户私有数据，均被 `.gitignore` 保护：

- `.env`、`.venv/`、`.streamlit/secrets.toml`；
- `data/` 中的 PDF、页面 PNG、Markdown 和 SQLite 数据库；
- `staging-data/`；
- `backups/`、`logs/`、`runtime/`；
- 测试、lint、编辑器和操作系统缓存。

如需迁移私人知识资产，应使用 EKB 的本地完整备份与恢复流程，并通过用户控制的安全介质
单独传输；不要上传到公开 GitHub 仓库。恢复前后都应执行 manifest、哈希和数据库完整性检查。

## 故障检查

- BAT 报找不到 Python：确认 `.venv\Scripts\python.exe` 存在，并重新安装依赖；
- 8501 被占用：运行 `查看运行状态.bat`，不要改用局域网监听；
- 数据库升级失败：保留现场和迁移前备份，不要删除原始 PDF 或页面图像；
- AI 不可用：保持 `EKB_AI_MODE=manual`，PDF、阅读、关键词搜索、笔记、证据与备份仍应可用。
