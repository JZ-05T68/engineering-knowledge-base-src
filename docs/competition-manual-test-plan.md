# Competition Manual Test Plan（三人真实人工测试计划 · 2026-09-10 ~ 09-15）

目的：在 2026-09-15 报告交接冻结前，由三名成员完成真实人工测试并留下可引用证据。
配套：报告大纲 `docs/competition-report-outline-2026.md`（§15 引用本文件）、
事实冻结模板 `docs/competition-report-fact-freeze-template.md`。

## 1. 测试环境与前提

- 版本：9/10 从 `origin/main` 最新提交启动测试；每个用例记录测试时 commit。
- 启动：`python -m streamlit run app.py --server.port 8501 --server.address 127.0.0.1 --server.headless true`；
  健康检查 `http://127.0.0.1:8501/_stcore/health` = 200。
- 数据：**一律使用各自的测试数据目录/演示数据，禁止使用或污染生产库
  `data/database/knowledge.db`**（导入测试使用自备样例 PDF，不含隐私）。
- Agent 演示测试跑 Mode 2（预置离线演示）；Mode 1 仅按 MT-AGENT-003 验证失败路径。
- 每人一个浏览器 profile，互不共享会话。

## 2. 三人分工（Tester A / B / C）

| 角色 | 负责组 | 覆盖理由 |
| --- | --- | --- |
| **Tester A — 核心知识工作流** | A 启动/关闭、B 导入、C 浏览、D 检索、M 备份恢复、O 跨页回归 | 知识管理主链路一次贯通 |
| **Tester B — Agent/AI/演示/失败态** | I AI(授权范围)、J Agent 工作台、K Demo A/B/C、L 错误/空态、P 投影环境 | 演示关键路径专家化 |
| **Tester C — 新人视角 + 集成** | 以"第一次接触的评委"视角走查全部页面 + D/E/F/G/H 抽测 + N 安全隐私 | 发现习以为常的可用性问题 |

**双重覆盖要求（至少两人独立执行）**：K 组全部（Demo A/B/C/空/失败/重置）、
A 组启动健康检查、D 组检索基本路径、N 组安全项。三人都不测的组：无。

## 3. 测试组与用例清单

### A. 启动 / 关闭（Tester A）
- MT-START-001 冷启动→health 200→首页正常渲染（记录秒数）。
- MT-START-002 关闭服务后访问 8501 应不可达；重启可恢复。
- MT-START-003 重复启动同端口应得到明确错误，不产生脏状态。

### B. 数据导入（Tester A）
- MT-IMPORT-001 导入一个多页 PDF：原件保留、逐页 PNG、文本层提取。
- MT-IMPORT-002 重复导入同一文件：SHA-256 判重提示。
- MT-IMPORT-003 扫描件（无文本层）：页面标记待复核，不失败。
- MT-IMPORT-004 导入损坏/非 PDF 文件：中文错误提示，无堆栈。

### C. 浏览（Tester A）
- MT-BROWSE-001 文档列表→页面双栏浏览、翻页、缩放。
- MT-BROWSE-002 页面状态与标签、项目显示正确。

### D. 检索（Tester A + C 双人）
- MT-SEARCH-001 关键词命中页面/笔记/标题；点击跳转正确。
- MT-SEARCH-002 无结果：空态友好，无异常。
- MT-SEARCH-003 长查询/特殊字符不崩溃。

### E. 笔记 / 整理（Tester C 抽测）
- MT-NOTE-001 页面 Markdown 新增/编辑/保存。
- MT-NOTE-002 待复核页处理流程走通。

### F. 证据篮（Tester C 抽测）
- MT-EVID-001 选取区加入证据篮、排序、导出 Markdown（来源可追溯字段存在）。

### G. 知识对象（Tester C 抽测）
- MT-KNOW-001 创建/编辑知识对象并关联来源。

### H. 知识记忆（Tester C 抽测）
- MT-MEM-001 创建问题解决记录（现象/原因/处理），列表可见。

### I. AI / RAG（授权范围，Tester B；未配置 Key 时验证 manual 默认）
- MT-AI-001 未配置 EKB_AI_API_KEY：核心功能不受影响，AI 功能明确提示手动模式。
- MT-AI-002 Ask AI（受控回答）：仅基于已选上下文回答，不写回知识库。
  （如团队授权配置测试 Key：验证台账记录生成；否则标记 BLOCKED-授权外。）

### J. Agent 工作台（Tester B）
- MT-AGENT-001 工作台初始态：品牌/模式徽章/3 场景卡/右栏信任提示齐全。
- MT-AGENT-002 自由输入问题→回答→徽章与引用正确渲染。
- MT-AGENT-003 Mode 1 未启动本机服务：显示"本机 Agent 服务当前不可用"，
  无堆栈；"切换到预置离线演示"可用。
- MT-AGENT-004 演示重置：重置后回到初始态、模式回预置演示、无残留。
- MT-AGENT-005 快速连点三张场景卡：最终显示最后一次提问结果，无串台。

### K. Demo A/B/C（Tester B + C 双人全量）
- MT-DEMO-A-001 点"A · 参数影响"→✓有依据回答+引用 2 条+预置演示标识。
- MT-DEMO-A-002 点击来源 #1→来源详情（标题/页面资料·第 12 页/为什么与回答有关）。
- MT-DEMO-B-001 点"B · 历史经验"→✓+⚠来源存在限制横幅+知识记忆来源。
- MT-DEMO-C-001 点"C · 来源可信度"→来源 #1→"来源状态·来源发生变化"琥珀块
  +免责句（不含"篡改/不可信"字样）+演示预置说明。
- MT-DEMO-E-001 折叠区"备用·超出知识范围"→诚实空态+换题建议（无引用）。
- MT-DEMO-F-001 折叠区"排练·失败演练"→"×本次请求未完成"+重试（无堆栈/JSON）。
- MT-DEMO-R-001 全部场景后执行重置→初始态；再次 A 场景结果一致（确定性）。

### L. 错误 / 空态（Tester B）
- MT-ERR-001 检索无结果空态；MT-ERR-002 表单空提交提示；MT-ERR-003 断网状态下
  使用 Mode 2 演示不受影响（可拔网线或飞行模式验证本机回环不受影响）。

### M. 备份 / 恢复（Tester A）
- MT-RESTORE-001 创建备份→检查备份文件存在；MT-RESTORE-002 恢复流程（测试目录）→
  数据一致；MT-RESTORE-003 恢复不覆盖/不删除原件（人工确认提示存在）。

### N. 安全 / 隐私（Tester C）
- MT-SEC-001 服务仅 127.0.0.1 可访问（同网段另一设备访问被拒）。
- MT-SEC-002 Agent 演示页全程无路径/内部 ID/API Key/堆栈出现（对照截图检查）。
- MT-SEC-003 截图素材中无真实隐私内容（导出前逐张核对）。
- MT-SEC-004 生产库文件未被演示流程读取/修改（修改时间不变）。

### O. 跨页回归（Tester A）
- MT-REG-001 导入→浏览→检索→笔记→证据篮全链路走通后，Agent 工作台不受影响。

### P. 投影 / 演示环境（Tester B）
- MT-PROJ-001 1920×1080：首屏完整叙事，无横向滚动条。
- MT-PROJ-002 1366×768：无溢出，CTA 可见。
- MT-PROJ-003 浏览器 F11 全屏演示 3 分钟走完 A→B→C 主脚本（对照 runbook 计时）。
- MT-PROJ-004 会议室亮度下 3 米距离可读回答与徽章文字。

## 4. 用例记录模板（每条用例一行，复制使用）

| 字段 | 值 |
| --- | --- |
| Test ID | MT-XXXX-NNN |
| Date / Tester | 2026-09-xx / A|B|C |
| Version / commit | 测试时 `git rev-parse --short HEAD` |
| Environment | Windows 11 / Chrome xxx / 1920×1080 |
| Preconditions | … |
| Steps | 1)… 2)… 3)… |
| Expected | … |
| Actual | … |
| Result | PASS / FAIL / BLOCKED |
| Severity | P0/P1/P2/P3（FAIL 时必填） |
| Issue ID | ISS-NNN |
| Fix commit | … |
| Retest | PASS / 未复测 |
| Evidence | 截图文件名或记录位置 |
| Notes | … |

建议存放：`docs/competition-manual-test-results-2026.md`（9/10 创建，三人分节追加）。

## 5. 严重级定义

- **P0**：比赛演示/数据安全阻断（崩溃、数据丢失、演示主路径不可用、隐私泄露）。
  9/10–9/15 内必须修复并复测，否则**阻断冻结**。
- **P1**：主要工作流缺陷。需维护者决定（修复或书面接受）。
- **P2**：次要功能/UX 缺陷。可记录并在报告中列"已知问题"。
- **P3**：外观/观察项。记录即可。

## 6. 9/15 报告冻结标准（全部满足才交接赵涵）

1. 无未解决 P0；
2. K 组（Demo A/B/C/空/失败/重置）至少两人独立 PASS；
3. A/C/D/N 组核心用例全部执行（PASS 或书面接受）；
4. 全量自动化套件冻结重跑 PASS（记录精确数字）；
5. 最终版本/commit/app_version 记录进 fact-freeze 模板；
6. 终版截图（FIG-DEMO-01…06）按大纲 §23 要求采集并核对无隐私；
7. 已知 P1/P2 清单完成；
8. 生产库安全确认（MT-SEC-004 PASS）；
9. runbook 主脚本至少排练一次并记录用时（目标 3.5–4.5 分钟）；
10. 事实冻结模板填写完成并交赵涵。

## 7. 汇总模板（9/15 填写，禁止现在填数）

| 指标 | 值 |
| --- | --- |
| Total cases | TBD |
| Passed | TBD |
| Failed | TBD |
| Blocked | TBD |
| P0 / P1 / P2 | TBD / TBD / TBD |
| Retested after fix | TBD |
| Final pass rate | TBD |

（此表 9/15 后同步到报告大纲 §15。）
