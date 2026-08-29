# Competition Report Fact Freeze Template（报告事实冻结模板）

**填写时点：2026-09-15 报告交接冻结当日。** 填写人：维护者 + Tester 汇总。
填写前逐项核对来源；填完后本文档即赵涵撰写期间唯一事实依据，改动需维护者签字。

| 字段 | 值（9/15 填写） | 来源 / 核对方式 |
| --- | --- | --- |
| Final version | TBD（v0.6.1 或冻结时实际版本） | `git describe` / 版本决策 |
| Final commit | TBD | `git rev-parse HEAD` |
| Release / tag | TBD（当前最近发布 tag `v0.6.0` → `bb1a4207…`） | `git tag` |
| App version | TBD（当前 `src/config.py` `app_version="0.6.0"`；如冻结前版本升级则更新） | `src/config.py` |
| Schema | TBD（当前 SCHEMA_VERSION=12） | `src/migrations.py` |
| Local endpoint | TBD（应仍为 127.0.0.1:8501；变更需维护者指令） | 启动命令 + health 检查 |
| Automated tests | TBD（精确 passed / skipped / warnings / duration / exit code；**9/15 冻结重跑**；不得沿用 2685/4 的 08-29 旧值） | `python -m pytest` 完整输出 |
| Manual test count | TBD（按 manual-test-plan 汇总表） | 测试记录文档 |
| Manual test pass | TBD（含修复后复测结论） | 同上 |
| Known issues | TBD（P1/P2 清单，逐条一句话） | 测试记录 + final-audit Findings |
| Demo freeze status | TBD（预期仍 KEEP_FROZEN；如 9/10–15 有解冻修复需记录原因与复测） | freeze manifest |
| Public deployment status | TBD（当前 DEFERRED / WP6A PARTIAL-PAUSED / WP6B NOT STARTED；**如无维护者明确指令不得变更此行**） | entry doc |
| Final screenshots | TBD（FIG-DEMO-01…06 文件名 + 采集日期 + 分辨率 + 隐私核对 PASS） | 大纲 §23 清单 |
| Demo recording | TBD（Mode 3 录像文件名/时长/分辨率，或 NOT RECORDED） | runbook 录制脚本 |
| Real AI validation status | TBD（当前冻结期间 completion=0/embedding=0/rerank=0；如 9 月上旬有授权真实验证需如实记录次数与用途） | AI 台账 + 维护者确认 |
| Report handoff date | TBD（2026-09-15） | 本文档提交记录 |

## 填写后动作

1. 将本表链接加入报告大纲 §0（替换 CURRENT 标注引用）；
2. 通知赵涵"事实已冻结，可以开始成稿"；
3. 此后任何事实变更 → 维护者更新本表并在报告稿中同步，禁止写稿人自行推测。
