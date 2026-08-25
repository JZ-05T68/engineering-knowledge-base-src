[简体中文](README.md) | [English](README_EN.md)

# Engineering Knowledge Base v0.5.2

**Engineering Knowledge Base（EKB）**

一个本地优先的个人知识系统，用于沉淀、组织、验证和复用长期知识资产。

EKB 面向需要长期积累工程经验的个人用户。它把用户主动导入的资料、人工记录的经验和可核验的来源
组织为本地知识资产，而不是把项目简化成 PDF 摘要器、RAG 工具或通用聊天机器人。

## Vision

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

## Core Architecture

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

RAG 或外部 AI 不是 EKB 的第一定位。可选 AI 只是一层显式启用的增强能力，不能替代本地事实来源、
人工确认和工程判断。

## v0.5.2 — Knowledge Foundation

v0.5.2 完成个人知识资产底座的第一阶段收口：

- **Knowledge Foundation**：建立知识对象、类型化关系、知识记忆、稳定标识、生命周期和追加式修订记录。
- **Source Integrity**：知识对象来源绑定本地文档、页面、笔记或证据，并通过 Source Fingerprint
  在读取时判断来源完整性。
- **FTS v11 Retrieval**：schema v11 为知识对象和知识记忆建立本地 SQLite FTS5 索引、同步触发器与安全重建路径。
- **Knowledge Object Search**：按标题、摘要、正文和标签检索知识对象，并保留状态与来源信息。
- **Knowledge Memory Search**：按问题解决、经验和决策内容检索个人知识记忆。
- **Provenance-aware retrieval**：页面结果、知识对象和知识记忆使用各自明确的来源锚点，检索结果可以回到本地来源核验。
- **Local-first operation**：核心知识管理和知识检索不需要账号、云服务、VPN 或 API Key。

v0.5.2 没有实现 Personal Context Agent、自动经验学习、后台自动改写知识或云端同步。

## Personal Knowledge Workflow

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
回到来源核验并复用
```

系统可以提取元数据、渲染页面、检测文本层、标记待复核页面并建议整理路径，但不会自动覆盖原始资料、
修改用户笔记、删除文件或把未经确认的推断写成个人经验。

## Existing Core Capabilities

- 导入 PDF，以 SHA-256 检测重复文件，逐页渲染 PNG 并提取已有文本层；
- 对扫描页显式执行本地单页 OCR，保留“未经人工核验”的边界；
- 浏览文档和页面，维护 Markdown、结构化笔记、标签与项目；
- 使用 SQLite FTS5、jieba 和字段权重执行本地全文检索；
- 创建页面、文字选区和图片区域证据，经人工确认后生成 citation-grounded 提示词包；
- 管理知识对象、知识记忆、来源、关系、修订和来源完整性；
- 创建、验证和恢复本地完整备份；
- 在显式配置时使用可选的页面向量与混合检索；AI 不可用时离线核心功能保持可用。

## Local-First and Information Boundaries

- 本地文件和 SQLite 是唯一事实来源；用户材料默认存放在本机。
- 正式服务固定绑定 `127.0.0.1:8501`，不暴露到局域网或公网。
- 系统仅处理用户主动导入或明确授权的资料，不抓取私人聊天或未授权第三方材料。
- 原始 PDF 和页面图像不会被自动覆盖或删除。
- 默认 `ai_mode="manual"`；没有 API Key 时应用仍可启动和使用核心功能。
- 不包含注册、登录、账号、密码、OAuth、JWT、角色、管理员或多用户权限系统。
- 不提供云同步；备份位置和备份介质由用户自行控制。

## Release Validation

v0.5.2 CLOSED 基线记录：

- Full pytest：**1646 passed in 975.32s**；
- Retrieval benchmark：**45 passed**；
- Focused regression：**279 passed**；
- Ruff：**PASS**；
- `git diff --check`：**clean**；
- 生产数据库：`data/database/knowledge.db`，327680 bytes；
- 生产数据库 SHA-256：`6a3ab3542c6865007c1fab3c739228f97d2120b1527dbb6cdefa26834e8b9c91`；
- CLOSED 运行验收：`127.0.0.1:8501`，health HTTP 200。

这些数字记录冻结发布基线，不代表所有工程领域、语料或查询都已获得同等质量保证。

## Roadmap

| 版本线 | 主题 | 状态 |
| --- | --- | --- |
| **v0.5.x** | Knowledge Foundation | v0.5.2 已完成知识对象、来源完整性、修订、知识记忆和本地检索底座。 |
| **v0.6.x** | Personal Context Agent | 未来规划；尚未实现。目标是在用户显式控制下组织个人上下文。 |
| **v0.7.x** | Experience Memory | 未来规划；尚未实现。目标是增强长期经验的确认、演进和复用。 |
| **v1.0** | Personal Experience System | 长期方向；尚未实现。目标是形成完整的个人经验系统。 |

详细边界见 [v0.5.x Roadmap](docs/v0.5.x-roadmap.md)。路线图不是当前能力承诺，也不承诺发布日期。

## Limitations

- 当前是 Windows 本地、单用户系统，没有多用户协作或权限模型。
- 没有云同步、云账号或托管服务；跨设备备份需要用户自行管理。
- Personal Context Agent 尚未完成，系统不会自主规划任务或代表用户持续行动。
- 不会自动学习个人经验；知识对象和知识记忆需要用户主动创建、复核或确认。
- 本地 OCR 面向单页印刷体，不支持手写、公式、复杂表格结构识别或批量 OCR。
- 关键词检索不会自动扩展同义词；可选混合检索不代表全面语义理解，也不替代来源核验。
- 当前搜索展示和批量操作有加载范围限制；超大知识库的深分页性能仍需持续观察。
- 完整备份是本地目录结构，不是加密归档；备份介质安全由用户负责。

## Installation

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

## Start and Stop

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

## Local Data and Safety

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

## Quality Checks

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

## Documentation

- [CHANGELOG](CHANGELOG.md)
- [v0.5.x Roadmap](docs/v0.5.x-roadmap.md)
- [Windows 恢复与环境重建](docs/windows-recovery.md)
- [GitHub Releases](https://github.com/JZ-05T68/engineering-knowledge-base-src/releases)

`README.md` 与 `README_EN.md` 是对等的正式项目说明；产品定位、能力、限制和 roadmap 变更必须同步维护。
