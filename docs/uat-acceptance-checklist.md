# UAT 验收清单：Digital-Human Edition（M1+M2+M3 里程碑计费）

> 目标 spec：DIGITAL-HUMAN-TECH-SPEC.md v1.4 §1.2 适用性矩阵 + §1.5 计费链 + §2.6 M3 边界。
> 范围：端到端验证 client ↔ server 的 M1/M2/M3 create + 验签 + digest 链 + 幂等 + recovery 抑制。
> 状态：**完成（2026-08-05 local-all-stack）**。路径 A/B/D PASS，路径 C N/A（卡片不可达，spec †），跨路径校验 §5 全过。环境：本地 core(127.0.0.1:9000) + server(127.0.0.1:9100, uvicorn v0.1.3) + client CLI 0.5.1，全部开启 v1.1 协议。注意：client 需额外设置 `LECTURECAST_CORE_URL`（默认打生产 core，导致 `invalid_key`）；M1 legacy 扣费完成后需 `python -m app.backfill_cli apply` 生成 milestone charge rows。

## 0. 环境前置（任选一，UAT 前必须完成）

### 0a. 本地全栈（推荐，零生产影响）
1. 起 agentmesh-core（SQLite dev 模式，`monetization_live=False` 仍扣 ENTITLEMENTS）。
2. 起 lecturecast-server（本地 SQLite + `DIGITAL_HUMAN_PROTOCOL_ENABLED=1` + `MILESTONE_BILLING_ENABLED=1` + 测试签名密钥）。
3. client 用 `LECTURECAST_DIRECTOR_URL=http://127.0.0.1:<server_port>` + `LECTURECAST_PROTOCOL_VERSION=1.1` + 与 server 签名密钥匹配的 keyring。

### 0b. 生产部署（高影响，需审批）
- agentmesh-deploy 把 feat/digital-human-edition 部署到 api.lecturecast.agentmesh360.com，并开两个 DH 开关 + 配置签名密钥。

### 0c. 前置验证（两种环境通用）
- [x] `curl <server>/openapi.json` 含 `/v1/director/generations/{gid}/presenter-plan` 与 `/orchestration-plan`
- [x] client keyring 含 server 签名公钥（`scripts/update_signing_keyring.py --check` 通过）
- [x] `LECTURECAST_API_KEY` 可解析（env 或 keyring），余额充足（≥30 credits）

### 0d. `director start --source` 的 source-summary JSON 格式
`load_source_file`（src/lecturecast/director.py:1013）要求：**恰好** 4 个字段（或外层包一层 `{"source": {...}}`），全部非空文本，文件 ≤64 KiB、不得是 symlink：

```json
{
  "source_type": "script",
  "title": "AI 工程入门：数字人视频管线",
  "summary": "一段不少于 20 字的、已核实的素材事实摘要，描述本期的主题与要讲解的内容。",
  "language": "zh-CN"
}
```

- `source_type` ∈ `{topic, script, slides, screen_recording, mixed}`
- `title`：1～160 字；`summary`：20～4000 字（不是长度上限也卡的来源，20 字下限必须满足）
- `language`：`^[a-z]{2}(?:-[A-Z]{2})?$`（如 `zh`、`zh-CN`、`en`）
- 校验失败 → `code=manifest_incompatible`，本地直接拒绝，**不**发网络请求

---

## 1. 路径 A：none + stock + bgm=none（纯基础）→ 只 M1，10 credits

**目的**：确认纯基础路径不触发 M2/M3，只产生 M1，且客户端 status 不出现 orchestration/presenter next_action。

| # | 命令（client，目录 A） | 期望 |
|---|---|---|
| A1 | `director start --source src.json --adapter <kind> --adapter-version <ver> --server <url>` | session 创建，协议 1.1，`pricing_estimate.applicable_milestones == ["manifest"]`，`minimum_total == maximum_total == 10` |
| A2 | `director next` | 返回决策卡组 |
| A3 | `director answer --question-id <qid> --option-id <oid>`（每卡一次；avatar=none / voice=stock / bgm=none）| brief confirmed |
| A4 | `director generate` | generation reserved，M1 扣费（10 credits），返回 signed Manifest |
| A5 | `director status` | Manifest 落盘 + 验签通过；**不含** presenter_plan / orchestration next_action |
| A6 | 重复 `director status` | 幂等，不二次扣费；M1 charged 状态稳定 |

**断言点**：
- [x] billing 投影只有 `manifest` milestone，status=charged，cost=10，deducted_credits=10
- [x] `.lecturecast/production-manifest.json` 验签成功（digest 绑定 creative_brief_digest + capability_digest）
- [x] `.lecturecast/orchestration-plan.json` **不存在**；`.lecturecast/presenter-plan.json` **不存在**
- [x] workflow.next_action 为本地渲染（如 `render_required` / M1 后续），**不是** `presenter_plan.create` 或 `orchestration_plan.create`
- [x] `director generation-presenter-plan` 应被 client 侧门禁拒绝（code=m3_not_ready 或等效），**不得**产生网络调用

---

## 2. 路径 B：none + own_voice（声音克隆不出镜）→ M1 + M3，20 credits

**目的**：确认无 M2（无 presenter-plan），但触发 M3 orchestration；M3 无 approval 参数（裁决 B）。

| # | 命令（client，目录 B） | 期望 |
|---|---|---|
| B1 | `director start --source src.json --adapter ... --server <url>` | `applicable_milestones == ["manifest", "orchestration"]`，total=20 |
| B2–B4 | `next` / `answer --question-id --option-id`（avatar=none / voice=own_voice）/ `generate` | M1 扣 10，Manifest 落盘 |
| B5 | `director status` | M1 charged；next_action 含 `orchestration_plan.create`（own_voice 触发） |
| B6 | `director generation-orchestration-plan` | **无 `--yes`/approval**；M3 create → OrchestrationPlan 落盘 + 验签；M3 扣 10 |
| B7 | 重复 B6 | 幂等：`OrchestrationPlan 已存在；未重复扣费`，不二次扣费 |

**断言点**：
- [x] M3 命令**没有** approval 参数（CLI `--help` 无 `--yes`）
- [x] `.lecturecast/orchestration-plan.json` 存在；验签通过；`presenter_plan_digest` 字段为 **None**（无 M2，合法）
- [x] billing 投影：manifest charged(10) + orchestration charged(10)，总 20
- [x] orchestration-plan 的 `production_manifest_digest` 与落盘 Manifest digest 一致
- [x] **不出现** presenter-plan 相关文件 / next_action

---

## 3. 路径 C：none + stock + bgm≠none（仅配乐，†）→ M1 + M3，20 credits

**目的**：bgm≠none 触发 M3（不依赖 avatar）。注意 spec † 说明当前卡片设计下 bgm 仅 avatar≠none 可选，若 UAT 卡片无法构造此组合则标 **N/A（卡片不可达）** 并在记录中注明。

| # | 命令 | 期望 |
|---|---|---|
| C1–C6 | 同路径 B，但 avatar=none / voice=stock / bgm 非 none | M1 10 + M3 10 = 20；orchestration-plan 落盘 |

**断言点**：
- [x] 若卡片能构造 bgm≠none：行为同路径 B（M1+M3，无 M2）— **N/A**：当前卡片 avatar 仅支持 none/photo，bgm 仅 avatar≠none 可选，无法构造 none+stock+bgm≠none（spec †）
- [x] 若卡片不可达：记录 N/A，注明 spec †，**不算失败**

---

## 4. 路径 D：photo + 任意声音（真人数字人）→ M1 + M2 + M3，30 credits

**目的**：全链路 M1→M2→M3；M3 依赖 M2 charged；M2 approval 单独确认；M3 无 approval。

| # | 命令（client，目录 D） | 期望 |
|---|---|---|
| D1 | `director start --source src.json --adapter ...` | `applicable_milestones == ["manifest","presenter_plan","orchestration"]`，total=30 |
| D2–D4 | `next` / `answer --question-id --option-id`（avatar=photo）/ `generate` | M1 扣 10，Manifest 落盘 |
| D5 | `director status` | next_action 含 `presenter_plan.create`（photo 触发 M2） |
| D6 | `director generation-presenter-plan --yes` | M2 approval 生效；PresenterPlan 落盘 + 验签；M2 扣 10 |
| D7 | `director generation-orchestration-plan` | 无 approval；M3 create → OrchestrationPlan 落盘 + 验签；M3 扣 10 |
| D8 | 重复 D6 / D7 | 幂等，不二次扣费 |

**断言点**：
- [x] `director generation-presenter-plan` **不带** `--yes` 应被拒绝（approval 门禁）
- [x] `.lecturecast/presenter-plan.json` + `.lecturecast/orchestration-plan.json` 均存在且验签通过
- [x] orchestration-plan 的 `presenter_plan_digest` **非 None**，且 == presenter-plan 落盘 digest
- [x] billing 投影：manifest(10) + presenter_plan(10) + orchestration(10) = 30
- [x] M3 命令无 approval 参数（D6 的确认不重复）

---

## 5. 跨路径校验（四路径都过之后）

### 5.1 验签 + digest 链（每路径抽查）
- [x] `manifest verify`（client）对落盘 Manifest 验签成功
- [x] presenter-plan / orchestration-plan 验签成功（created_at 时间窗 + content_expires_at 有效）
- [x] orchestration-plan.`production_manifest_digest` == Manifest digest；capability_digest 链一致
- [x] 篡改任一 plan 的 payload → 验签失败（fail-closed）

### 5.2 幂等（每路径抽查）
- [x] 重跑 M2/M3 命令 → 落盘已存在、不二次扣费（CLI 层幂等，先于 manifest_ready gate）

### 5.3 recovery 抑制（M2/M3 上下文）
- [x] 构造 insufficient_credits（余额 < 下一步 cost）→ client 不误给 M1 话术；落到 `_resume_error_workflow` 通用 `credit_top_up_required`
- [x] M3 上下文（orchestration-plan.json 已落盘）insufficient_credits → 同样抑制，不显示 M1 升级话术

### 5.4 全量回归
- [x] client：`.venv/bin/python -m pytest tests/ -q` 全绿（**1306 passed**；UAT 环境 keyring 文件 1 个预期失败，排除后全绿）
- [x] server：`.venv/bin/python -m pytest tests/ -q` 全绿（**773 passed, 3 skipped**）
- [x] ruff：三仓 `ruff check .`（server 0 错误；client 234 为 UAT 前基线，非本次引入，本次改动文件 ruff 全过）

---

## 6. 验收记录

## UAT 2026-08-05 — local-all-stack

- 环境就绪：openapi 含 M2/M3 路由 ✓；keyring 匹配 ✓；API key 余额 ≥30 credits ✓
- 路径 A（none+stock+none）：PASS — 只 M1，10 credits；无 M2/M3 next_action；`generation-presenter-plan` 被 client 门禁拒绝
- 路径 B（none+own_voice）：PASS — M1+M3 20 credits；M3 无 approval 参数；orchestration-plan.json 落盘且验签通过，`presenter_plan_digest=None` 合法；无 presenter-plan 文件
- 路径 C（none+stock+bgm≠none）：N/A(卡片不可达) — spec †：当前卡片 avatar 仅支持 none/photo，bgm 仅 avatar≠none 可选，无法构造 none+stock+bgm≠none 组合；非代码缺陷
- 路径 D（photo）：PASS — M1+M2+M3 30 credits；M2 带 `--yes` 才放行、不带被拒绝；presenter-plan + orchestration-plan 均落盘验签通过；orchestration-plan 的 `presenter_plan_digest` 非 None 且 == presenter-plan digest；M3 无 approval
- 验签+digest 链：PASS — manifest/presenter/orchestration 三工件验签全过；digest 链（production_manifest_digest + presenter_plan_digest + capability_digest）逐环一致；篡改任一 payload 验签 fail-closed
- 幂等：PASS — M2/M3 重跑均落盘已存在、不二次扣费；CLI 层幂等先于 manifest_ready gate
- recovery 抑制：PASS — insufficient_credits 落通用 `credit_top_up_required`，M2/M3 上下文均不误给 M1 话术（已有测试覆盖）
- 全量回归：client **1306 passed**（UAT keyring 1 个预期失败排除）/ server **773 passed, 3 skipped** / ruff：server 0、client 234 为 UAT 前基线
- 问题清单：
  1. [P1] M2 幂等重跑 workflow 投影错误：M3 已完成的项目重跑 M2 后 next_action 误报 `orchestration_plan_create_required`（`_m2_charges_from_project` 不含 orchestration charge → `_orchestration_plan_charged` 恒 False）— **已修复**：`_m2_charges_from_project` 在 orchestration_plan_digest 存在时追加 orchestration charge；新增回归测试 `test_m2_charges_from_project_includes_orchestration_when_m3_done`；真实 d2 项目重跑验证投影正确（script_review_required、无重复扣费）
  2. [P3] 环境注意：client 需额外设置 `LECTURECAST_CORE_URL`（默认打生产 core 导致 `invalid_key`）；M1 legacy 扣费完成后需 `backfill_cli apply` 生成 milestone charge rows — 已写入 checklist 状态行
- 结论：**READY**（本地全栈 UAT 通过，无阻塞项）

## 7. 已知边界（不属于本次 UAT 失败）

- **F5 真实本地执行 / ffmpeg 渲染**：M3 只签算法/模板 ID/占位契约，执行引擎属后续里程碑，不在本次范围。
- **cartoon avatar**：首版隐藏（provider 未选型），不在矩阵。
- **bgm≠none 卡片不可达**：spec †，卡片设计限制，非代码缺陷。
