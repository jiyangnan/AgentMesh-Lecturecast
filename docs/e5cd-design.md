# §5.5e5c/d 设计稿 — capability wiring + doctor/canary

> 技术规格 v1.4 §3.7 / §2.6 / §5 / §8（`lecturecast-server/docs/DIGITAL-HUMAN-TECH-SPEC.md`）
> 前置：§5.5e5b0c3c-c3 已锁定（989 测试全绿，commits `8980403`→`67da214`）
> 本稿覆盖 e5c（capability 上报真相）+ e5d（doctor + canary + maintenance 接线）
> 调研：5-agent workflow（4 并行 extract：capabilities/primitives/doctor-canary/scope + 1 completeness critic，375k tokens）

---

## 0. 范围与拆分决策

`DIGITAL-HUMAN-PROGRESS.md:72,96-99` 把 e5c/d 写成**一个合并步**：「capability wiring + doctor/canary：把已锁的删除子系统（coordinator + resolver + processors）+ HeyGen adapters 接入宿主 workflow，加 doctor 健康检查 + canary」。全仓 grep 无任何先例区分 e5c 与 e5d（scope extract 确认）。

**本稿裁定拆分为两个独立可锁定的子步**（理由：影响面不同，可独立 Codex 审、独立锁、独立回滚）：

| 子步 | 主题 | 影响面 | 判锁依据 |
|------|------|--------|----------|
| **e5c** | capability 上报真相（client 向 server **报告**什么） | M2 gate 计费契约（server `validate_presenter_capabilities` 按 `configured=true` + operations 列表真扣 PresenterPlan credits） | 上报的 operations/features **每一项都有已锁原语背书**；probe 深度 = 实际就绪而非仅可导入；M1 独立性不破 |
| **e5d** | 本地健康 + 验证 + 恢复 + 交互降级（client **本地**做什么） | 纯本地（doctor 只读、canary 烟测、maintenance 恢复驱动、director preflight 交互卡片）—— **不影响上报的 capability payload** | doctor fresh re-capture + 只读不写；canary 覆盖 §5 line 489 全体不变量；maintenance 透传 `type(force) is bool` 且不引入新 truthy force 来源；**用户要数字人但未配置时懒触发交互卡片（配 key / 降级 M1）** |

**拆分依据**：e5c 改的是发往 server 的 payload（动 M2 计费门禁），e5d 改的是本地行为（不动 payload）。两者耦合点仅一个——doctor/canary 必须用 e5c 落地的「真实 probe」**fresh** 调 `capture_capabilities_v1_1`，不能读 cache。故 **e5c 必须先锁，e5d 依赖 e5c 的真实 probe**。

**盲预测四约束**（PROGRESS.md:99 verbatim，e5c/d 全程不得违反）：
- (a) wiring 不能引入新的 truthy `force` 来源或绕过已锁的 claim↔apply 镜像
- (b) doctor/canary 只读不写（绝不触发真实删除/上传）
- (c) 宿主 workflow 必须把 `force` 当 bool 透传（`type(force) is bool`）
- (d) 不能放松 c1/c2/c3 任一已锁不变量

---

## 1. 调研结论（workflow 综合 + 第一手核实）

### 1.1 生产接线 gap（e5c 核心）

`capture_capabilities_v1_1(env=None, adapter_probe=_not_available, journal_probe=_not_available, ...)`（capabilities.py:229-271）接受 probe 参数，但**两个生产 call site 都不传**：

| call site | 位置 | 问题 | 第一手核实 |
|-----------|------|------|-----------|
| Director 生成路径 | `commands/director.py:772`（条件选 v1.1 over v1.0）→ `:775-780` 调 `capture_capabilities_v1_1(adapter_kind=, adapter_version=, project_root=, repo_root=)` | 不传 `env/adapter_probe/journal_probe` → 默认 `_not_available`（:31-35 永返 False）→ `heygen_processor()` 永远 None → `third_party_processors` 永远不上报 | 4 extract 一致 |
| `project capabilities` 命令 | `commands/project.py:180-185` 调 `capture_capabilities(adapter_kind=, adapter_version=, project_root=, repo_root=)` | **v1.0-only**（无 protocol_version 分支）+ 不传 probe → 即便切 v1.1 也永远 None | **critic 补漏**；本稿已第一手读 project.py:180 确认 |

后果：即便 §5.5e5b0c3c 的真实 adapter（`HeyGenAssetAdapter` heygen_asset_adapter.py、`HeyGenVideosAdapter` heygen_videos_adapter.py、`heygen_journal.init_database`、`DeletionCoordinator` operation_repository.py:3775）已 ship，生产中 HeyGen capability **永不报告**。`capture_capabilities_v1_1` 全仓仅 director.py:772 一处引用。

> **e5c 必须同时改两个 call site**（director.py + project.py），否则 `configured` 信号取决于用户跑了哪个命令——`project capabilities` 持久化的 `client-capabilities.json` 会缺 processor，director 在 adapter/schema mismatch 时重采（director.py:688-722 `_stored_capabilities`）会掩盖这个 bug，但显式刷新路径仍坏。

### 1.2 操作/特性 under-reporting

**spec §3.7（TECH-SPEC.md:441-443）** 枚举 6 operations + 3 features。当前 `heygen_processor()`（capabilities.py:89-97，本稿第一手核实）硬编码 3 ops / 1 feature，注释「Only operations the shipped adapter actually implements」——**该注释相对已锁代码已 stale**。

| operation | spec §3.7 | 已锁原语 | 上报? |
|-----------|-----------|----------|-------|
| `direct_asset_upload` | ✓ | `AssetUploadProcessor`（op_repo:3363）+ `HeyGenAssetAdapter.upload_asset`（e5b0c2） | ✓ |
| `photo_avatar` | ✓ | `HeyGenVideosAdapter.submit_video`（descriptor type='image'，e5b1）—— **与下面 lipsync 共享同一 symbol** | ✓ |
| `prerecorded_audio_lipsync` | ✓ | 同 `submit_video`（image_asset_id+audio_asset_id 同一 /v3/videos 调用，e5b1）—— 无独立 code path | ✓ |
| `asset_delete` | ✓ | `AssetDeletionProcessor.delete_once`（op_repo:3737）+ `HeyGenAssetAdapter.delete_asset`（asset_adapter.py:468，e5b0c3a）—— **e5c 必须加** | ✗ |
| `video_delete` | ✓ | `DeleteProcessor.delete_once`（op_repo:3710）+ `HeyGenVideosAdapter.delete_video`（videos_adapter.py:273，e5b0c3b）—— **e5c 必须加** | ✗ |
| `avatar_delete` | ✓ | **无独立原语**（见 §3.2） | ✗ |

| feature | spec §3.7 | 已锁原语 | 上报? |
|---------|-----------|----------|-------|
| `idempotency_24h` | ✓ | `RECONCILE_SEARCH_WINDOW_SECONDS=24*3600`（op_repo:70）+ `_ASSET_IDEMPOTENCY_WINDOW_SECONDS=24*60*60`（:3120）+ transport Idempotency-Key regex（heygen_http.py:31） | ✓ |
| `title_query` | ✓ | `HeyGenVideosAdapter.query_videos_by_title`（videos_adapter.py:204）+ `ReconcileProcessor`（op_repo:3468）—— **e5c 必须加** | ✗ |
| `read_only_auth_check` | ✓ | **无独立 transport 方法**，但 `get_asset` docstring 明确背书（见下）—— **e5c 决策** | ✗ |

**read_only_auth_check 的 get_asset 背书**（本稿第一手读 heygen_asset_adapter.py:440-510 核实）：
- docstring（:441-443）原文："GET /v3/assets/{id}. … **Used for doctor / manual reconciliation only** — never attest content digest from the GET response."
- `_map_read_error`（:492-510）：401/403 → `AssetReadError(code="auth_failed")`；429 → `rate_limited`；5xx → `provider_server_error`。
- GET 语义：404 → `AssetProbeResult(exists=False)`（**credential 被接受**，key 有效）；200 + 回显 id/type → exists=True（key 有效）；401/403 → auth_failed（key 无效）。
- **结论**：对合成/已知 asset_id 发 GET，404/200 = key 有效，401/403 = key 无效。read_only_auth_check **可由 get_asset 背书，无需新增 transport 方法**。

### 1.3 configured 语义 + fail-closed omit 模式

`heygen_processor(env, adapter_probe, journal_probe)`（capabilities.py:70-97）要求**三者全 AND**：
1. `HEYGEN_API_KEY` env 非空（:85）
2. `adapter_probe()` truthy（:87）
3. `journal_probe()` truthy（:87）

任一失败 → 返回 `None`（processor **整个 omit**，不上报）。**无 `configured=False` 路径**。模块注释（:22-26）：「presence-only … configured is a capability gate (M2 compatibility), not a preflight-passed claim.」

`capture_capabilities_v1_1` 仅在 processor 非 None 时设 `payload['third_party_processors']=[processor]`（:269-270），否则 key 缺席（schema 可选）。test `test_capture_v1_1_fail_closed_by_default`（test_capabilities_v1_1.py:94）断言 `third_party_processors in (None, [])`。spec §3.7 line 448：旧 client 缺字段 → 默认 `[]`，原五阶段路径不受影响——**omit 是 spec-compliant**。

> **决策 §7.1（用户裁定 2026-08-02）**：**上报层面保持 omit**（server 只接受 configured=true 真相，不能报假；匹配 §5.5b 测试契约 + spec §3.7 line 448 允许缺字段）。**用户体验层面不静默**：当 director preflight 探测到「用户要数字人（photo_avatar/digital_human）但 third_party_processors 缺失/未配置」时，**懒触发交互式卡片**——选项 A「重新配置 HEYGEN_API_KEY 并反馈（重新 capture）」/ 选项 B「跳过 HeyGen 降级 M1 基础视频」。capture 时不问（中性快照），用时才问（用户意图明确）。M1 路径（不要数字人）不受影响、不弹。落 **e5d**（契约 D13）。

### 1.4 doctor v1.0-only

`doctor_report(capabilities: ClientCapabilities)`（capabilities.py:274-310）**只查 v1.0 runtime**（node/remotion/ffmpeg/libass），返回 `{ready, missing, next_actions, capabilities}`。`ready = can_render_locally AND has_libass`。**零 awareness** of `third_party_processors`/HeyGen/journal。

consumers：
- `commands/doctor.py:22` —— `doctor` Typer 命令调 `doctor_report(capture_capabilities(project_root, repo_root))`（**v1.0 base**，非 v1.1），`--project-root`/`--json`，emit 中文消息。
- `commands/onboard.py:24` —— onboard 调 `doctor_report(capture_capabilities(...))`，把 `lecturecast doctor --json` 作为 next_suggested。

CLI 注册（cli.py:22-35）：`app.add_typer(sub_app, name=...)` 用于子命令组；`app.command()(func)` 用于 leaf 命令（doctor/onboard 是 leaf）。测试入口 `typer.testing.CliRunner.invoke(app, [...])`（test_cli.py:7-14）。

### 1.5 F5 doctor 模板（f5-capability-check.md，spec §8 line 610 指名）

F5 自检是 **Director 消费的决策级 JSON**，非纯本地报告：
- 6 项 check 各标 PASS/WARN/BLOCKER（CPU arm64、Python 3.10+ arm64、disk≥10GB、RAM≥8GB、HuggingFace reachable、ffmpeg installed + MPS post-install WARN）
- 决策逻辑：任一 BLOCKER → `can_run_f5=false`；无 BLOCKER 有 WARN → `can_run_f5=true`
- 自包含 Python 自检脚本 emit JSON `{can_run_f5, blockers[], warnings[], details{}}`，client 跑、output 回传 Director
- Director 决策树（fallback 到 edge-tts）

> **e5d 必须做 HeyGen 等价**：`{configured, operations, features, blockers[key_missing/adapter_unimportable/journal_ahead/...], warnings}` —— 让宿主 workflow 做 BLOCKER/WARN-style gate 决策（要不要给用户 offer photo-avatar 路径 vs 降级 edge-tts + 无 avatar）。**这比「只是 extend doctor_report」更强**（critic §3）。当前无 HeyGen doctor 文档（f5-capability-check.md 是 F5-only）。

### 1.6 journal migration head 机制

SQLite `PRAGMA user_version`（**非** schema_version 表）。`_SCHEMA_VERSION = 6`（heygen_journal.py:28，当前 head）。`init_database(project_dir)`（:391-454）：
- 开 `.lecturecast/heygen-runtime/heygen.db`，读 `PRAGMA user_version`（:441-448）
- `> 6` → `RuntimeError('refusing to downgrade')`
- `< 6` → `_migrate(conn, current_version)`（:350-376，BEGIN IMMEDIATE 内跑 v1→v2…v5→v6 全部 step，再 `PRAGMA user_version = 6`，all-or-nothing rollback）

> **critic §8 关键张力**：`init_database` **副作用 auto-migrate**（写 DDL + bump user_version）—— 与约束 (b)「doctor/canary 只读不写」冲突。journal_probe 若调 init_database，会在陈旧 DB 上**触发一次迁移写**。e5d 必须：(a) 接受「head-current 蕴含一次性幂等迁移写」并文档化；或 (b) 开只读 sqlite 连接（`file:...?mode=ro` URI）比 `PRAGMA user_version` vs 6 **不迁移**。doctor 还须把 refuse-downgrade `RuntimeError` 当独立失败模式（journal head ahead of client），不能误报「needs migration」。

### 1.7 capability caching（critic §4）

capability **持久化到盘**（`client-capabilities.json` + `capability_digest`），仅在 adapter kind/version mismatch（director.py:717-719）或 schema_version mismatch（:722）时重采。`_stored_capabilities`（director.py:688-722）；`save_capabilities`（project.py:278-293）写 capabilities_path + capability_digest。policy `refresh_before_generate_on_adapter_mismatch`（director.py:527）。

> **后果**：heygen_processor probe 跑一次，结果**冻结**。若 HEYGEN_API_KEY 在 capture 后加/删、或 adapter 后装，**cache 报 stale HeyGen 状态**。spec §2.6 line 368 要求「每次 M2/M3 请求携带/刷新当前 capabilities（不复用 M1 时快照）」——adapter-mismatch-only refresh 是潜在 M2 契约 gap。**直接对 e5d**：读 cache（或调 `_stored_capabilities`）的 doctor 会报 stale configured=true/false → **doctor 必须 fresh 调 `capture_capabilities_v1_1` + 活 probe**。

### 1.8 recover_deletions 需双 adapter（critic §5）

`DeletionCoordinator.recover_deletions`（op_repo:3928）+ `delete_pass_for_operation`（:3799）签名**同时要求** `deleter`（`DeleteVideoAdapter` = `HeyGenVideosAdapter.delete_video`）**和** `adapter`（`HeyGenAssetAdapter.delete_asset`）。maintenance/host 接线必须构造**一个** `HeyGenHttpTransport`，实例化**两个** adapter（videos=deleter, asset=adapter），**同时**转发。单 adapter 接线会让 §3.5 sweep 的 video-delete 或 asset-delete 半边失效。

### 1.9 recover_asset_deletions 过期名（critic §6）

`e5b0c3c-c3-design.md:111` 点名 maintenance primitive `recover_asset_deletions`（**不存在**）。真实符号在**两个不同类**：
- `DeletionCoordinator.recover_deletions`（op_repo:3928）—— network deletion sweep（需 deleter+adapter）
- `OperationRepository.recover_withdrawn_asset_cleanups`（op_repo:3057）—— DB-only consent-withdrawal replay（无 network）

> **e5d 接线裁定**：接**两个方法**、各自类、**正确顺序**——`recover_withdrawn_asset_cleanups` **先**（DB-only、无 network、安全），`DeletionCoordinator.recover_deletions` **后**（network-bound、需 deleter+adapter）。设计稿用真实符号，不用 stale 设计稿名。

### 1.10 §2.6 M2/M3 gate 契约

spec §2.6（TECH-SPEC.md:330-373）capability-gate 表：
- **M1**（`validate_generation_capabilities`，server generations.py:227，唯一 M1 调用点）：仅查 M1 protocol + base renderer + `edge in tts_engines`。**显式「不查 HeyGen、不查 F5、不查 configured」**（line 359）。spec line 75/519/489 反复：「M1 ProductionManifest = 独立可执行的基础视频计划… M1 不依赖任何旁支能力」。
- **M2**（`validate_presenter_capabilities`，NEW 独立函数，late，preflight 后跑）：查「HeyGen capability 存在 + `configured=true` + 用户 M2 approval + `consent_status=granted` + §1.5 digest 链。**不查 verified**」。
- **M3**（`validate_orchestration_capabilities`）：查 orchestration_plan schema +（`voice_mode=own_voice` → f5 in tts_engines）。

> **e5c 的 configured 是 server-side M2 gate 消费的**——client 只报告。**e5c wiring 绝不能让 HeyGen 进 M1 path**（M1-independence，spec line 489 canary 显式 assert：「M1 不依赖 HeyGen 配置（photo 用户无 HeyGen key 仍交付基础视频）」）。

### 1.11 §5 canary 契约（TECH-SPEC.md:487-489，30-credit photo+stock）

line 487：「closed → internal allowlist → 30-credit canary（photo+stock）→ staged rollout」。
line 489 pass criteria：
1. migration head 一致
2. Core 3 action 成本逐项一致
3. digest 链四项 + 补跑案例
4. **一次最多 30 credits**
5. 三笔 ledger + awaiting_credits + **删除恢复（download_status=verified→逐资源 deleted）**
6. client 展示 estimate == server pricing_estimate
7. **M1 不依赖 HeyGen 配置**
8. rollback 已 charged 处理方案

> 现状：client 无数字人 canary harness（grep 'canary' 命中全是无关命名巧合：test_timing.py:41/test_commercial.py:203/test_platform_contract.py:128/test_site_contract.py:205/config.py:21/SIGNING-KEYRING.md:46）。server 侧有 10-credit 非数字人 canary（`deploy/canary.md`、`deploy/rollout.md:29-37`）—— **30-credit 数字人 canary 是全新**，无 harness。删除恢复原语（DeletionCoordinator.recover_deletions + recover_withdrawn_asset_cleanups）**已锁**，canary harness 本身未写。

#### 锁定记录（2026-08-02，3 轮 Codex，LOCKED）

`src/lecturecast/canary.py` + `commands/canary.py`（CLI 叶）+ `tests/test_canary.py`（26 测）。commits 545f387→5aae32e→c417cc5。3 轮 Codex（effort=low，invariant-completeness framing）：

- **round-1 (545f387)**: NOT LOCKABLE，4 blockers。
- **round-2 (5aae32e)**: 关 3/4 ——
  1. **fail-closed cap**（#4）：原 round-1 fail-open = 验签失败时 fallback 读未授信 `minimum_total`（3×100 但 minimum_total=0 漏过 cap 还驱动了 3 删除）。修：`cap_held = estimate_ok and (sum(validated per_milestone) ≤ cap)`，验签失败即 cap FAILS CLOSED + 删除 drive 在 gate 处拒绝（区分「estimate invalid」vs「cap exceeded」两种 refusal）。
  2. **zero-network by construction**（constraint b）：删掉 `deleter`/`adapter` 注入参数 —— canary 始终用模块内 `_StubDeleter`/`_StubAdapter`，无注入 seam 即无真 transport 路径。§3.5 routing 改由 `report.deletion_calls {video:(...), asset:(...)}` 暴露（不再靠注入 spy）。
  3. **#8 rollback routing**：不再只查 `charge_model` 字段（validate 已查），改为真驱动 `director._status_workflow` 断言 `credit_returned→estimate_refresh_required` + `awaiting_credits→credit_resume_required`（client-depth 处理方案）。
- **round-3 (c417cc5)**: 关最后 1/4 ——
  4. **#6 display projection**：round-2 的 `next_milestone_cost_or_fail` 是 credit-cost reader 非显示路径。真显示投影是 `director._session_workflow`（director.py:244-268）：经 `_validated_estimate`（含 card↔session estimate 相等校验）+ 8 字段子集投影到 `workflow["pricing_estimate"]`。canary 驱动 v1.1 confirmed session（card 带 same estimate）穿 `_session_workflow`，断言投影 == 校验 estimate 的 8 字段逐项相等；`validated == estimate` 收紧为 `validated is estimate`（identity，锁 validator 不 mutate/copy，pricing.py:121）。teeth test 证伪篡改投影（minimum_total 翻倍 / 漏字段）必败。

**约束遵守**：(b) 只读沙箱（init_database 仅写 canary 自己的隔离 DB；删除 drive 全 stub）✓；(d) 不放松已锁不变量（gate probe / journal classification / coordinator force=False literal / claim↔apply 拓扑全未触）✓。server 侧 #2 ledger / #5 awaiting / #8 refund execution = SERVER canary + §6 跨仓契约范围，client canary 各 detail 诚实标注边界（lesson #13）。canary 对 `_session_workflow`/`_status_workflow`/`DirectorState` 的依赖是刻意耦合 —— director 回滚/显示路由回归时 canary 应红（回归检测）。

### 1.12 宿主 workflow 接线现状（critic §11）

已锁原语（DeletionCoordinator / recover_deletions / recover_withdrawn_asset_cleanups / SubmitCoordinator / capture_capabilities_v1_1 / heygen_processor）**全部存在于 src/**，但**无一**接入任何 CLI 命令或 maintenance scheduler。`commands/`（agent.py/manifest.py）不 import OperationRepository 或任何 HeyGen processor。唯一 OperationRepository 的 repo 外使用是 consent.py:602（`enqueue_consent_withdrawal_cleanup_in_tx`）。cli.py 注册 auth/agent/director/project/manifest/outcome + doctor/onboard/version/workflow——**无 heygen 子命令**。

> **`agent` 命令**（commands/agent.py:24 `_command`、:35 `_project_action`、:167-181 `adapters`/`status` 子命令，「drive the current native host through one safe next action」模式）是现有宿主驱动入口——e5d 端到端 HeyGen driver（submit→poll→download→delete 序列）最自然的扩展目标。**但「接入宿主 workflow」全对接（submit/poll/download/delete driver）远超 doctor/canary 范围**——本稿裁定 e5d 只接 **maintenance 恢复 pass**（recover_withdrawn_asset_cleanups + recover_deletions）+ doctor + canary；完整生成 driver 留 §5.5e6（RecoveryDirectiveCatalog 宿主 workflow）。

---

## 2. 盲预测（实现前先写死的契约）

### 2.1 e5c 契约（capability 上报真相）

| # | 契约 | 验证 |
|---|------|------|
| C1 | `heygen_processor()` 上报的 operations **每一项**都有已锁原语背书（见 §1.2 表，asset_delete/video_delete 必须加；avatar_delete 见 §3.2 决策） | 对每个 op grep 到原语 + probe 深度验证 |
| C2 | `heygen_processor()` 上报的 features **每一项**都有已锁原语背书（title_query 必须加；read_only_auth_check 见 §3.3 决策） | 同上 |
| C3 | 真实 `adapter_probe`：importlib 导入 heygen_http/heygen_videos_adapter/heygen_asset_adapter **且** 证实关键类可解析（非仅顶层模块 import） | 注入 fake 不可导入模块 → probe 返回 False |
| C4 | 真实 `journal_probe`：证 journal head==6（`_SCHEMA_VERSION`），**只读**（mode=ro URI 或 init_database 的幂等性文档化，见 §3.4 决策） | head=5 → False；head=6 → True；head=7 → False（refuse-downgrade 区分） |
| C5 | **probe 深度 = 实际就绪**（非仅 importability）：configured=true 当且仅当 key+adapter+journal-head 三者就绪 | importable 但 journal v5 → None（omit） |
| C6 | director.py:772 **和** project.py:180 **两个** call site 都传真实 probe + 走 v1.1（project.py 须加 protocol_version 分支或默认 v1.1） | 两路径都报 third_party_processors |
| C7 | M1 独立性：无 HEYGEN_API_KEY 时 third_party_processors=[] 且 M1 仍可交付基础视频（edge-tts + 无 avatar） | 显式 test |
| C8 | 无 secret 泄漏：`repr(processor)` 不含 key 值（'sk_secret_value' not in repr） | 复用 test_capabilities_v1_1.py:82 不变量 |
| C9 | 无 `verified` 字段（spec §3.7 line 445 v1.3 移除） | 复用 test_capabilities_v1_1.py:83 不变量 |
| C10 | test_capabilities_v1_1.py:74-81 精确 dict 断言**随 operations/features 扩展同步重写**，保留 C8/C9 伴生不变量 | 新断言匹配新 op/feature 集 |

### 2.2 e5d 契约（doctor + canary + maintenance）

| # | 契约 | 验证 |
|---|------|------|
| D1 | doctor **fresh** 调 `capture_capabilities_v1_1` + 活 probe（**不读 cache**、不调 `_stored_capabilities`） | monkeypatch cache 文件 → doctor 仍报活态 |
| D2 | doctor 只读不写：journal_probe 不触发迁移写（mode=ro URI），或文档化 init_database 的幂等一次性写 | 只读连接读 user_version 不改盘 |
| D3 | doctor 区分三态 journal head：`<6` needs-migration / `==6` current / `>6` ahead-of-client（refuse-downgrade RuntimeError 独立分类） | 三态各一测 |
| D4 | doctor HeyGen section emit BLOCKER/WARN 决策 JSON（镜像 f5-capability-check.md）：blockers=[key_missing/adapter_unimportable/journal_ahead]、warnings=[journal_behind_head]、`{configured, operations, features, blockers, warnings}` | 结构 + 决策逻辑测 |
| D5 | doctor 不泄漏 key（只报存在性 non-empty，绝不报值） | repr/assert 无 key 值 |
| D6 | canary 覆盖 §5 line 489 全体 8 项（migration head / Core 3 成本 / digest 链 / 30-credit cap / 三笔 ledger+awaiting+删除恢复 / estimate==pricing / M1 独立 / rollback） | canary harness 逐项 assert |
| D7 | canary 30-credit cap 硬门禁（单次 run 累计 cost ≤ 30） | 注入超 30 → 拒绝 |
| D8 | canary 删除恢复：download_status=verified 的每资源最终达 deleted 终态（逐资源 assert） | 驱动 DeletionCoordinator 后查每行 |
| D9 | canary M1-independence：无 HEYGEN_API_KEY 时 third_party_processors=[] 且 M1 路径仍交付基础视频 | 显式 assert |
| D10 | maintenance 接线：`recover_withdrawn_asset_cleanups` 先（DB-only）→ `DeletionCoordinator.recover_deletions` 后（network），**双 adapter**（deleter+adapter）同时传入 | 调用顺序 + 双 adapter 测 |
| D11 | maintenance `force` 透传：`type(force) is bool`（复用 c3 铁律），不引入新 truthy force 来源 | type-mismatch → ValueError |
| D12 | doctor/canary/maintenance **绝不触发真实删除/上传**（canary 的删除恢复是清理 canary 自己刚创建的 photo+stock 资源，非用户数据；maintenance 只跑已锁 coordinator 的既有幂等逻辑） | canary 用隔离 project dir；maintenance 不动 c3 已锁不变量 |
| D13 | **交互式降级卡片**（用户裁定 §7.1）：director preflight 探测「presenter_plan 要数字人但 configured 缺失/失败」时，返回交互式 next_action（选项 A 配 HEYGEN_API_KEY 并反馈 / 选项 B 降级 M1 基础视频），**懒触发**（capture 时不问、用时绝不静默放弃）；M1 路径（不要数字人）**不触发**；上报层面仍 omit（卡片是 client 本地 UX，不改 payload） | 探测时机 + next_action 两选项结构 + M1 不触发测 |

---

## 3. bypass risks（fail-closed 威胁模型）

> 原则（继承 c3）：**「producer never generates X」不是边界**——所有 schema-legal 异常态必须 fail-closed。e5c/d 的威胁模型核心是 **capability 过报（over-report）→ M2 gate 真扣费给不可执行的能力**。

### 3.1 ⚠️ over-reporting（critic 判定最大风险）

**场景**：e5c (a) 扩 operations 到 6 + features 到 3，**且** (b) 用 importability-only 的 importlib probe 背书 → client 报 configured=true + delete ops，**但** journal 可能还在 v5（pre-DeletionCoordinator 表）→ server M2 gate 按报文真扣 PresenterPlan credits → 实际 delete op 执行失败。

**根因**：importability probe 证明模块可导入，**不证明** journal 在 head v6、**不证明** transport 能认证、**不证明** 删除 schema 存在。

**缓解（写入 C3/C4/C5）**：
- `adapter_probe` = importlib 导入 3 个 heygen 模块 **且** 证实 `HeyGenHttpTransport`/`HeyGenVideosAdapter`/`HeyGenAssetAdapter` 类可解析
- `journal_probe` = head==6（只读）
- `configured=true` **当且仅当** key + adapter_probe + journal_probe 三者全过
- 上报的 operations 是「已验证栈实际服务的子集」——avatar_delete 因无独立原语**不上报**（§3.2）

**判据**：probe 证实的就绪栈能服务多少 op，就报多少。不能 importable-but-not-ready。

### 3.2 avatar_delete over-report（无独立原语）

**事实**：avatar 生命周期今天**结构性惰性**：
- `_asset_retention_mode`（op_repo:3140-3143）**永远返 'ephemeral'**（reusable_avatar 是 future lifecycle，从不上产）
- `avatar_look`/`avatar_group` resource_kind **无 processor**，路由到 `skipped_unknown_kind`（op_repo:2434-2436 + 3865-3869 + 4060 注释）
- 唯一的「avatar」删除是删 ephemeral portrait_photo，走 **asset_delete** 路径（AssetDeletionProcessor on resource_kind='portrait_asset'）
- reusable_avatar 行**显式被删除规划过滤**（op_repo:2415-2416，注释「revocation via HeyGen dashboard」）

**决策（写入 §7.2）**：**omit avatar_delete**。spec §3.7 min_length=1，不要求全 6 个。把 photo_avatar（生成 ephemeral portrait）的删除归到 asset_delete 上报。**若硬要报**，必须带显式 caveat「co-served by asset_delete on ephemeral portrait_asset; reusable_avatar not client-deletable」——但带 caveat 的 capability 上报在 M2 gate 语义上无意义（gate 只看 op 在不在列表），故选 omit。

### 3.3 read_only_auth_check 决策（get_asset 背书）

**事实**：无独立 transport auth-probe 方法，但 `get_asset` docstring 明确「Used for doctor / manual reconciliation only」（§1.2 第一手核实）。对合成 asset_id GET：404/200 = key 有效，401/403 = auth_failed。

**决策（写入 §7.3）**：**e5c 上报 read_only_auth_check**（get_asset 背书，docstring 是权威意图）。**但**——get_asset 的 auth 语义是 **doctor/canary 实际调用时的运行时验证**，不是 capability probe 的静态属性。故 read_only_auth_check 上报的条件与 idempotency_24h/title_query 不同：它依赖 transport 可构造（adapter_probe 已证）。**风险**：上报 read_only_auth_check 但 key 实际无效 → M2 gate 仍放行（gate 不查 verified）→ 运行时 401。这符合 spec 设计（configured 是 presence-gate，非 preflight-passed-claim，模块注释 :22-26 明示），故可接受。

> **替代**：若保守起见 e5c 不报 read_only_auth_check（等真正加 /v3/user 端点），也合规——features max_length=8 非强制 3。本稿推荐**报**（get_asset 已背书 + docstring 权威）。

### 3.4 journal_probe 只读 vs init_database 幂等写（约束 b 冲突）

**场景**：journal_probe 调 init_database → 陈旧 DB（head<6）触发迁移写 → **违约束 (b)「doctor/canary 只读不写」**。

**决策（写入 §7.4）**：**journal_probe 用只读连接**（`sqlite3.connect("file:<path>?mode=ro", uri=True)`）读 `PRAGMA user_version`，比 `_SCHEMA_VERSION`（=6），**不调 init_database**：
- head==6 → True
- head<6 → False（needs-migration，**doctor 报告告诉用户跑一次正常路径触发迁移**，不在 doctor 里自动迁）
- head>6 → False（ahead-of-client，refuse-downgrade 独立分类）
- DB 不存在 → False（未初始化）

**理由**：probe 应**反映当前真实就绪**，不**改变**状态。head<6 是合法的「未就绪」态，doctor 报告它、由用户/正常生成路径触发迁移。这同时满足约束 (b) 和「probe 深度 = 实际就绪」。

> **e5d maintenance pass**（recover_deletions）则**可以**调 init_database（maintenance 不是 doctor/canary，约束 (b) 不适用 maintenance；且 recover 前需确保 head 当前——init_database 幂等，head==6 时是 no-op）。

### 3.5 doctor false-ready / false-negative

- **false-ready（读 cache）**：HEYGEN_API_KEY 删后 cache 仍报 configured=true → doctor 误报就绪。**缓解 D1**：doctor fresh re-capture。
- **false-negative（init_database 写触发）**：见 §3.4，用只读 probe。
- **refuse-downgrade 误报**：head>6 的 RuntimeError 若被当「needs migration」会建议用户降级。**缓解 D3**：独立分类「journal head ahead of client」。

### 3.6 configured key-leak

`repr(processor)` / doctor JSON 绝不含 key 值。heygen_processor 只读 `(sources.get(HEYGEN_API_KEY_ENV) or '').strip()` 的 bool（:85），不存值。**缓解 C8/D5**：复用 test_capabilities_v1_1.py:82 模式，doctor JSON 只报 `key_present: bool`。

### 3.7 maintenance wiring 新 truthy force 来源（约束 a/c）

**场景**：maintenance CLI 接 `--force` 透传给 DeletionCoordinator，若用 `argparse`/typer 默认 bool 解析（`store_true`），类型是 bool ✅；但若用 truthy 字符串/整数默认，违反 c3 铁律 `type(force) is bool`。

**缓解 D11**：maintenance 命令的 force 参数 typer 用 `bool` 类型，透传前 `type(force) is bool` 守卫（复用 c3 e5b0c3c-c3 已锁模式）。**不引入**新 force 来源（如 `--aggressive`/`--unsafe`）——复用 c3 已锁 force 语义。

### 3.8 recover_deletions 单 adapter（critic §5）

**场景**：maintenance 只传 deleter（videos）忘传 adapter（asset）→ video-delete 半 sweep 跑、asset-delete 半 sweep 静默跳过 → 部分资源残留。

**缓解 D10**：接线构造一个 transport + 双 adapter，**同时**传 recover_deletions(deleter=videos, adapter=asset)。test 断言两者都被调。

### 3.9 M1 依赖泄漏（约束 d + spec line 489）

**场景**：e5c wiring 误把 heygen probe 调用塞进 M1 生成路径（如 `validate_generation_capabilities` 误调 capture_capabilities_v1_1）→ M1 路径依赖 HeyGen 配置 → 违 spec line 359/489。

**缓解 C7/D9**：e5c 只改 director.py:772（M2/M3 路径）+ project.py:180（capability 持久化命令），**不动** M1 生成路径。显式 test：无 HEYGEN_API_KEY 时 M1 仍交付基础视频。

### 3.10 capability cache stale configured（critic §4）

**场景**：HEYGEN_API_KEY 在 capture 后加/删 → cache 不刷 → director.py 用 stale payload 发 server → M2 gate 用 stale configured。

**e5c 范围裁定**：cache 刷新策略（adapter-mismatch-only）是**既有设计**，改它属 §2.6 line 368 「每次 M2/M3 刷新」契约，**超出 e5c 范围**（e5c 只补真实 probe + op/feature 真相）。**但** e5d doctor 必须 fresh re-capture（D1）绕开 cache。cache 刷新策略改进留 §6 #10（M1 门禁跨仓契约）。

### 3.11 maintenance 接线绕过 claim↔apply 镜像（约束 a）

**场景**：maintenance recover_deletions 驱动 coordinator，若 wiring 在 coordinator 外另起「直接删」旁路 → 绕过 c3 已锁的 claim↔apply 跨 tx 授权镜像。

**缓解**：maintenance **只调** `DeletionCoordinator.recover_deletions` / `delete_pass_for_operation` / `OperationRepository.recover_withdrawn_asset_cleanups` 这些**已锁**入口，**绝不**直接调 adapter.delete_video/delete_asset。coordinator 内部已含完整 claim↔apply 镜像。

### 3.12 test lockstep 冲突（critic §2）

**场景**：test_capabilities_v1_1.py:74-81 是**精确 dict 全等**断言（非 subset）。e5c 扩 op/feature → 测试红。

**缓解 C10**：同步重写断言为新 op/feature 集，**保留** line 82（`'sk_secret_value' not in repr(proc)`）+ line 83（`'verified' not in proc`）伴生不变量。不能只改被点名的字段。

### 3.13 静默 omit 用户体验风险（用户裁定 §7.1）

**场景**：用户选了数字人路径但没配 key，client 静默 omit third_party_processors → director preflight 探测到要数字人但配置缺失 → 若不交互，用户不知道为啥没数字人、可能反复重试；或 M2 gate 收到无 processor 的 presenter_plan 直接拒，体验差。

**缓解 D13**：director preflight 探测到「要数字人但未配置」时**弹交互卡片**（选项 A 配 key 反馈 / 选项 B 降级 M1）。**懒触发**：capture 是中性能力快照（不问），用时（用户意图明确要数字人）才问。M1 路径完全不受影响（不要数字人不弹）。**关键边界**：交互卡片是 **client 本地 UX**，**不改上报 payload**——上报层面仍 omit（不能给 server 报 configured=true 假话）。即「对 server 诚实 omit，对用户透明交互」。

---

## 4. 测试矩阵

### 4.1 e5c tests（test_capabilities_v1_1.py 扩展 + 新增）

| # | 测试 | 覆盖契约 |
|---|------|----------|
| T1 | `test_heygen_operations_match_locked_primitives` —— assert 上报的 operations ⊆ {direct_asset_upload, photo_avatar, prerecorded_audio_lipsync, asset_delete, video_delete}（**无 avatar_delete**），且每个 op grep 到原语 | C1 |
| T2 | `test_heygen_features_match_locked_primitives` —— assert features ⊆ {idempotency_24h, title_query, read_only_auth_check}，每个有原语 | C2 |
| T3 | `test_adapter_probe_real_imports` —— 注入不可导入 fake 模块 → adapter_probe False → processor None | C3 |
| T4 | `test_journal_probe_head_check` —— head=5 → False；head=6 → True；head=7 → False；DB 缺 → False | C4 |
| T5 | `test_configured_requires_all_three` —— key 单独 / key+adapter / key+journal / 三者全过 各态 | C5 |
| T6 | `test_director_passes_real_probes` —— monkeypatch director 生成路径，assert capture_capabilities_v1_1 收到非 _not_available probe | C6 |
| T7 | `test_project_capabilities_uses_v1_1_and_probes` —— CliRunner 跑 `project capabilities`，assert 持久化的 client-capabilities.json 含 third_party_processors（v1.1） | C6 |
| T8 | `test_m1_independent_of_heygen` —— 无 HEYGEN_API_KEY，M1 生成路径仍产基础视频（third_party_processors=[]） | C7 |
| T9 | `test_no_secret_in_processor_repr` —— 'sk_secret_value' not in repr（复用 :82） | C8 |
| T10 | `test_no_verified_field` —— 'verified' not in proc（复用 :83） | C9 |
| T11 | `test_heygen_declared_payload_exact_dict_lockstep` —— 重写 :74-81 断言为新 op/feature 集，保留 T9/T10 | C10 |
| T-ctrl | 现有 test_heygen_none_when_no_key / fail_closed_without_adapter_and_journal / capture_v1_1_fail_closed_by_default 全绿（不退化） | 回归 |

### 4.2 e5d tests（test_capabilities.py doctor 扩展 + 新 test_doctor_v1_1.py + 新 test_canary.py + maintenance）

| # | 测试 | 覆盖契约 |
|---|------|----------|
| D-T1 | `test_doctor_fresh_recaptures_not_cache` —— monkeypatch cache 报 configured=false、live key 在 → doctor 报 configured=true | D1 |
| D-T2 | `test_journal_probe_readonly_no_migration` —— head=5 的 DB，跑 journal_probe，assert 盘上 user_version 仍=5（未迁移） | D2 |
| D-T3a/b/c | `test_doctor_journal_head_below`/`_current`/`_ahead` —— 三态分类 | D3 |
| D-T4 | `test_doctor_heygen_blocker_warn_decision` —— key_missing→BLOCKER；adapter_unimportable→BLOCKER；journal_ahead→BLOCKER；journal_behind→WARN；全过→无 blocker。emit JSON 结构 | D4 |
| D-T5 | `test_doctor_no_key_value_leak` —— doctor JSON 无 key 值（只 key_present bool） | D5 |
| D-T6 | `test_canary_migration_head` / `_core3_cost` / `_digest_chain` / `_30_credit_cap` / `_ledger_awaiting_deletion_recovery` / `_estimate_equals_pricing` / `_m1_independence` / `_rollback` —— §5 line 489 逐项 | D6 |
| D-T7 | `test_canary_30_credit_cap_hard_gate` —— 注入累计 31 → 拒绝 | D7 |
| D-T8 | `test_canary_deletion_recovery_per_resource` —— 驱动 coordinator 后逐资源 assert deletion_status=deleted | D8 |
| D-T9 | `test_canary_m1_independence` —— 无 key 时 canary 仍验 M1 路径交付 | D9 |
| D-T10a/b | `test_maintenance_recovery_order` —— recover_withdrawn_asset_cleanups 先、recover_deletions 后；双 adapter 都传 | D10 |
| D-T11 | `test_maintenance_force_bool_passthrough` —— `type(force) is not bool` → ValueError | D11 |
| D-T12 | `test_canary_isolated_project_dir` / `test_maintenance_only_locked_entries` —— canary 用隔离 dir；maintenance 只调已锁入口 | D12 |

---

## 5. Codex 审阅问题（发审用，effort=low，rephrased 绕 cyber 过滤）

> 沿用 c3 round-13 框架：**invariant-completeness（不变量穷举）而非 security 措辞**。

1. **probe 深度契约**：adapter_probe（importlib 导入 3 模块 + 类可解析）+ journal_probe（只读 head==6）能否充分背书「configured=true 当且仅当栈实际可服务上报的 ops」？是否存在 importable-but-not-ready 的 schema-legal 态（如 adapter 可导入但 journal head=6 表结构因某个 migration step 未跑而缺列）使过报？逐项列。
2. **avatar_delete omit 合规性**：spec §3.7 operations min_length=1，omit avatar_delete 是否合规？M2 gate（server validate_presenter_capabilities）是否对「photo_avatar 生成但无 avatar_delete」有隐含假设（如生成 ephemeral portrait 后必须可清理）？omit 会不会让 M2 gate 放行一个会留死资产的能力？
3. **read_only_auth_check 上报条件**：get_asset 的 auth 语义是运行时验证（非静态属性）。上报 read_only_auth_check 但 key 无效时，M2 gate 放行（不查 verified）→ 运行时 401。这是 spec 设计（presence-gate 非 preflight-claim）还是 e5c 的过报风险？
4. **journal_probe 只读正确性**：mode=ro URI 读 PRAGMA user_version 是否在所有 SQLite 版本/driver 下可靠？head>6（refuse-downgrade）与 head<6（needs-migration）的分类是否完整？DB 不存在态如何处理？
5. **两个 call site 对称**：director.py（M2/M3 路径）与 project.py（持久化命令）都传真实 probe + v1.1 后，两者上报的 payload 是否**逐字段一致**？是否存在 project.py 持久化 v1.1 但 director.py 生成时重采 v1.0 的残留分支？
6. **doctor fresh re-capture 与 cache 不变量**：doctor fresh 调 capture_capabilities_v1_1 是否绕开了 _stored_capabilities 的 adapter-mismatch 守卫？fresh capture 本身有无副作用（如意外触发 save_capabilities 写盘）？
7. **canary 删除恢复隔离**：canary 驱动 DeletionCoordinator 删 canary 自创的 photo+stock 资源时，能否保证不触碰用户既有资源（隔离 project dir 是否充分，还是 journal 跨 project 共享）？
8. **maintenance 双 adapter + 顺序**：recover_withdrawn_asset_cleanups（DB-only）先、recover_deletions（network）后的顺序是否对（前者清理的 consent_withdrawal 行是否是后者 network sweep 的前置）？双 adapter 同时传 recover_deletions 是否对（签名要求 deleter+adapter 都在）？
9. **maintenance force 透传**：maintenance 命令的 force 参数从 CLI 到 coordinator 的类型是否全程 bool？有无 typer 解析层把 str→truthy 的隐式转换？
10. **claim↔apply 镜像不破**：maintenance recover_deletions 调用是否纯走已锁 coordinator 入口（无旁路直接 adapter.delete）？wiring 有无引入新 truthy force 来源？

---

## 6. 实现顺序

### e5c（先锁，capability 上报真相）

1. **probe 实现**（capabilities.py）：
   - 新增 `_real_adapter_probe()` —— importlib 导入 heygen_http/heygen_videos_adapter/heygen_asset_adapter，证实 `HeyGenHttpTransport`/`HeyGenVideosAdapter`/`HeyGenAssetAdapter` 类可解析；任一 ImportError/AttributeError → False
   - 新增 `_real_journal_probe(project_root)` —— 只读 `file:<db>?mode=ro` URI 读 `PRAGMA user_version`；==6 → True；其他 → False（不调 init_database）
   - `heygen_processor` 仍接受注入 probe（保测试可注入），默认改 `_real_adapter_probe`/`_real_journal_probe`？—— **否**：保 `_not_available` 默认（fail-closed 契约 + 测试注入），由 caller（director/project）显式传真实 probe
2. **operations/features 扩展**（capabilities.py:94-96）：
   - operations → `["direct_asset_upload", "photo_avatar", "prerecorded_audio_lipsync", "asset_delete", "video_delete"]`（**omit avatar_delete**）
   - features → `["idempotency_24h", "title_query", "read_only_auth_check"]`（read_only_auth_check 由 get_asset 背书）
   - 注释从 stale「Only operations the shipped adapter actually implements」改为「反映 §5.5e5b0c3c 已锁原语 + adapter_probe/journal_probe 证实的就绪子集」
3. **director.py:772 接线**：`capture_capabilities_v1_1(env=os.environ, adapter_probe=_real_adapter_probe, journal_probe=lambda: _real_journal_probe(project_root), ...)`
4. **project.py:180 接线**：加 protocol_version 分支（默认 v1.1 或从 state 读），v1.1 时调 `capture_capabilities_v1_1` 传真实 probe
5. **测试同步**（test_capabilities_v1_1.py）：重写 T11 精确 dict 断言；新增 T1-T8；保 T9/T10/ctrl
6. **M1 独立性验证**（T8）：显式测无 key 时 M1 路径
7. 全绿 → 发 Codex 审（round-1，问题 1-5）→ 改 → 直到 lockable → 锁 e5c → commit

### e5d（后锁，依赖 e5c 真实 probe）

1. **doctor v1.1 path**（capabilities.py 新 `doctor_report_v1_1` 或扩 `doctor_report` 签名，见 §7.5 决策）：
   - fresh 调 `capture_capabilities_v1_1` + 活 probe（**不读 cache**）
   - 解析 third_party_processors → HeyGen section：`{configured, operations, features, blockers[key_missing/adapter_unimportable/journal_ahead], warnings[journal_behind_head]}`
   - BLOCKER/WARN 决策逻辑（镜像 f5-capability-check.md）
   - doctor 命令（commands/doctor.py）切到 v1.1 path（保 v1.0 runtime check 不退化）
2. **canary harness**（新 src/lecturecast/canary.py `run_canary(project_dir, transport_factory) -> CanaryReport` + commands/canary.py leaf 命令，见 §7.6 决策）：
   - 隔离 project dir
   - §5 line 489 八项逐项 assert（D-T6 系列）
   - 30-credit cap 硬门禁
   - 删除恢复驱动 DeletionCoordinator，逐资源 assert deleted
   - M1-independence assert
   - 无 HEYGEN_API_KEY 时跳过需 key 项（仅跑 M1-independence + migration head + digest 链本地项）
3. **maintenance 接线**（commands/ 新 maintenance.py 或扩 agent.py，见 §7.7 决策）：
   - 双 adapter 构造（一个 transport → videos deleter + asset adapter）
   - `recover_withdrawn_asset_cleanups` 先（DB-only）→ `recover_deletions` 后（network，传双 adapter）
   - force 参数 typer bool + `type(force) is bool` 守卫
   - 只调已锁 coordinator 入口，无旁路
4. **测试**（D-T1 ~ D-T12）
5. 全绿 → 发 Codex 审（round-1，问题 6-10）→ 改 → lockable → 锁 e5d → commit

### 锁后

- 更新 `docs/DIGITAL-HUMAN-PROGRESS.md`（e5c/d ✅ 行 + 下次会话接续点改 §5.5e6）
- 更新 memory `project_digital_human_edition_progress.md`

---

## 7. 开放决策（需用户/Codex 裁定）

| # | 决策 | 本稿推荐 | 理由 |
|---|------|----------|------|
| 7.1 | 未配 key 时：omit vs configured=False vs 交互式降级 | **omit 给 server（真相）+ director preflight 懒触发交互卡片给用户**（用户裁定 2026-08-02） | server 只收 configured=true 真相，不能报假；但静默 omit 对用户不友好。卡片两选项：A 配 key 反馈 / B 降级 M1。落 e5d（D13）；上报仍 omit |
| 7.2 | avatar_delete omit vs caveat | **omit** | 无独立原语；带 caveat 在 M2 gate 语义无意义；spec min_length=1 不强制 |
| 7.3 | read_only_auth_check 上报 vs omit | **上报**（get_asset 背书） | docstring 权威「doctor only」；spec presence-gate 设计接受运行时 401 |
| 7.4 | journal_probe 只读 URI vs init_database 幂等写 | **只读 URI** | 约束 (b) doctor 只读不写；head<6 是合法未就绪态，由正常路径触发迁移 |
| 7.5 | doctor_report 扩签名 vs 新 heygen_doctor_report | **扩 doctor_report 接受 V1_1 + 保 V1.0 兼容** | 单一 doctor 入口；V1_1 payload 含 V1.0 runtime 子集 |
| 7.6 | canary home: CLI leaf / pytest / module | **module + CLI leaf**（C+A） | 逻辑可 Typer-free 测；CLI 供 operator 跑 |
| 7.7 | maintenance: 扩 agent vs 新 maintenance 命令 | **新 maintenance 命令**（leaf） | agent 是「one safe next action」驱动器；maintenance 是批量恢复，语义不同；完整 HeyGen 生成 driver 留 §5.5e6 |

---

## 附录 A：primitive → operation/feature 映射表（已锁，e5c 上报依据）

| operation/feature | 已锁原语 | 仓库位置 | 锁定子步 |
|------------------|----------|----------|----------|
| direct_asset_upload | AssetUploadProcessor.upload_once + HeyGenAssetAdapter.upload_asset | op_repo:3363,3430; asset_adapter.py | e5b0c2 |
| photo_avatar | HeyGenVideosAdapter.submit_video (type='image') | videos_adapter.py:95 | e5b1 |
| prerecorded_audio_lipsync | HeyGenVideosAdapter.submit_video (同 symbol) | videos_adapter.py:95 | e5b1 |
| asset_delete | AssetDeletionProcessor.delete_once + HeyGenAssetAdapter.delete_asset | op_repo:3737; asset_adapter.py:468 | e5b0c3a/c1 |
| video_delete | DeleteProcessor.delete_once + HeyGenVideosAdapter.delete_video | op_repo:3710; videos_adapter.py:273 | e5b0c3b |
| ~~avatar_delete~~ | （无独立原语；ephemeral portrait 走 asset_delete） | — | omit |
| idempotency_24h | RECONCILE_SEARCH_WINDOW_SECONDS + _ASSET_IDEMPOTENCY_WINDOW_SECONDS + transport Idempotency-Key | op_repo:70,3120; heygen_http.py:31 | e5b0c1/e3 |
| title_query | HeyGenVideosAdapter.query_videos_by_title + ReconcileProcessor | videos_adapter.py:204; op_repo:3468 | e3d |
| read_only_auth_check | HeyGenAssetAdapter.get_asset（docstring「doctor only」背书） | asset_adapter.py:440 | e5b0c3a |

## 附录 B：probe 深度契约（e5c 核心 fail-closed 边界）

```
configured = (
    HEYGEN_API_KEY env non-empty
    AND adapter_probe()  # importlib 导入 3 heygen 模块 + 类可解析
    AND journal_probe()  # 只读 PRAGMA user_version == _SCHEMA_VERSION(=6)
)
# 任一失败 → return None（processor omit，不上报 configured=False）
# 上报的 operations ⊆ {已锁原语背书的 op}（avatar_delete 排除）
# 上报的 features ⊆ {已锁原语背书的 feature}
# probe 证实的就绪栈能服务多少 op/feature，就报多少 —— importable-but-not-ready = 过报 = fail-closed 违反
```
