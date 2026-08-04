# m3-6 设计稿：client M3 orchestration create 命令消费

> 状态：**LOCKABLE**（盲预测 + 现状差距 + 关键设计决策 + 测试计划，全量 1308 passed；Kimi m3-6 审阅 bgwik7fx8 已处理：P1 photo 门禁已修 + 补正例测试，P2 跨仓契约 recovery 抑制测试已补，P3 doc/命名清理完成）｜目标 spec：DIGITAL-HUMAN-TECH-SPEC.md v1.4 §1.2/§1.5/§2.6/§5.3.9 ｜分支：feat/digital-human-protocol-v1_1
> 流程：盲预测 → 设计稿 → RED-first 实现+测试 → Kimi 审阅 → lock → commit。

## 0. 盲预测（调研后、实现前的固定断言）

1. **client 无 M3 create 消费桩**：`DirectorClient` 无 `create_orchestration_plan`；`ProjectStore` 无 orchestration 路径/落盘；CLI 无 `generation-orchestration-plan`；`_status_workflow` 无 M3 分支。M3 envelope 已 vendor（`OrchestrationPlanV1_1` @protocol/models.py:314 + registry @385），但无任何 M3 create 消费 → m3-6 全部新增。
2. **M3 触发点 = M1 released + M2（按需）charged + 里程碑投影到 orchestration**：M3 适用于 §1.2 矩阵 —— `avatar=photo`（photo 路径需 M2 charged，gate ③ 强制）、`voice_mode=own_voice`、`bgm≠none`。client 侧 M3 分支插入在 M2 分支之后：`_presenter_plan_charged(generation)` 为真（photo 路径）或 `_d13_brief_avatar != "photo"`（none+own_voice 路径，无 M2 row 天然跳过）→ 且 orchestration milestone 未 charged → `orchestration_plan_create_required`。photo 路径 M3 依赖 M2 charged：**M3 命令本身无 approval 参数**（裁决 B），不重复收集 M2 已确认的风险披露。
3. **M3 create payload 只有 `{capabilities}`**（裁决 B）：M3 无第三方媒体传输（F5 本机执行），无 approval 字段。响应 `OrchestrationPlanOut` = `{orchestration_plan, billing, recovery_catalog}`（recovery_catalog = base catalog，M3 无 provider 依赖）。
4. **OrchestrationPlan 验签复用 PresenterPlan 模式**：OrchestrationPlanV1_1 有 `created_at`/`content_expires_at`（同 PresenterPlan），签名验证走 `manifest_signing_bytes` + Ed25519 keyring + created_at 时间窗口。`verify_presenter_plan_signature` 是逐字镜像模板（manifest.py:284-368），换 plan 类即可。
5. **幂等**：server `_existing_plan`（仅 status in artifact_ready/charged）已护二次 create。client 落盘后重跑命令命中已有 orchestration-plan.json → 不二次扣费（CLI 层幂等，先于 manifest_ready gate，同 M2 presenter-plan 模式）。
6. **recovery 抑制**：M3 create 响应附 `get_base_catalog()`（无 provider directive）。`_recovery_workflow` 的 `m2_context` 参数镜像扩展：M3 上下文（orchestration-plan.json 已落盘）下 insufficient_credits → 同样落到 `_resume_error_workflow` 通用话术。**但 M3 上下文本质上与 M2 上下文共享同一信号**（charge 在 presenter_plan 之后）：`_project_in_orchestration_context` 直接用 `orchestration_plan_path.exists()`，并同时接受 M2 抑制语义。
7. **orchestration 不涉及 `_d13_heygen_configured` / `_stored_heygen_still_live`**：M3 gate 只查 `tts_engines` 含 f5（own_voice）与 M2 charged（photo），不查 heygen 配置。client M3 命令不需要 D13 泄漏守卫（那是 M2 的第三处理器边界）。

## 1. 现状差距（调研确认）

| 项 | 现状 | m3-6 需要 |
|---|---|---|
| client HTTP 方法 | 无 `create_orchestration_plan` | 新增（镜像 `create_presenter_plan` @director.py:526，payload 无 approval，URL `/orchestration-plan`，响应 `_orchestration_plan_out` envelope 校验）|
| 落盘/验签 | 无 orchestration 路径/验签 | `ProjectStore.orchestration_plan_path` + `save_orchestration_plan` + `verify_orchestration_plan_signature`（created_at 窗口）|
| CLI 命令 | 无 `generation-orchestration-plan` | 新增（镜像 `generation_presenter_plan` @commands/director.py:1298，无 approval 门禁，M3 前置 gate）|
| `_status_workflow` | 无 M3 分支 | M2 charged（photo）或 avatar!=photo（none）后 → orchestration-plan create next_action |
| 幂等 | 无 | orchestration-plan.json 已存在 → 直接返回既有，不二次扣费 |
| recovery 抑制 | 仅 `m2_context` | M3 上下文（orchestration-plan.json 落盘）insufficient_credits → 通用话术 |

**已有（复用，不改）**：OrchestrationPlanV1_1 vendor schema + registry；`OrchestrationPlanOut` envelope 结构（`{orchestration_plan, billing, recovery_catalog}`）；`derive_billing_state` / `_m2_generation_view` / `_m2_charges_from_project`（billing 投影，orchestration milestone 合并进同一投影）；`_stored_capabilities` / `_stored_heygen_still_live` / `_d13_brief_avatar`；`_recovery_workflow` 的 m2_context 抑制模式。

## 2. 关键设计决策

### 2.1 `DirectorClient.create_orchestration_plan`

- `POST /director/generations/{generation_id}/orchestration-plan`（server m3-4 route：director.py:489-541）。
- payload：`{capabilities: ClientCapabilitiesV1_1}` —— **无 approval 字段**（裁决 B：M3 无第三方媒体传输，F5 本机执行）。
- 响应 `OrchestrationPlanOut` = `{orchestration_plan, billing, recovery_catalog}`（recovery_catalog = base catalog，M3 无 provider 依赖）。
- `_orchestration_plan_out` envelope 校验（fail-closed）：`orchestration_plan` dict + `billing` list + v1.1 `OrchestrationPlanV1_1.model_validate` + `recovery_catalog` None|dict。
- 错误：v1.1 `ErrorEnvelopeV1_1` 严格校验（照 `_error`）。

### 2.2 `ProjectStore.save_orchestration_plan` + 验签

- 新路径 `orchestration-plan.json`，mode 0o444（只读，同 manifest/presenter-plan）。
- digest 绑定：`state.payload["production_manifest_digest"]` 必须匹配 plan 的 `production_manifest_digest`；`capability_digest` 绑定同 manifest。**orchestration 的 `presenter_plan_digest` 是可选字段（None 合法，none+own_voice 无 M2）**，save 不强制它有值。
- 验签：`verify_orchestration_plan_signature`（镜像 `verify_presenter_plan_signature`，created_at 窗口 + content_expires_at 有效性）。
- `_verify_documents` 加 orchestration 分支（同 presenter_plan 分支）。

### 2.3 `generation-orchestration-plan` CLI 命令

- 前置：session confirmed、generation_id 已锁定、manifest 已 release、M3 适用（photo → M2 charged / none+own_voice → 无 M2 row）、capabilities 采集/复用。
- **无 approval 门禁**：M3 扣费不需要独立确认（裁决 B）。photo 路径依赖 M2 charged（M2 已确认过风险披露）；none+own_voice 路径无第三方传输。
- 调用 `create_orchestration_plan` → `save_orchestration_plan`（落盘+验签）→ `_status_workflow` 投影。
- 幂等：orchestration-plan.json 已存在 → 直接返回既有，不二次扣费。

### 2.4 `_status_workflow` M3 触发分支

- 插入在 M2 分支之后（manifest release 分支内部）：`gen_status=="ready"` 且 `_can_release_manifest` 且 M3 适用（M2 charged 或 avatar!=photo）且 orchestration 未 charged → `orchestration_plan.create` next_action。
- 注意优先级：awaiting_credits/resume 仍最先（钱优先）。

### 2.5 recovery 抑制（M3 上下文）

- `_recovery_workflow` 的 `m2_context` 参数语义扩展：调用点 `_project_in_m2_context` 改为同时检查 orchestration-plan.json —— M3 上下文（orchestration 落盘）下 insufficient_credits → 落到 `_resume_error_workflow` 通用 `credit_top_up_required`（不误给 m1 话术）。
- 跨仓契约：断言 client M3 上下文不把 insufficient_credits 解析成 m1 话术。

### 2.6 不做的事（边界）

- **不实现 F5 真实本地执行 / ffmpeg 渲染**（M3 只签算法/模板 ID/占位契约，执行在 client，但执行引擎属后续里程碑）。
- **不动 agentmesh-core / server 核心**（红线）。
- **不改 M1/M2 已锁代码**（含 save_presenter_plan / _status_workflow M2 分支）。
- **不重复 M2 approval**（M3 无第三方传输）。

## 3. 测试计划（RED-first）

### 3.1 client（tests/test_orchestration_plan_client.py + test_generation_orchestration_plan_cli.py）

1. `test_create_orchestration_plan_posts_without_approval` — URL/方法/payload 断言（仅 capabilities，无 approval）。
2. `test_create_orchestration_plan_rejects_v1_0_response` — v1.0 响应 fail-closed。
3. `test_status_workflow_m2_charged_offers_orchestration_plan` — photo + M2 charged → orchestration-plan create。
4. `test_status_workflow_own_voice_offers_orchestration_plan` — avatar=none + manifest ready → orchestration-plan create。
5. `test_save_orchestration_plan_rejects_mismatched_manifest_digest` — digest 绑定失败 → manifest_incompatible。
6. `test_save_orchestration_plan_rejects_tampered_signature` — 篡改 → manifest_signature_invalid。
7. CLI：`test_generation_orchestration_plan_creates_and_saves`（落盘+验签+state 升级）。
8. CLI：`test_generation_orchestration_plan_idempotent_second_run`（不二次扣费）。
9. recovery：M3 上下文 insufficient_credits → 通用话术。

### 3.2 跨仓契约（test_cross_repo_contracts.py）

- `test_client_orchestration_plan_path_matches_server_route` — client `create_orchestration_plan` URL/方法 == server route。
- `test_client_orchestration_plan_has_no_approval` — client payload 无 approval 字段（裁决 B）。
- `test_client_m3_context_recovery_suppression` — M3 上下文不误给 m1 话术。

### 3.3 全量回归

- client：`.venv/bin/python -m pytest tests/ -q`（基线 1276 passed）。
- ruff：新增代码零错误。

## 4. 参考锚点

- server route：`lecturecast-server/app/routes/director.py:489-541` `create_orchestration_plan`
- server schema：`lecturecast-server/app/schemas/presenter.py:63-88`（CreateOrchestrationPlanIn / OrchestrationPlanOut）、`:150-193`（F5VoiceOrchestration / OrchestrationPlanV1_1）
- server gate：`lecturecast-server/app/services/generations.py:159-204` `validate_orchestration_capabilities`
- client `_error`（v1.1 envelope 严格校验）：`director.py:259-302`
- client `create_presenter_plan` / `_presenter_plan_out`（M3 镜像）：`director.py:505-557`
- client `save_presenter_plan`（落盘模式）：`project.py:352-397`
- client `verify_presenter_plan_signature`（验签模式）：`manifest.py:284-368`
- client `generation_presenter_plan`（CLI 模式）：`commands/director.py:1298-1464`
- client `_status_workflow`（M3 分支插入点）：`commands/director.py:315-376`
- client `_project_in_m2_context`（M3 抑制扩展）：`commands/director.py:1504-1520`
- client `_recovery_workflow`（m2_context 抑制）：`commands/director.py:1523-1602`
- client `derive_billing_state`：`director.py:50-78`
- client `OrchestrationPlanV1_1`：`protocol/models.py:314-320` + registry @385
