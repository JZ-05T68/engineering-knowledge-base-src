# Demo Integration Contract Design Review — Gap A（权威引用映射）与 Gap B（来源完整性）

- **日期：** 2026-08-29　**性质：** 设计/ADR/契约评审，**不实现**。
- **基线：** `main` @ `c6657bc6`（v0.6.0 RELEASED / v0.6.1 演示 KEEP_FROZEN；2704 collected / 2704 passed / 0 skipped；公网部署 DEFERRED，WP6A PARTIAL/PAUSED）。
- **决策载体：** `docs/adr/ADR-008-agent-citation-source-contract.md`（Gap A）与
  `docs/adr/ADR-009-source-integrity-public-contract.md`（Gap B），状态均为 **PROPOSED**，
  待维护者 / DeepSeek-Pro 评审后才可进入实现包。
- **配套：** 文末附「DeepSeek-Pro 决策包」紧凑版，无需重读仓库即可评审。

---

## 0. 现状事实基础（全部经源码核实，非转述）

| # | 事实 | 证据 |
| --- | --- | --- |
| F1 | `#N` 编号**已经**在服务端唯一铸造：`KnowledgeContextPackager.build` 对保留的上下文项 1-based 顺序生成 `(stable_id, "#N")`，发生在**模型调用之前** | `src/knowledge_context_packager.py:261-263` |
| F2 | 引用校验是 fail-closed 的：回答中的 `#N` 必须命中 F1 的映射，裸 stable_id 必须命中包内条目，未知/伪造/缺失 → 整个回答拒绝（`RagAnswerError`）；合法集合按首次出现去重 | `src/ai/rag_answer_service.py:161-200` |
| F3 | 公共响应只暴露 `citations: tuple[StrictStr, ...]`（validated stable ids），**不含** `#N → stable_id` 映射 | `src/hosted_api/contracts.py:83-90,93-105` |
| F4 | 公共来源响应只有 `stable_id/type/title/label`，无完整性字段 | `src/hosted_api/contracts.py:108-125` |
| F5 | 公共 DTO 一律 `extra="forbid"`；Mode-1 客户端用 pydantic 严格解析 → **服务器新增任何字段都会让冻结的 v0.6.1 客户端解析失败** | `contracts.py:21-22`；`src/agent_client.py:86,99` |
| F6 | 演示层是真实 DTO 的显式超集：`DemoCitation(display_index≥1, stable_id, anchor_label, source_type, title, label)`、`DemoAgentRunResponse.citations_detail`、`DemoSourceResponse.integrity_state: ContextFingerprintState \| None` | `src/demo/contracts.py:59-81` |
| F7 | UI 已经按"可选增强 + 回退"消费：`build_citation_chips` 有 detail/无 detail 双分支（无 detail 时 `display_index=None`，绝不暗示正文 `#N` 映射）；viewer 在 `integrity_state` 缺失时显示 `LIVE_INTEGRITY_NOTE` | `src/demo_ui.py:335-377,381-426` |
| F8 | 指纹 = 对 **DB 行**的规范化渲染做 SHA-256（document 用导入时 `sha256` 列、page 用 extracted/ocr 文本、note/evidence 用行字段）；读路径只读、不写回、不触文件系统 | `src/source_fingerprint.py:13-96` |
| F9 | 基线快照存在 **knowledge-object 来源链接**上（`knowledge_object_sources` 的指纹列，v11 加入，legacy 为 NULL→unknown）；状态=每次读取时重算对比 | `src/migrations.py:955-975,1060-1067`；`src/models.py:1121-1135` |
| F10 | 公共来源类型仅四种（`ContextItemType`）：page / knowledge_object / knowledge_memory / evidence；document/note 不是公共类型 | `src/models.py:1463-1470`；`src/source_metadata.py:82-110` |
| F11 | 完整性状态机已有三层词表：链接级 `KnowledgeSourceStatus`（valid/changed/missing/unknown）、对象级聚合 `KnowledgeSourceAggregateState`（含 partially_valid/unsourced）、锚点级 `ContextFingerprintState`（含 not_applicable） | `src/models.py:1121-1160,268-295` |
| F12 | `inspect_source_integrity` 工具只覆盖 knowledge_object / knowledge_source stable-id，永不刷新、永不写库 | `src/agent/tools/adapters/source_integrity.py:6-9` |

---

## 1. GAP A — 权威引用映射

### 1.1 设计问题的直接回答（规格 §5.1-12）

1. **编号从哪里来？** 从 F1 的既有铸造点（packager）。`display_index` 必须**恒等于**回答正文中出现的 `#N` 标记数字——否则 UI 会在正文写 `#1` 处渲染指向别处的 `#2`。禁止任何二次编号。
2. **display_index 是展示关切还是语义契约？** 两者皆是，但**权威性属于语义契约**：它是"正文标记 ↔ 来源"映射的键。展示层（颜色、排序、折叠）才是展示关切。
3. **Final Answer Stage 是否应在文本渲染前生成结构化引用？** 是——结构化映射在**校验完成后**由 Agent 响应层一次性生成（见 §1.3），文本只是该映射的一种渲染。
4. **模型应输出什么表示？** 维持现状：提示词要求模型输出 `#N`（校验器同时容忍裸 stable_id 作为回退）。不引入新 token 语法（`[cite:…]` 等）——模型依赖越小，fail-closed 面越小。
5. **如何防止正文 "#1" 因形似而变成权威？** 权威性**不来自文本形状**，而来自"该 `#N` 在 F1 映射中存在且通过了 F2 校验"。正文里任何未被映射的 `#N`（例如巧合的"#12"）已经触发整体拒绝——这正是现有行为（测试钉住），保持不变。
6. **如何保证 #1→源A、#2→源B 无前端启发？** 映射由校验器使用的**同一个** `citation_by_number` 字典投影而来；前端只做查表渲染。前端永远收不到"需要猜"的输入。
7. **重复引用（一源多处）？** 同一 `#N` 在正文出现多次 → 映射中只有**一条**条目（`display_index` 唯一）；`citations[]` 保持去重首次出现序。
8. **一句多源？** 正文 `#1#2` → 两个独立条目；映射按键（N）而非按句组织，无需句级模型（Option B 的注解才需要）。
9. **排序如何定义？** `citations_detail` 按 `display_index` **升序**；`citations[]` 维持现有"校验器首次出现序"不变。两者排序语义分别文档化（§3 演进规则）。
10. **校验剔除/拒绝某源会怎样？** 现状即 fail-closed：任何未知引用 → 整个回答 FAILED（`citation_invalid`）。因此**已完成的回答里不存在"被剔除但残留"的映射**；映射只为通过校验的标记存在。
11. **索引会有缺口吗？** 会，且应当允许：包铸造 `#1..#K`（K=全部保留上下文项），回答只引用子集 → 响应只携带被引用的 N（如 #1、#3）。缺口诚实反映"哪些标记出现在正文中"。**禁止**重排闭缺（会破坏与正文标记的对应）。UI 按存在的索引渲染 chips。
12. **索引作用域？** 单次响应内有效（per-response），非持久、跨响应无意义——`request_id` 是其作用域键。

### 1.2 设计选项（三个，均从本仓约束出发）

**OPTION A — 响应级结构化引用对象（additive 字段）**

```
AgentRunResponse:
  citations: tuple[StrictStr, ...]              # 不变
  citations_detail: tuple[CitationDetail, ...] = ()   # 新增，默认空

CitationDetail:
  display_index: StrictInt (>=1)     # = 正文 #N
  stable_id: StrictStr               # 已通过校验
  source_type: ContextItemType
  title / label: StrictStr | None    # safe_display_text 后的展示元数据
```

- 铸造点：校验完成后，Agent 响应层用校验器同一个映射生成；Hosted 投影只序列化。
- 正文裸 stable-id 引用（无 #N）：进 `citations[]`，**不进** `citations_detail`（无显示编号可给，宁缺毋假）。

**OPTION B — 回答分段 / 注解（annotations）**

```
answer + annotations: [{start_offset, end_offset, citation_refs: [N...]}]
```

- 优点：句级归属最精确，未来高亮最直接。
- 缺点：**偏移量语义脆弱**（模型输出文本在服务端被截断/规整后偏移漂移；Unicode 偏移 vs 字符偏移歧义）；要求服务端做正文位置解析=把"排序不得来自不安全正则"的风险变成必修项；对单步、≤1 工具的当前产品是明显过度设计；后端复杂度与测试面最大；与现有 fail-closed 校验器结构不匹配（校验器是"存在性"检查，不是位置分析）。

**OPTION C — 语义引用 ID 与显示编号分离（`[cite:c_...]`）**

```
citations: [{citation_id: "c_1", stable_id: "..."}]; answer 含 [cite:c_1]
```

- 优点：显示编号与语义键解耦，理论上重排自由。
- 缺点：要求**改模型提示词与输出语法**（模型依赖增大、失败面增大）；与现有 `#N` 校验器/grounding 规则冲突，需要重写校验语义；演示冻结的 Scenario A/B/C 口播与测试全部钉住 `#N` 行为；对"单次响应内作用域"的问题（§1.1.12）没有增益——`#N` 本身就是良好的 per-response 语义键。

### 1.3 权威映射在哪一层铸造（规格 §9）

**在"引用校验完成之后"、Agent 响应层铸造；Hosted 序列化只投影。** 理由：
- 语义所有权："`#N` 是否合法"由校验器裁决；映射是校验结论的直接投影，二者必须同源，否则出现两套权威。
- 不放在 Hosted 序列化层：那里拿不到包（`AgentResponse` 之外无上下文），会诱发"从 answer 文本再解析一遍"的反模式。
- 不放在 packager（模型调用前）：包的映射是超集（含未引用项），响应映射必须是**被引用子集**，只有校验器知道子集。

### 1.4 决策矩阵

| 维度（规格 §7） | A 响应级结构化对象 | B 分段/注解 | C 语义 ID 分离 |
| --- | --- | --- | --- |
| 正确性（#N→源 无猜测） | 高（同源投影） | 中（偏移依赖文本处理链） | 高 |
| 前端简单度 | 高（查表渲染；F7 已具备该分支） | 低（区间渲染+偏移对齐） | 中（新 token 渲染） |
| 后端复杂度 | 低（校验器返回值扩展） | 高（位置解析+一致性） | 中高（新语法+校验重写） |
| 模型依赖 | 无新增（维持 #N） | 无新增，但隐式依赖偏移稳定 | **新增**（新 token 语法） |
| 校验器兼容 | 完全兼容（同一映射） | 需扩展为位置感知 | 不兼容，需重写 |
| 失败行为 | 与现状同：未知→整体拒绝 | 偏移错位→静默错源风险 | 新失败模式需定义 |
| 向后兼容 | additive（见 §3 客户端约束） | additive 但结构重 | 破坏性（正文语法变） |
| 安全（防猜测/防伪造） | 强 | 中 | 强 |
| 可测试性 | 高（纯数据投影） | 中（偏移稳定性难测） | 中 |
| 可读性（降级到纯文本） | 高（正文即 #N） | 高 | 低（[cite:] 噪声） |
| 重复源行为 | 自然（键唯一） | 需区间合并规则 | 自然 |
| 流式兼容（未来） | 好（detail 可先于正文就绪） | 差（偏移需全文定稿） | 好 |
| 多步兼容（v0.7） | 好（每 run 一个映射） | 中 | 好 |
| API 演进负担 | 最小 | 大 | 大 |
| 迁移成本 | 最小 | 大 | 大 |

**结论：推荐 OPTION A；OPTION C 为候补（仅当未来出现"跨响应持久引用"需求才重评）；OPTION B 拒绝（过度设计 + 偏移脆弱性直接违反 §8 的排序安全要求）。**

### 1.5 N+1 的自然缓解

`CitationDetail` 携带 `title/label/source_type` 后，Mode-1 UI 渲染引用 chips **不再需要**为每个引用调 `GET /sources`（现状 pages/0 逐引用串行拉取，最坏 N×8s）；`/sources` 只在用户点开 viewer 时调用。因此 Option A 同时缓解了 §23 的 N+1，而无需新增 batch 端点（拒绝过早优化；batch 端点留待 N 实测变大）。

---

## 2. GAP B — 来源完整性公共契约

### 2.1 语义定义（规格 §10/§11 的直接回答）

**基线与计算**（由 F8/F9 决定）：完整性 = "当前 DB 行内容的重算指纹" vs "该实体被知识对象来源链接捕获时的基线快照"。因此——

1. **每种公共类型都可计算吗？** 不是。可计算性由"是否存在基线快照"决定：
   - `knowledge_object`：**总是可计算**（其来源链接各自的快照聚合；无任何链接 = unsourced，无完整性语义）。
   - `page` / `evidence`：**仅当被至少一个 KO 来源链接引用**（链接上有快照）；未被链接的实体没有基线 → 不可计算。
   - `knowledge_memory`：**永不可计算**——记忆是用户创作内容，不是外部材料的快照，"changed" 对它无意义。
   - `document`/`note`：非公共类型（F10），不适用。
2. **哪些类型可以合法地没有完整性状态？** `knowledge_memory`（永远）、未被链接的 `page`/`evidence`、unsourced 的 `knowledge_object`。
3. **何时计算？** **读取时**（source-detail 请求时刻 + Agent 工具读取时刻），与现状一致（F8/F9 "read-time"、"computed on every read"）。不在摄入时（快照捕获除外）、不缓存到 Agent run。
4. **成本？** 每次检查 = 1 次链接快照 SELECT + 1 次对 DB 内文本的 SHA-256。页面文本典型 ≤100KB → 毫秒级。无文件系统 I/O。
5. **会改动任何东西吗？** 不会（F8 只读；`inspect_source_integrity` 明示不刷新不写）。
6. **会触碰私有文件路径吗？** 不会——计算只读 DB 行；`documents.sha256` 是导入时记录的哈希列，不做文件重读。
7. **原始来源不可用（行被删）？** → `missing`（链接指向的实体不存在）；实体在但无可比对内容（如页面无任何文本层）→ `unknown`（指纹函数返回 None 的两种不可验证情形按行存在性区分）。
8. **"changed" 证明了什么？** 仅证明：**该实体的持久内容与其链接捕获时的基线快照不一致（快照漂移）**。不证明造假、篡改、恶意修改或内容错误。文档（`documents.sha256` 变化=重新导入替换）同理。
9. **要 reason codes 吗？** v0.6.3 **不要**——状态枚举已闭合、语义单一；reason codes 是 YAGNI，且会诱导 UI 转译成因果叙事（违背"changed≠造假"的红线）。留待真实需求出现。
10. **暴露 status only / +checked_at / +basis？** 推荐 **status + checked_at**。不暴露 basis/指纹载荷（§13）。
11. **"verified" 是不安全标签吗？** 是——禁用。本机制是**本地快照对比**，不是核验。"valid" 一律语义化为"与记录的基线快照一致"。
12. **概念命名？** 沿用 **`integrity_state`**（与冻结演示字段同名，见 §5 演示关系），API 文档必须内嵌定义："本地快照对比，非实时核验、非篡改检测"。

### 2.2 设计选项

**OPTION A — 扩展 `GET /v0.6/sources/{stable_id}` 响应（additive 可选字段）**

```
SourceResponse:
  stable_id / type / title / label          # 不变
  integrity_state: StrictStr | None         # 新增；None/缺省 = 该类型或该实体不适用
  integrity_checked_at: StrictStr | None    # 新增；状态判定时刻（UTC ISO-8601）
```

**OPTION B — 独立子资源 `GET /v0.6/sources/{stable_id}/integrity`**

- 优点：关注点分离；integrity 可独立限流。
- 缺点：UI 渲染 viewer 需要第二跳（现状 N 串行调用已经偏多）；端点面、错误面、限流类别（agent/source 之外第三类）全部扩大；单机 demo 负载下无收益。

**OPTION C — Agent 响应内嵌 run 时刻完整性快照**

- 缺点：把完整性计算耦合进每次 agent run（对从未被查看的引用也付成本）；引入"run 时刻状态 vs 查看时刻状态"双时间语义，恰好制造 §15 要避免的混乱；`inspect_source_integrity` 工具已覆盖"run 内查询完整性"的需求，无需在最终响应重复。

**结论：推荐 OPTION A。** 计算时机 = source-detail 请求时刻；成本毫秒级；单跳；与演示字段同名形成超集连续性。

### 2.3 状态词表与类型映射（闭合枚举，加性演化）

公共 `integrity_state` 采用闭合集合：`valid / changed / missing / unknown / partially_valid / not_applicable`（= `ContextFingerprintState` ∪ `partially_valid`；枚举扩展按 §3 规则属 additive）。

| 公共类型 | 计算基础 | 映射规则 |
| --- | --- | --- |
| knowledge_object | 来源链接聚合（F9） | `valid→valid`；`changed→changed`；`missing→missing`；`unknown→unknown`；`partially_valid→partially_valid`；`unsourced→not_applicable` |
| page / evidence | 其 KO 来源链接快照 vs 重算 | 单链接 1:1；多链接冲突按保守聚合：`changed > missing > unknown > valid`（任一链接发现漂移即报 changed，绝不把冲突"乐观折叠"为 valid） |
| page / evidence 未被任何 KO 链接引用 | 无基线 | 字段为 `None`（诚实：无从比较） |
| knowledge_memory | 无外部基线 | 字段为 `None`（永不适用的类型级语义） |

演示使用的 `ContextFingerprintState`（无 partially_valid）保持冻结不动；UI 对未知枚举值必须回退到通用说明（§4 演进规则 "enum expansion" 条款）。

### 2.4 `checked_at` 语义（规格 §14）

- **必要，非有害**：状态是请求时刻的点观测。没有 `checked_at`，UI 会把"来源一致"展示成无时效的事实——这正是规格警告的假实时保证。
- 取值 = 服务端完成对比的 UTC 时刻（**不是**快照捕获时刻、不是文件 mtime、不是 Agent run 时刻）。
- API 文档必须声明："该状态仅代表 checked_at 时刻的本地快照对比结果，不构成对过去或将来的保证。"
- 不引入 TTL/缓存——计算本身毫秒级，缓存只会制造假新鲜度。

### 2.5 Run 一致性（规格 §15）

竞态（run@T1 → 源变化 → /sources@T2 状态不同）**可接受，且应接受**：
- 采用 **latest-state lookup**：`/sources` 永远返回请求时刻的最新状态 + `checked_at` 交代判定时刻。
- 不采用 run 时刻快照（Option C 的弊病）；也不做"双状态"（过度设计）。
- UI 的既有文案已覆盖此语义（Mode-1 viewer 显示通用说明；演示免责"不代表实时核验"）。`checked_at` 让 UI 能显示"检查于 T"来显式交代时效。

---

## 3. 向后兼容与契约演进规则

### 3.1 关键约束（F5）

`PublicDTO(extra="forbid")` + `agent_client.model_validate` 意味着：**服务器新增字段会击落冻结的 v0.6.1 Mode-1 客户端**。这不是理论问题——是 pydantic 严格解析的确定行为。

因此：

- **`citations: string[]` 必须原样保留**（不合并进 detail、不改形状）→ YES。
- 新字段全部 **可选、带默认值、additive**（`citations_detail=()`、`integrity_state=None`、`integrity_checked_at=None`）。
- **版本偏斜政策**：冻结客户端与同版本服务器版本锁定（比赛期间两端同树，自洽）；Phase A 后端落地**必须与客户端容忍更新（`AgentRunResponse`/`SourceResponse` 声明新字段或改 ignore）同包发布**，并在冻结解除前不得让冻结 Mode-1 客户端指向已增强的服务器。外部 JSON 消费者天然兼容（未知字段被忽略）。
- `/v0.7` 何时才正当：删除/改名既有字段、改变既有字段语义或 nullability、要求新必填字段、`citations[]` 语义变化。**additive 可选字段不足以 bump 路径**——防止路径通胀。

### 3.2 演进规则（写入 ADR-008 §治理）

| 变更类别 | 分类 | 政策 |
| --- | --- | --- |
| 新增可选字段（带默认） | 兼容-additive | 允许在 `/v0.6`；必须附"缺字段回退"测试 |
| 枚举扩展（新值） | 兼容-additive（对 fail-closed 消费者） | 消费者契约：未知值→通用回退，不得崩溃；服务端不得删除已有值 |
| 新增必填字段 / 改 nullability / 删字段 / 改语义 | **破坏性** | 禁止在 `/v0.6`；需新路径 + 新 ADR |
| 排序保证 | 契约的一部分 | `citations[]`=校验器首次出现序；`citations_detail`=display_index 升序；两者均须测试钉住 |
| 错误码 | 封闭目录 | 只增不改义；`public_error` 兜底 internal_failure |

---

## 4. 前端视图模型与能力检测（规格 §19/§20）

- **不做"if mock 换渲染器"**：UI 单一渲染路径，两个模式都走 F7 已存在的可选增强分支——`citations_detail` 存在且非空 → 权威 chips；否则 → 纯 `citations[]` 列表（`display_index=None`）。`integrity_state` 存在 → 徽章；`None`/未知枚举 → `LIVE_INTEGRITY_NOTE` 通用说明。**现状代码已经是这个形状，设计只是把它升格为正式契约。**
- 能力检测规则（写入 ADR）：
  1. 字段存在且非空/非 None → 使用；
  2. 字段缺失/空/None → 回退到无猜测基线渲染；
  3. 枚举值不可识别 → 按通用说明渲染，不得臆测。
- **Source Viewer 字段分级**（规格 §20）：

| 字段 | 分级 | 说明 |
| --- | --- | --- |
| stable_id, type, title, label | SAFE PUBLIC | 现状；经 `safe_display_text` 过滤 |
| citation display_index（chips） | SAFE PUBLIC | Option A 新增；per-response 作用域 |
| integrity_state / integrity_checked_at | SAFE PUBLIC（Option A 新增） | 仅状态词表 + 时刻；不带原因/路径 |
| warnings（响应级，封闭文案） | SAFE PUBLIC | 现状 |
| 正文锚点区间 / 全文内容 | LOCAL-ONLY | 不进公共来源端点 |
| 文件路径 / 页面图片 / markdown 路径 | DO NOT EXPOSE | Hosted 用 `demo://` 伪路径，公共 DTO 永不含路径 |
| 指纹载荷 / sha256 / DB 主键 / SQL / 内部异常 | DO NOT EXPOSE | §13 红线 |

- **Local vs Hosted**（§25）：Local UI 想要更丰富的位置上下文时，走内部服务自有投影（现状如此），**不经公共 DTO**。Hosted 契约保持最小净化元数据；禁止为"两端统一"而让 Hosted 继承 Local 字段。

---

## 5. 演示契约关系（规格 §18）

- 方向保持：demo 继续是真实 DTO 的**超集**。真实 API 引入 `citations_detail` / `integrity_state` 后：
  - `DemoAgentRunResponse.citations_detail` 与真实字段**同名同形**（`DemoCitation` 可改为继承真实 `CitationDetail`、仅追加 `anchor_label`）→ demo-only 字段收窄为 `anchor_label`、`mode`、`demo_note`。
  - `DemoSourceResponse.integrity_state` 与真实字段同名（值域为冻结的 5 值枚举；真实侧是 6 值超集——UI 按能力检测回退）。
  - **变成共享的**：display_index/stable_id/source_type/title/label、integrity_state/integrity_checked_at。**保持 demo-only**：`mode="mock_demo"`（反冒充）、`anchor_label`（预置演示的讲解性锚点）、`demo_note`（"该状态来自演示数据"免责）。
- 这消除了当前"两个名字描述同一概念"的漂移风险（总审计 R-20 同族问题），且冻结演示的**语义零变化**（fixture 值、A/B/C 场景、文案全部不动）。

## 6. 错误模型（规格 §21）

| 情形 | 处置 | 理由 |
| --- | --- | --- |
| source not found / id 非法 | 现状 404/422（封闭目录） | 不变 |
| 引用映射不可用（服务器未启用该字段） | 字段缺省=`()`，UI 回退 | 能力缺失是状态，不是错误 |
| integrity 对该类型不适用 | `integrity_state=None` | 成功响应的一部分 |
| **changed** | **200 + 状态值** | 是成功的完整性结果，绝非 HTTP 失败 |
| integrity 计算内部失败（DB 异常等） | 省略可选字段 + 服务端 `LOGGER.warning`（仅失败类别，无正文/路径） | 唯一被批准的静默降级，严格限定于可选增强字段；主元数据照常返回 |
| 不支持/未知的 stable 类型 | 现状 404 | 不变 |

## 7. 性能（规格 §22/§23）

- Agent 响应内嵌 `citations_detail`：零额外计算（校验器已有映射）；序列化体增长 ≈ 每引用一行元数据。
- `/sources` 完整性检查：1 SELECT + 1 SHA-256（DB 内文本），毫秒级；不批处理、不缓存、不做后台刷新（全部属于过早优化）。
- N+1：Option A 使 chips 渲染零外部调用（§1.5），viewer 点击时单跳。现有 N（≤ 引用数，典型 ≤5）接受。
- 不新增限流类别；`/sources` 沿用 60/min/IP。

## 8. Hosted 安全面评估（规格 §24）

新增面 = 两个响应字段 + 一次只读指纹对比。评估：
- 无新端点、无新输入 → 请求面不变；
- 响应只增状态词表与 UTC 时刻，无路径/载荷/主键（§4 分级表）；
- 指纹对比是 DB 读，不触碰文件系统 → **不产生新攻击面**；唯一注意点是多链接冲突聚合不得回显内部细节（规则已闭合）；
- 只读、单 worker、fail-closed readiness、限流/体积/并发边界全部不受影响。

## 9. Schema（规格 §26）

**不需要 schema v13。** 引用映射是运行时投影（校验器映射已在内存）；完整性来自 v12 既有的链接快照列 + 读时重算。文档化红线：**禁止为"暴露一个 DTO 字段"而建 v13**。

---

## 10. 测试策略（规格 §27，设计即清单）

| # | 测试 | 锁定点 |
| --- | --- | --- |
| T1 | citations_detail 与校验器映射同源：#N→stable_id 逐项断言 | 权威性 |
| T2 | 回答含未映射 `#K` → 整体拒绝，无 detail 泄出 | 防猜测 |
| T3 | 伪造 stable_id → 拒绝 | 防伪造 |
| T4 | 一源多处 / 一句多源 → 单条目、多条目行为 | §1.1.7/8 |
| T5 | citations=首次出现序；citations_detail=升序；允许缺口 | 排序契约 |
| T6 | 旧客户端兼容：响应含新字段时，旧形状解析（模拟无字段模型）不受影响；新客户端容忍旧响应（缺字段默认值） | 版本偏斜 |
| T7 | integrity 四状态 + partially_valid + not_applicable 各一例 | 词表 |
| T8 | 多链接冲突聚合（changed>missing>unknown>valid） | 保守聚合 |
| T9 | 未链接 page / knowledge_memory → 字段 None | 能力边界 |
| T10 | changed 返回 200（非错误） | §6 |
| T11 | 完整性计算注入失败 → 字段省略 + warning 日志 + 200 | 降级 |
| T12 | 响应/来源无路径、无指纹载荷、无主键（复用现有 scrub 测试模式） | §13 |
| T13 | Mode 1：冻结客户端对**未增强**服务器照常；Phase B 后对增强服务器正确渲染 chips/徽章 | 兼容 |
| T14 | Mode 2：fixture 语义逐字节不变（现有 superset/snapshot 测试全绿） | 冻结 |
| T15 | checked_at 为有效 UTC ISO 且 ≥ 快照捕获时刻 | 新鲜度 |

**测试策略状态：READY（作为实现包的验收清单）。**

## 11. 迁移 / 发布计划（规格 §28）

- **Phase A（后端，additive）**：`CitationDetail` + 校验器映射投影 + `citations_detail` 字段 + 客户端容忍更新（同包！）→ **v0.6.2**。
- **Phase B（前端）**：demo_ui/页面按能力检测消费新字段（现成分支转正）；Mode 1 chips 显示权威 #N；冻结解除包内完成 → **v0.6.2**。
- **Phase C（integrity）**：`/sources` 增加两个可选字段 + 聚合规则 + 测试 T7-T15 → **v0.6.3**。
- **Phase D**：移除 Mode-1 viewer 的"通用说明"临时限制 → **v0.6.3**（测试/门禁全绿后）。
- 发布门禁：每阶段跑 `release_check.py`（R-01 整改后已与冻结树兼容）；破坏性检查禁止清单（§3.2）纳入门禁评审。

## 12. 冻结影响（规格 §29）

- **语义层面：无需解冻。** Mode 2 fixtures、A/B/C 场景文本、徽章/免责文案、mock/real 分离语义全部不动；演示响应在字段级仍是其自身（真实字段在 demo 中已有同名超集形状）。
- **代码层面：实现包必须触碰冻结清单所列文件**（`src/agent_client.py` 容忍更新、`src/demo_ui.py` 消费转正、`src/demo/contracts.py` 超集重建、`src/hosted_api/contracts.py` 加字段）——这正是冻结清单标注"需后端评审"的范围。
- **结论：本设计包不实现；实现需维护者在赛后单独授权一个整合包**（建议名：v0.6.2 Demo Integration Package）。在整合包落地前，冻结 Mode-1 客户端不得指向增强后的服务器（§3.1 版本偏斜政策）。

## 13. 版本归属（规格 §30）

| 变更 | 版本 | 理由（按 roadmap 语义） |
| --- | --- | --- |
| 权威引用映射 + 客户端容忍 | **v0.6.2** | 对已发布 /v0.6 契约的**兼容性补全**（演示期已知缺口 DEMO-03 的正式修复），patch 级 |
| 来源完整性公共契约 | **v0.6.3** | **新增只读 API 能力**（新计算、新字段、新语义文档），与修复分开以守住"fix ≠ capability"的演进纪律 |
| Agent 写能力 | v0.7.0（不变） | 独立 ADR（ADR-006 预留） |
| 不新造 v0.6.4+ | — | 无此需求 |

## 14. DeepSeek-Pro 决策包（紧凑版，无需重读仓库）

- **Problem:** 公共 /v0.6 契约缺 (a) 回答正文 `#N` 与 stable_id 的权威映射（演示靠 demo-only `citations_detail`，Mode 1 正确拒绝猜测）；(b) 来源完整性状态（演示用预置值，Mode 1 只有通用说明）。
- **Constraints:** 公共 DTO `extra="forbid"` + pydantic 严格客户端 → 服务器加字段会击落冻结客户端（版本偏斜须同包解决）；`#N` 映射已在 packager 铸造且校验 fail-closed；指纹=DB 行 SHA-256、基线在 KO 来源链接上、读时只读；公共来源类型仅 page/knowledge_object/knowledge_memory/evidence；Hosted DB-only；Local-first、最小暴露、防路径泄漏为红线。
- **Options:** 引用——A 响应级结构化引用对象 / B 正文注解偏移 / C 语义 cite-token；完整性——A 来源响应可选字段 / B 独立子资源 / C run 时刻内嵌。
- **Tradeoffs:** A 引用=最小复杂度+与校验器同源权威；B=偏移脆弱+过度设计；C=改模型语法+校验重写。完整性 A=单跳毫秒级+与演示同名超集；B=多一跳+面扩大；C=run 延迟耦合+双时间语义混乱。
- **Recommended:** 引用=OPTION A（`citations_detail: [{display_index,stable_id,source_type,title,label}]`，校验后由 Agent 响应层铸造，display_index≡正文 #N，允许缺口，升序）；完整性=OPTION A（`integrity_state`(闭合 6 值枚举)+`integrity_checked_at`，读取时计算，latest-state，changed=200 成功值）。`citations:string[]` 与 4 字段来源响应原样保留。
- **Open questions（供 DeepSeek 裁决）:** ① `partially_valid` 进入公共枚举 vs 降为 absent；② 多链接冲突聚合的保守序（changed>missing>unknown>valid）是否足够保守；③ `citations_detail` 命名沿用 demo 名 vs 更名 `citation_details`（本设计选同名以保超集连续性）；④ integrity 内部失败的"省略+日志"是否需要升级为显式警告字段。
- **Risk:** 主要风险=版本偏斜击落冻结客户端（已用同包发布+版本锁定政策缓解）；次要=字段被未来消费者误读为实时核验（用 checked_at+文档定义+拒绝"verified"标签缓解）。
- **Migration:** Phase A/B→v0.6.2（引用+客户端），Phase C/D→v0.6.3（完整性+去临时限制）；无 schema v13；无 /v0.7。
- **Tests:** §10 清单 T1-T15（READY）。
- **Freeze impact:** 语义零变化；实现触碰冻结清单"需后端评审"文件 → 赛后维护者单独授权整合包。

## 15. 最终推荐

- **引用：OPTION A**（结构化引用对象，响应级 additive 字段，Agent 响应层校验后铸造）。
- **完整性：OPTION A**（来源响应 additive 可选字段，读取时计算，status+checked_at）。
- **候补：** 引用 OPTION C（仅当出现跨响应持久引用需求）；完整性 OPTION B（仅当完整性需要独立限流/缓存策略）。
- **拒绝：** 引用 OPTION B（偏移脆弱、过度设计）；完整性 OPTION C（run 耦合 + 双时间语义）。
- 两项均：不破坏 /v0.6、不需要 schema v13、不解冻演示语义、实现归属 v0.6.2 / v0.6.3 两个赛后整合包。
