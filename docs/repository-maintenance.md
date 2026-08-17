# GitHub 仓库维护规则

EKB 使用两个职责不同的 GitHub 仓库：

| 仓库 | 职责 | 固定同步纪律 |
|---|---|---|
| `engineering-knowledge-base` | 公开展示、截图与版本摘要 | **每个小版本都必须同步更新展示仓库** |
| `engineering-knowledge-base-src` | 源码、脚本、依赖与恢复方法 | **每个支版本都必须同步更新源码仓库** |

以上术语和版本规则不得在发布过程中擅自改变。发布检查应同时核对版本号、功能事实、截图、
测试口径、roadmap 和链接，避免源码演进后展示仓库仍停留在旧版本。

## 安全边界

允许进入 GitHub 的内容包括源码、BAT/VBS、依赖清单、`.env.example`、配置占位符、恢复步骤、
脱敏样例、架构说明和公开展示材料。

禁止提交真实 `.env`、API Key、Token、密码、私钥、私人数据库、真实用户文档、未脱敏日志、
runtime PID、缓存、机器私有状态或未脱敏备份。发现疑似敏感内容时，停止暂存和提交该文件，
先报告并人工判断；不能以灾备为理由上传整个本机或用户资料。

## 发布与维护检查

1. 以源码仓库当前真实实现、tag、release notes 和已执行验证为依据，不把 roadmap 写成已完成；
2. 展示截图只使用隔离的合成演示资料，逐张检查 Key、Token、用户名、绝对路径和私密文件名；
3. 核对 `.gitignore`、`.env.example`、requirements、启动脚本与 Windows 恢复文档；
4. BAT 必须相对仓库根目录定位 `.venv` 和 `scripts/service_manager.py`，production 固定为
   `127.0.0.1:8501`，staging 固定为隔离的 8502 与 `staging-data/`；
5. 修改代码后运行 Ruff 与 pytest，并只报告实际执行结果；
6. 不 force push、不重写已发布历史、不移动已发布 annotated tag；
7. 历史 worktree 仅在 clean、无独有 untracked/未提交成果且 Git 历史可恢复时删除，固定开发
   worktree `ekb-dev` 始终保留。
