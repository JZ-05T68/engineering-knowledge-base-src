# ADR-009: Source Integrity Public Contract — Per-Source Snapshot Status on the Hosted /v0.6 Sources Endpoint

- **Status:** PROPOSED（待维护者 / DeepSeek-Pro 评审，未经批准不得实现）
- **Date:** 2026-08-29
- **Deciders:** EKB maintainer（本地单用户工程知识库）
- **Scope:** v0.6.x Demo Integration — Gap B（来源完整性）的公共契约设计冻结
- **Supersedes:** 无
- **Related:**
  - `docs/demo-integration-contract-design-review.md`（完整评审）
  - `docs/adr/ADR-007-public-deployment-foundation.md`（Hosted 只读边界）
  - `docs/adr/ADR-008-agent-citation-source-contract.md`（同批设计）
  - `docs/v0.6.1-competition-demo-freeze.md`（Known Limitations #2、Claim Matrix "changed≠篡改" 条款）

## Background

公共 `GET /v0.6/sources/{stable_id}` 目前只返回
`stable_id/type/title/label`（经 `safe_display_text` 净化），不含完整性状态。
演示 Mode 2 用 demo-only 的 `integrity_state` 预置值渲染徽章；Mode 1 正确地
只显示通用说明。Mode 1 需要真实完整性语义的公共契约。

代码事实（全部已核实）：

- 指纹 = 对 **DB 行**的规范化渲染做 SHA-256（`src/source_fingerprint.py`）：
  document 用导入时 `sha256` 列、page 用 extracted/ocr 文本、note/evidence 用
  行字段；读路径只读、不写回、不触文件系统；
- 基线快照存储在 **knowledge-object 来源链接**上（`knowledge_object_sources`
  指纹列，v11 加入）；状态 = 每次读取时"重算 vs 快照"对比；
- 状态词表已有三层：链接级 valid/changed/missing/unknown、对象级聚合
  （含 partially_valid/unsourced）、锚点级（含 not_applicable）；
- 公共来源类型仅四种：page / knowledge_object / knowledge_memory / evidence；
  knowledge_memory 是用户创作内容，不是外部材料快照。

## Decisions

### 1. 完整性作为来源响应的可选字段（OPTION A）

**Decision:** `SourceResponse` 新增**可选**字段
`integrity_state: StrictStr | None = None` 与
`integrity_checked_at: StrictStr | None = None`。字段值 `None`/缺省 =
"该类型或该实体不适用完整性语义"；计算在 **source-detail 请求时刻**执行
（latest-state），`checked_at` = 服务端完成对比的 UTC 时刻。

**Reason:** UI 需要完整性的时机就是打开 viewer 的时刻；单跳、毫秒级成本
（1 SELECT + 1 次 DB 内文本 SHA-256）、与演示字段同名形成超集连续性。
拒绝独立子资源（OPTION B：多一跳、端点/限流面扩大、单机 demo 无收益）与
Agent 响应内嵌（OPTION C：对未被查看的引用白付成本、引入 run 时刻 vs 查看
时刻的双时间语义混乱）。

**Consequences:** run 与查看之间源发生变化 → 查看端显示最新状态 + 判定时刻，
可接受且如实交代（不造快照、不做双状态）；`checked_at` 防止 UI 把"来源一致"
展示成无时效事实；不引入缓存/TTL（缓存=假新鲜度）。

### 2. 闭合状态词表与类型能力边界

**Decision:** 公共 `integrity_state` 采用闭合枚举
`valid / changed / missing / unknown / partially_valid / not_applicable`。
映射规则：

- `knowledge_object` → 来源链接聚合：valid/changed/missing/unknown 直映；
  `partially_valid` 原样保留；`unsourced → not_applicable`；
- `page` / `evidence` → 其 KO 来源链接快照 vs 重算：单链接 1:1；多链接冲突
  按保守序聚合 `changed > missing > unknown > valid`（绝不乐观折叠为 valid）；
  **未被任何 KO 链接引用** → `None`（无基线，无从比较）；
- `knowledge_memory` → 永远 `None`（用户创作内容，无外部基线，"changed"
  对其无意义）。

**Reason:** 可计算性由"是否存在基线快照"决定，词表必须如实暴露这一边界；
保守聚合确保冲突漂移不会被静默洗白；与演示共享 `integrity_state` 名称，
演示冻结的 5 值枚举成为真实 6 值词表的子集，UI 对未知值回退通用说明。

**Consequences:** `partially_valid` 进入公共词表（枚举扩展 = additive 演进，
消费者须 fail-closed 回退）；禁止为"原始实体独立基线"建 schema v13 ——
v12 数据已支撑本契约（无链接实体诚实返回 None）。

### 3. 语义红线（安全与诚实）

**Decision:** 公共契约**永不暴露**绝对路径、指纹载荷/sha256、敏感文件元数据、
私有存储位置、SQL、DB 主键、内部异常、provider 信息。`changed` 的唯一含义 =
"当前内容与记录的基线快照不一致（快照漂移）"，**永不**暗示造假、篡改、恶意
修改或虚假信息；禁用 "verified/已核验" 标签，`valid` 一律表述为
"与记录的基线快照一致"。API 文档必须内嵌："本地快照对比，非实时核验、非
篡改检测；状态仅代表 checked_at 时刻的观测。"

**Reason:** 与冻结清单 Claim Matrix（"完整性为实时联网核验 = NOT SUPPORTED
禁说"）同一条红线；指纹载荷与路径是本机隐私边界（Local-first）。

**Consequences:** 不引入 reason codes（状态语义单一，reason 会诱导 UI 转译
因果叙事）；计算内部失败 → 省略可选字段 + 服务端 `LOGGER.warning`（仅失败
类别）+ 200 —— 唯一被批准的静默降级，严格限定于可选增强字段；`changed` 是
成功结果（200），绝不是 HTTP 错误。

### 4. 向后兼容与演进

**Decision:** 字段 additive、可选、带默认 `/v0.6` 不 bump；`SourceResponse`
既有四字段原样保留。版本偏斜政策与 ADR-008 §4 相同（公共 DTO
`extra="forbid"` → 实现包必须同包更新客户端容忍；冻结客户端在整合包前不得
指向增强服务器）。枚举只增不删；删字段/改语义 = 破坏性 → 新路径 + 新 ADR。

**Reason:** 同 ADR-008 —— JSON additive 安全，pydantic 严格解析是确定破坏，
用发布纪律处理。

### 5. 归属与冻结

**Decision:** 实现归属 **v0.6.3**（新增只读 API 能力，与 v0.6.2 的引用映射
兼容性补全分开，守住 "fix ≠ capability" 演进纪律）。演示**语义**零变化
（fixture、场景、文案不动），但实现触碰冻结清单"需后端评审"文件
（`src/hosted_api/contracts.py`、`src/demo_ui.py`、`src/demo/contracts.py`），
须维护者赛后单独授权整合包；落地前冻结 Mode-1 客户端不得指向增强服务器。

## Consequences Summary

- `GET /v0.6/sources/{stable_id}` 新增两个可选字段；不新增端点；不新增限流
  类别；schema 保持 v12；路径保持 /v0.6。
- 完整性是**读时点观测**：最新状态 + checked_at；无缓存、无 TTL、无实时承诺。
- 安全不变量：无路径/载荷/主键泄漏；changed ≠ 篡改；无 "verified" 标签；
  冲突保守聚合；内部失败省略字段并留日志。
- 测试清单见设计评审 §10 T7-T15；状态 PROPOSED，实现待赛后授权的 v0.6.3
  整合包。

## References

- `docs/demo-integration-contract-design-review.md`（§2、§2.3 词表、§10 测试清单、§11 发布计划、§14 DeepSeek 决策包）
- `src/source_fingerprint.py`、`src/models.py`（状态词表）、
  `src/knowledge_object_service.py`、`src/migrations.py`（链接快照列）、
  `src/hosted_api/contracts.py`、`src/source_metadata.py`
- `docs/v0.6.1-competition-demo-freeze.md`（Claim Matrix、Known Limitations #2）
