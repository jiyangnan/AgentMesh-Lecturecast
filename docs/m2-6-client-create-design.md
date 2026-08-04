# m2-6 设计稿：client M2 create 命令消费

> 状态：**LOCKED**（盲预测 + 现状差距 + 关键设计决策 + 测试计划 + Kimi 审阅 PASS）｜目标 spec：DIGITAL-HUMAN-TECH-SPEC.md v1.4 §2.2/§2.6/§5.3.9 ｜分支：feat/digital-human-edition
> 流程：盲预测 → 设计稿 → RED-first 实现+测试 → Kimi 审阅 → lock → commit。

## 0. 盲预测（调研后、实现前的固定断言）

1. **client 无 M2 create 消费桩**：`_status_workflow` 只处理 awaiting_credits/resume、manifest release、credit_returned，无 `presenter_plan` 分支；`DirectorClient` 无 `create_presenter_plan` 方法；`ProjectStore` 无 presenter_plan 路径。→ m2-6 全部新增。
2. **M2 触发点 = manifest released 且 brief avatar=photo**：manifest 落盘后（status 命令 save_manifest 成功 → `project.status=="manifest_ready"`），若 `brief.presenter.avatar=="photo"` 且 generation billing_state 未到 presenter_plan charged → 提供 presenter-plan create。M1（own_voice）路径 avatar=none → 永不触发。avatar 判断复用 `_d13_brief_avatar`（commands/director.py:855）。
3. **M2 approval 凭证不落库、由用户显式确认**：server `M2Approval` 裁决 C——`{approved: bool, disclosure_version: "heygen-transfer-2026-07-27"}`，不写数据库。client 侧由 `director presenter-plan` 命令收集用户确认，然后上传 `{capabilities, approval}`。默认 `--yes` 才 approved=true（cost 门禁，同 manifest 模式）。
4. **PresenterPlan 验签复用 manifest 模式 + created_at 窗口**：PresenterPlanV1_1 有 `created_at`/`content_expires_at`，签名验证走 `manifest_signing_bytes` canonical bytes + Ed25519 keyring + created_at 时间窗口（catalog 无此字段所以 catalog 版无窗口；presenter 版需要，参照 `verify_manifest` 的 created_at 逻辑）。
5. **recovery.py:21 映射是 M2 话术缺陷**：`insufficient_credits → m1_insufficient_credits` 会让 M2 resume 402 得 M1 话术。需让 M2 上下文得到 `m2_insufficient_credits`（server catalog 有 `m2_insufficient_credits` 则直接命中；没有则需 server 侧加）。→ **m2-6 只做 client 侧解析 + 契约测试，server catalog 加 directive 属 server 侧，需确认是否已存在**。
6. **幂等**：server `create_and_charge` 有 `_existing_plan` 幂等（同 generation 二次 create 返回既有 plan）。client 落盘后重跑命令应命中已有 presenter-plan 文件 → 不二次扣费。落盘 digest 需与 generation `presenter_plan_digest` / milestone_charge 的 `artifact_digest` 对齐（`_can_release_manifest` 同款逻辑）。

## 1. 现状差距（调研确认）

| 项 | 现状 | m2-6 需要 |
|---|---|---|
| client HTTP 方法 | 无 `create_presenter_plan` | 新增（照 `resume_generation` 模式，payload 含 capabilities + approval，响应用 `PresenterPlanV1_1.model_validate`）|
| CLI 命令 | 无 `generation-presenter-plan` | 新增（收集 approval 凭证 → 调用 → 落盘 → 验签 → digest 绑定）|
| `_status_workflow` | 无 M2 分支 | manifest released + avatar=photo → presenter-plan create next_action |
| 落盘/验签 | 无 presenter_plan 路径/验签 | ProjectStore 新增 `presenter_plan_path` + `save_presenter_plan` + `verify_presenter_plan_signature`（created_at 窗口）|
| recovery 映射 | `insufficient_credits → m1_insufficient_credits` | M2 上下文 → `m2_insufficient_credits`（client 侧 + 跨仓契约）|

## 2. 关键设计决策

### 2.1 `DirectorClient.create_presenter_plan`

- `POST /director/generations/{generation_id}/presenter-plan`（server m2-4 route：director.py:335-388）。
- payload：`{capabilities: ClientCapabilitiesV1_1, approval: M2Approval}`，approval = `{approved: bool, disclosure_version: "heygen-transfer-2026-07-27"}`。
- 响应：`PresenterPlanOut` = `{presenter_plan: PresenterPlanV1_1, billing: list[MilestoneChargeOut], recovery_catalog}`。
- v1.1 解析：`PresenterPlanV1_1.model_validate` + billing 投影（照 `_generation` 全量校验模式）。
- 错误：v1.1 `ErrorEnvelopeV1_1` 严格校验（照 `_error`）。

### 2.2 `generation-presenter-plan` CLI 命令

- 前置：session confirmed、generation_id 已锁定、manifest 已 release（`project.status=="manifest_ready"`）、`_d13_brief_avatar=="photo"`、capabilities 采集/复用（照 generate 的 `_stored_capabilities` + B1 stale guard）。
- `--yes` 门禁：默认拒绝（cost 门禁），`--yes` 才 approved=true。交互式提示用户已阅读 heygen-transfer-2026-07-27 披露。
- 调用 `create_presenter_plan` → `save_presenter_plan`（落盘+验签）→ `_status_workflow` 投影。
- 幂等：presenter_plan 已存在 → 直接返回既有，不二次扣费。

### 2.3 `_status_workflow` M2 触发分支

- 插入在 manifest release 分支之后：`gen_status=="ready"` 且 `_can_release_manifest` 为真 且 `_d13_brief_avatar=="photo"` 且 billing_state 未到 presenter_plan charged → `presenter_plan.review` next_action。
- 注意优先级：awaiting_credits/resume 仍最先（钱优先）。

### 2.4 `ProjectStore.save_presenter_plan` + 验签

- 新路径 `presenter-plan.json`，mode 0o444（只读，同 manifest）。
- digest 绑定：`state.payload["production_manifest_digest"]` 必须匹配 plan 的 `production_manifest_digest`；`capability_digest` 绑定同 manifest。
- 验签：`verify_presenter_plan_signature`，created_at 时间窗口 + content_expires_at 有效性。

### 2.5 recovery 映射修复（client 侧）

**调研确认：server catalog 只有 `m1_insufficient_credits`（recovery_catalog.py:95），无 `m2_insufficient_credits` directive。** 这决定了修复方向不是新增映射，而是上下文感知抑制 + provider catalog 持久化：

- **M2 create 响应附带 provider catalog**（`_presenter_out` → `get_provider_catalog()`，含 heygen_key_invalid / heygen_balance_insufficient / heygen_rate_limited）。client 落盘 presenter_plan 时，把响应里的 `recovery_catalog` 持久化进 v1.3 state（`DirectorStateStore.update` 已有该字段，schema 1.3 已支持）。
- **M2 上下文 resume 402**：`failure_kind_for_error` 仍映射 `insufficient_credits → m1_insufficient_credits`，但 M2 上下文（presenter_plan charge 存在 / heygen 配置）应**抑制** m1 映射，让 `_resume_error_workflow` 落到 `credit_top_up_required` 通用话术，而不是错误的 m1 话术。具体：client 检测到 M2 上下文时，`recover_from_failure` 查不到 m1 directive（provider catalog 无 m1 key）→ 天然返回 None → 落到 `_resume_error_workflow`。**这正是正确行为**——m1 话术是 M1 专属，M2 阶段额度不足应给通用 credit_top_up_required。
- **跨仓契约测试**：断言 server provider catalog（M2 附带）不含 `m1_insufficient_credits`，且 client 在 M2 上下文不把 insufficient_credits 解析成 m1 话术。

### 2.6 不做的事（边界）

- **不实现 M3 orchestration**（M3 主线）。
- **不动 agentmesh-core / server 核心**（红线）。
- **不改 manifest 落盘/验签**（已 lock）。
- **不实现 M1 里程碑化**（M1 主线）。

## 3. 测试计划（RED-first）

### 3.1 client（tests/）

1. `test_create_presenter_plan_http` — URL/方法/payload 断言（capabilities + approval）。
2. `test_presenter_plan_parse_v1_1` — 完整 PresenterPlanOut 解析 + 敏感字段防御。
3. `test_save_presenter_plan_digest_bind` — digest 绑定失败 → manifest_incompatible。
4. `test_save_presenter_plan_verify_signature` — 篡改 → manifest_signature_invalid。
5. `test_presenter_plan_recovery_mapping` — M2 上下文 insufficient_credits → m2_insufficient_credits。
6. `test_status_workflow_presenter_plan_branch` — avatar=photo + manifest ready → presenter_plan.review。
7. 幂等测试 — 二次 create 不二次扣费。

### 3.2 跨仓契约（test_cross_repo_contracts.py）

- `test_client_presenter_plan_path_matches_server_route` — client `create_presenter_plan` URL/方法 == server route（`@router.post /director/generations/{generation_id}/presenter-plan`）。
- `test_m2_disclosure_version_matches_server` — client `DISCLOSURE_VERSION` == server `M2Approval.disclosure_version` Literal。
- `test_server_provider_catalog_has_no_m1_directive` — server provider catalog（M2 附带）不含 `m1_insufficient_credits`（§2.5：M2 阶段额度不足不该给 M1 话术）。

### 3.3 全量回归

- client：`uv run pytest -q`。
- server（如需确认 catalog）：`UV_CACHE_DIR=/tmp/lc-uv-cache uv run --project . pytest -q`。

## 4. 参考锚点

- server route：`lecturecast-server/app/routes/director.py:335-388` `create_presenter_plan`
- server gate：`lecturecast-server/app/services/generations.py:100-156` `validate_presenter_capabilities`
- client `_error`（v1.1 envelope 严格校验）：`director.py:213-257`
- client `_generation`（v1.1 全量校验）：`director.py:317-347`
- client `resume_generation`（HTTP 方法模式）：`director.py:432-447`
- client `_status_workflow`（M2 分支插入点）：`director.py:304-344`
- client `save_manifest`（落盘模式）：`project.py:305-339`
- client `verify_manifest`（created_at 窗口验签模式）：`manifest.py:133-198`
- client `verify_recovery_catalog_signature`（无窗口验签模式）：`manifest.py:201-281`
- client `recovery.py:20-23` `_ERROR_CODE_TO_FAILURE_KIND`
- client `_d13_brief_avatar`（avatar 判断）：`commands/director.py:855`
- server `PresenterPlanV1_1`：`lecturecast-server/app/schemas/presenter.py:95-119`
- server `M2Approval`/`CreatePresenterPlanIn`/`PresenterPlanOut`：`presenter.py:25-60`

## 5. Kimi 审阅 + LOCK 记录

**全量：1272 passed**（基线 d19b758 m2-5 = 1242，m2-6 新增 30 测：CLI 6 + PresenterPlan client 15 + recovery 抑制 6 + M2 create 跨仓契约 3；三仓并排下全部含 cross-repo 测试）。ruff：本次新增代码零错误（3 个 F401 为 HEAD 既有）。

### Kimi 逐项裁决（round-2 收敛）

| # | 疑点 | 裁决 | 说明 |
|---|------|------|------|
| 1 | `generation_presenter_plan` 无显式 `require_commercial_access()` | **非 bug** | `_make_client()`（director.py:54-55）内部调用它，命令必经 `_make_client` → 门禁生效，无 fail-open 绕过 |
| 2 | capability digest 一致性（测试 monkeypatch `_stored_capabilities` 可能掩盖真实路径） | **非 bug** | 真实流程 v1.1 caps 在 generate 时保存、digest 写入 state；M2 `_stored_capabilities`（director.py:832）强制 `canonical_digest==state.capability_digest`；server 按发送内容签名 → save 绑定检查成立。digest-bind 拒绝测试已存在（test_presenter_plan_client.py:401,417）。测试 monkeypatch 属测试真实度，不阻塞 |
| 3 | `_project_in_m2_context` fail-closed=True 可能误伤 M1 | **可接受 trade-off** | 异常分支近死代码（ProjectStore 构造只做 path join）；抑制 M1 话术 < 把 M1 话术误给 M2 |
| 4 | idempotent short-circuit 在 manifest_ready gate 前 | **正确** | presenter_plan_ready ≠ manifest_ready，后置会误拒重跑；已注释 |
| 5 | 跨仓 decorator regex 假阳性 | **无** | 80 字符窗口够；真实文件 `@router.post(` 紧邻 path |

**Blocker 检查：无**——approval 凭证不泄漏、无二次扣费、M1 话术不误入 M2、门禁无 fail-open 绕过。

**LOCK-review PASS**。commit 见下。
