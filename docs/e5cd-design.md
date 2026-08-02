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

### 1.13 §5.5e5d-c maintenance wiring（D10-D12）锁定记录（2026-08-02）

`src/lecturecast/maintenance.py`（lib：`run_maintenance` + `MaintenanceReport`）+ `src/lecturecast/commands/maintenance.py`（CLI 叶：`lecturecast maintenance --project-root --force --json`）+ `cli.py` 注册（:19 import / :39 `app.command()(maintenance)`）+ `tests/test_maintenance.py`（25 测）。

**8-lens 对抗式设计审计（实现前，Workflow w9d56hv2u）** —— 在写任何实现代码之前先跑了一轮 8 视角 bypass-hunt（force-coercion / dual-adapter / ordering / locked-entry-bypass / key-fail-open / init-database / lease-now-iso / error-reporting）+ 综合。裁决「NOT lockable as-is」—— 1 blocker + 3 major + 6 minor，全部在 `maintenance.py` 写下第一行前并入设计：

| 级别 | finding | 闭合方式（file:line） |
|------|---------|----------------------|
| **B1 blocker** | whitespace-key fail-open：`if not key:` 缺 `.strip()` → transport + capability probe 都 `.strip()` → 三谓词不一致，空白 key 在零候选场景下掩盖配置 bug | `maintenance.py:217-218` 读 transport 自己的 `_api_key_provider()` + `not isinstance(key, str) or not key.strip()`（与 heygen_http.py:107 逐字一致；读 transport provider 而非二次裸读 env = 单一真相，吞并 minor m7） |
| **M1 major** | durable 先用 sentinel 踩在 fresh project：原设计 `init_database(project_dir)` 在 fresh journal 上 touch `.lecturecast/heygen.used`（heygen_journal.py:475）→ runtime/ 删除后 capability probe fail-close | `maintenance.py:179-198` 只读 `_journal_state` gate（mode=ro URI，不创建/迁移/写）—— 仅 `classification=="current"` 放行，其余全 skip 不触 init/recover；`_JOURNAL_SKIP_REASONS` 字典给每类 non-current 一个中文 reason。gate 放行时 recover 方法内部 init 是 schema no-op（head==6） |
| **M2 major** | exit-0 掩盖 partial/skip：原设计「exit 0 even if network_skipped」让 piping script 误读「全删了」 | `maintenance.py:100-111` `MaintenanceReport.clean` 属性（network_skipped False AND failed/alerted/ops_alerted 全 0）+ `commands/maintenance.py:60-61` `if not report.clean: raise typer.Exit(code=2)`。exit 契约 0 clean / 2 partial-or-skip / 1 reserved for harness exception |
| **M3 major** | message 文本未规约 / skip_reason 被淡化为只是一行 payload | `commands/maintenance.py:64-83` `_format_message` 在非 --json 的人类消息里也浮出 db_recovery（cleanup_required/cancelled/kept/manual/left_uploading）+ deletion_recovery（deleted/failed/alerted/ops_alerted）+ skip_reason 逐字（含 ⚠ 前缀 + 「资产未从 HeyGen 删除」+ 「配置 key 后重跑」） |
| m1 minor | lib force 入口守卫（让 D-T11 结果 key/journal 无关） | `maintenance.py:169-170` `if type(force) is not bool: raise ValueError` 在任何 DB 读之前（`type() is bool` 非 isinstance —— int 1/0 会过 isinstance） |
| m2 minor | committed-visibility 只在 call-order 不在 committed-order | 闭合为**结构保证 + 单元证据 + seeded 端到端测**：begin_immediate 在 context 退出时 commit（op_repo:429）；test_asset_journal.py:260-264 单元证；`test_maintenance.py::test_d_t10a_committed_visibility_fresh_conn_sees_cleanup_required` seeded 端到端证（withdrawn receipt + uploaded asset → recover_deletions spy 内开 FRESH conn 见 cleanup_required 已 committed） |
| m3 minor | db_tally 在 post-DB 失败时丢失 | `maintenance.py:239-256` try/except 包 recover_deletions → 失败仍返回带 committed db_tally 的 partial MaintenanceReport（fail-closed intact —— 网络未在未知态跑；reporting 改进非 correctness gap） |
| m4 minor | adapter 调换（videos→adapter=、asset→deleter=）未被测 | `test_maintenance.py::test_d_t10b_dual_adapter_one_transport_not_swapped` 断言 `isinstance(deleter, HeyGenVideosAdapter)` + `isinstance(adapter, HeyGenAssetAdapter)` + 共享 transport identity |
| m5 minor | test_maintenance 是 locked probe 的回归守卫，勿改 probe | 文档化：test_maintenance 不改 `_JOURNAL_READY` / `_api_key_provider`；只测 wiring 层 |
| m6 minor | init_database 必要性 | 吞并于 M1 —— 删掉显式 init_database 调用；recover 方法内部 begin_immediate 自带 init，gate 放行时 init 是 no-op |
| m7 minor | 二次 env 读可能与 transport 实际用的值漂移 | 吞并于 B1 —— 读 transport 自己的 provider |

**测试矩阵（25 测，全绿）** —— `tests/test_maintenance.py`：D-T11 force 非 bool → ValueError（8 例参数化，bare tmp_path 证 key/journal 无关）+ bool False/True 过守卫；D-T12 source-level 静态断言只调两 locked 入口（禁 `.delete_video(` / `.delete_asset(` / `delete_pass_for_operation`）；M1 fresh + parent_unwritable 两态各证 sentinel + DB 未触；D-T10c 空白/未设 key fail-closed（4 例参数化，证 DB pass 仍跑 + deletion_recovery={}）；D-T10a call-order（`["db","net"]`）+ committed-visibility（seeded，fresh conn 见 cleanup_required）；D-T10b+m4 dual adapter 共享 transport + 不调换 + force literal bool；M2 exit 0/2/2/2/2 矩阵（clean / missing-key / whitespace-key / partial-failed / recover_deletions-raises）+ report.clean 属性矩阵。全量 1082 测全绿（基线 823 + 本期增长，零回归）。

**约束遵守** —— (a)/(c) force 是 typer bool，UNCHANGED 透传 recover_deletions（`type(force) is bool` 守卫在 lib 边界先于任何 DB 读；无 `--aggressive`/`--unsafe` 新 truthy 源）✓；(d) 只调两 locked 入口 `recover_withdrawn_asset_cleanups` + `recover_deletions`，源级静态测禁直接 adapter.delete_video/delete_asset/delete_pass_for_operation + **round-2 动态测**（instrument 两 adapter 类所有 delete-named 方法，证运行时零调用）✓；(b) N/A —— maintenance 本身就是恢复 driver，允许 network + 写用户项目（b 是 canary-only 的零网络约束）。fail-closed 主不变量（configured=true ⟺ 栈实际可服务）由 B1+M1 共同保证：whitespace key 不虚报删除、fresh journal 不碰 durable sentinel。

**Codex round-1 复审结果（2026-08-02，effort=low，invariant-completeness framing）—— NOT LOCKABLE，4 blockers + gaps**：

| # | finding | 闭合方式（round-2，file:line） |
|---|---------|-------------------------------|
| B1 blocker | `clean` 忽略 `skipped` + `not_advanced`：原 `failed/alerted/ops_alerted 全 0` 谓词放过 `skipped>0`（一个候选 skipped_no_upload_id/skipped_unknown_kind）+ `not_advanced>0`（busy/retry_wait/not_ready/fence_conflict —— recover_deletions **不聚合**进 8-key 返回，op_repo:395，仅 `attempted-(deleted+failed+skipped+alerted)>0` 可探）→ 一个未删候选被 exit 0 当 clean | `maintenance.py:121-164` `clean` 重写：加 `attempted == deleted` 谓词（一个等式吞掉 skipped + not_advanced 两类，因 attempted = deleted+failed+skipped+alerted+not_advanced，配合上面的 failed/alerted/ops_alerted==0 检查，attempted!=deleted ⟺ skipped>0 或 not_advanced>0） |
| B2 blocker | `clean` 忽略 DB-side `manual` + `left_uploading`：DB pass 可能留下 manual（资产需人工 reconciliation）或 left_uploading（活跃 upload lease 未动 —— fenced apply 下次上传会接住，但本 sweep 确实未解决）→ exit 0 掩盖未决 DB 工作 | `maintenance.py:160-163` `db.get("manual",0) or db.get("left_uploading",0)` → not clean |
| B3 blocker | TOCTOU journal race：`_journal_state` gate 读与 recover 后续 open 是两次文件 open，中间 unchecked 窗口内 CONCURRENT 进程删/换 journal 可让 begin_immediate 的 init_database 重建 fresh journal + 触 sentinel | **文档化为已知局限**（`maintenance.py:253-265`）：单用户本地 CLI，并发 journal mutation 非支持场景；彻底闭合需 operation_repository 加 recovery-only-open 原语（锁态改动，deferred）。M1 不变量按 gate 时观测态成立（12 类 non-current 全 skip，已穷举测） |
| B4 blocker | DB-pass 异常逃逸：`recover_withdrawn_asset_cleanups` 抛出（如 withdrawn receipt 拓扑损坏 → OperationIntegrityError）未被包，逃逸为 harness exit-1 无 MaintenanceReport | `maintenance.py:301-315` try/except 包 DB pass → `db_recovery_failed=True` + `network_skipped=True` + `db_recovery={}` 结构化 skip report（tx 已回滚，无 over-claim；网络不在半恢复 journal 上跑）+ `maintenance.py:119` MaintenanceReport 加 `db_recovery_failed: bool` 字段（区分「DB pass 未完成」vs「DB pass 跑了零工作」） |
| gap | lease/now_iso 入口校验：recover_deletions 在 processor claim 内才校验 lease_seconds（op_repo:111），零候选时无效 lease_seconds/now_iso 永不被校验 → 可能对无效 config 报 clean 空 sweep | `maintenance.py:225-243` 入口校验三 lib-边界参数：`_require_lease_owner(lease_owner)` + `type(lease_seconds) is not int` → ValueError + `_check_lease_seconds(lease_seconds)` + `_parse_utc(now_iso)`（均在 force 守卫之后、任何 DB 读之前；CLI 默认值恒有效，从不经 CLI 触发 —— 只守直接 lib caller） |
| gap | empty-tally 静默：`deletion_recovery={}, network_skipped=False` 在原 `clean` 下被当 clean（畸形/空 coordinator 返回伪装成 no-op sweep） | `maintenance.py:88-95` `_DEL_TALLY_KEYS` frozenset（8 键精确集）+ `maintenance.py:153` `clean` 内 `set(d.keys()) != _DEL_TALLY_KEYS` → not clean + `maintenance.py:381-391` recover_deletions 返回后 shape 校验 → 畸形 tally 转 skip report |
| M3 gap | `_format_message` 只浮 deleted/failed/alerted/ops_alerted，藏 attempted/skipped/ops_driven/ops_empty | `commands/maintenance.py:64-92` 重写：浮出全 8 键（ops_driven/ops_empty/ops_alerted + attempted/deleted/failed/skipped/alerted）+ DB 5 键（含 manual/left_uploading）+ db_recovery_failed 时显式 ⚠ |

**round-2 测试加固（25 → 61 测，+36）** —— `tests/test_maintenance.py`：`test_report_clean_property_matrix` 扩展（覆盖 skipped/busy not_advanced/db manual/db left_uploading/db_recovery_failed/empty shape/missing-key/extra-key 全维度）；新增 `test_m2_exit_2_on_skipped` / `test_m2_exit_2_on_busy_not_advanced` / `test_m2_exit_2_on_db_manual` / `test_m2_exit_2_on_db_left_uploading`（exit-code 维度穷举）；`test_db_pass_exception_wrapped_to_skip_report`（B4：DB 抛 → db_recovery_failed=True + exit 2 非 1）；`test_malformed_tally_shape_wrapped_to_skip_report`（3 例参数化：empty/missing/extra）；`test_entry_guard_rejects_bad_lease_seconds`（8 例：str/float/bool/None/list + 29/3601 越界）+ `test_entry_guard_rejects_bad_now_iso`（4 例：非 ISO / 无 tz / 空 / 非法月日）+ `test_entry_guard_rejects_bad_lease_owner`（5 例：空/过短/过长/含空格/非 ASCII）+ `test_entry_guard_order_force_first`（force 守卫先于 lease/now）；`test_constraint_d_dynamic_adapter_delete_never_called`（constraint d 动态补：instrument 真 adapter 类所有 delete-named 方法，证运行时零调用）；`test_m1_every_non_current_class_skips_without_sentinel`（11 类 non-current 参数化，mock `_journal_state` + patch init_database 为 boom，证 gate 拒绝全 11 类不触 sentinel —— 穷举闭合 M1）。全量 **1118 测全绿**（基线 823 + 本期增长，零回归）。

**待 Codex round-2 锁定复审**（effort=low，invariant-completeness framing：4 blocker 闭合是否穷举 + B3 TOCTOU 文档化是否充分 + 入口校验/shape 校验/db_recovery_failed 语义是否无残留 gap）。

**Codex round-2 复审结果（2026-08-02，effort=low）—— NOT LOCKABLE，2 blocker（B1/B3/B4/entry/M3 全 CONFIRMED closed）**：

| # | finding | 闭合方式（round-3） |
|---|---------|---------------------|
| B1（skipped/not_advanced）| **CONFIRMED closed** —— 算术正确（attempted = deleted+failed+skipped+alerted+not_advanced；failed/alerted/ops_alerted==0 后 attempted==deleted ⟺ skipped+not_advanced==0）；deleted>attempted 不可能（deleted 是 attempts 子集），不等式也拒 | 无需改（维持 round-2 `attempted==deleted` 谓词；round-3 加 `deleted>attempted` inverted 用例 defense-in-depth） |
| **B2 残留 blocker** | **pre-existing `manual_reconciliation_required` 行被算进 `kept`（op_repo:2995），非 `manual`** → `clean` 只查 `manual`/`left_uploading` 漏过预存人工行，exit 0 掩盖未决 | **源处修**：op_repo:2995-2997 改 `tally["manual"] += 1`（不再 `kept`）+ docstring 补 `manual_reconciliation_required → manual` disposition。`kept` 复归纯「resolved/terminal idempotent」语义；`manual` = 所有需人工对账行（本 sweep 新翻 OR 预存）。maintenance 的 `clean` 已有的 `db.get("manual",0)` 门禁直接生效，无需新耦合。爆炸半径：`consent.py:602` 丢弃 tally 返回值；无 locked test 覆盖该路径（补 `test_preexisting_manual_counted_as_manual_not_kept` 回归） |
| B3（TOCTOU）| **CONFIRMED 文档化充分** —— 二次 `_journal_state` 检查太晚（init 可能已重建）；inode/mtime 快照只缩小窗口不闭合；彻底闭合需 recovery-only-open 原语（repo 改动）。单用户本地 CLI 威胁模型下 deferral 合理 | 无需改（维持 round-2 文档化） |
| B4（DB-pass 异常）| **CONFIRMED closed** —— `except Exception` 适宽（KeyboardInterrupt/SystemExit 继承 BaseException 不被吞）；DB recovery 单一 begin_immediate 无子事务/逐行 commit；失败态一致（db_recovery_failed=True/network_skipped=True/deletion_recovery={}/clean=False） | 无需改 |
| 入口校验 | **CONFIRMED closed**（顺序对、type is int 拒 bool、bounds 匹配、_parse_utc 拒 naive）+ **minor caveat**：非 str now_iso 抛 AttributeError（.replace()）、非 str lease_owner 抛 TypeError（regex）而非 ValueError | **round-3 修**：入口加 `type(lease_owner) is not str` + `type(now_iso) is not str` 守卫 → 统一 ValueError（lib 契约一致；CLI 永不触发） |
| **shape 校验 blocker** | **非 type-stable**：`recover_deletions` 返回 None/list/str 时 `del_tally.keys()` 在 try 块外抛 AttributeError → 逃逸 exit 1（非结构化 exit 2）；负值/bool 值漏过；DB tally 5-key shape 完全未强制（`{}`/partial 因 .get 默认 0 过 clean） | **round-3 修**：抽 `_valid_tally(tally, keys)` helper（isinstance dict AND 精确键集 AND 全值 `type(v) is int and v>=0`，bool 被拒）—— type-stable（非 dict 返 False 不抛）；`_DB_TALLY_KEYS` 5-key frozenset；`clean` + `run_maintenance` 双侧用 `_valid_tally` 校验 deletion + DB 两 tally |
| M3（message）| **CONFIRMED closed** —— 全 8 deletion 键 + 5 DB 键 + db_recovery_failed + skip_reason 都浮出 | 无需改 |

**round-3 改动** ——
1. `src/lecturecast/operation_repository.py:2995-2997`（locked primitive 修正）：`manual_reconciliation_required` 计 `manual`（非 `kept`）+ docstring 补 disposition。`tests/test_asset_journal.py` 加 `test_preexisting_manual_counted_as_manual_not_kept`（覆盖此前未测的路径）。
2. `src/lecturecast/maintenance.py`：`_DB_TALLY_KEYS`（5 键）+ `_valid_tally(d, keys)` helper（type-stable shape+value 校验）；`clean` 重写用 `_valid_tally` 双侧校验（deletion 8 键 + DB 5 键）+ inverted `deleted>attempted` defense-in-depth；入口加 `type(lease_owner) is not str` + `type(now_iso) is not str` → ValueError；recover_deletions 后 shape 校验改用 `_valid_tally`（非 dict → 结构化 skip 不抛）。
3. `src/lecturecast/commands/maintenance.py`：exit-code docstring 更新（exit 2 含 DB 失败/畸形 tally；exit 1 = 直接 lib 误用，CLI 永不触发）。

**round-3 测试加固（61 → 82 测，+21）** —— 矩阵扩展（db shape `{}` 不过 clean / deleted>attempted inverted / non-dict None-list-str-int / negative / bool 值全维度）；`test_recover_deletions_non_dict_wrapped_to_skip`（5 例参数化：None/list/str/int/object → 结构化 skip exit 2 非 1）；`test_recover_deletions_bad_value_types_wrapped_to_skip`（3 例：negative/str/bool 值）；`test_m2_exit_2_on_preexisting_manual`（B2 端到端：seed 预存 manual 行 → 真 DB pass 计 manual → exit 2）；`test_db_failure_message_surfaces_warning_and_reason`（M3：DB 抛时消息含 ⚠ DB 行 + reason + 异常类型，无网络行）；`test_entry_guard_now_iso_non_str_raises_value_error`（5 例）+ `test_entry_guard_lease_owner_non_str_raises_value_error`（5 例，统一 ValueError 非 AttributeError/TypeError）；`test_asset_journal.py::test_preexisting_manual_counted_as_manual_not_kept`（locked primitive 回归）。全量 **1139 测全绿**（基线 823 + 本期增长，零回归；op_repo 改动对 consent/deletion-coordinator 等消费者零影响）。

**待 Codex round-3 锁定复审**（effort=low，invariant-completeness framing：B2 源处修是否穷举 + shape 校验 type-stability 是否无残留 + locked primitive 改动是否引入新不变量回归）。

**Codex round-3 复审结果（2026-08-02，effort=low）—— NOT LOCKABLE，5 blocker**：

| # | finding | 闭合方式（round-4） |
|---|---------|---------------------|
| B2（pre-existing manual 分类）| **CONFIRMED closed**（10/10）—— op_repo:2949 每个 status 分支映射正确；amendment at op_repo:3004 纯 tally-only（无 SQL update/状态翻转/resource mutation）；aggregate at op_repo:3077 正确传播 manual；primitive 回归 + real aggregate 端到端测覆盖充分 | 无需改 |
| 入口校验 | **CONFIRMED closed**（顺序对、非 str now_iso/lease_owner 不能达 regex/_parse_utc） | 无需改 |
| **2A（诊断 sorted 混合键）** | `_valid_tally` 正确拒混合型键 dict，但 diagnostic `sorted(del_tally.keys())` 在 try 块**外**对 int+str 混合键抛 TypeError → 逃逸 exit 1 | **round-4 修**：diagnostic 改 `sorted(repr(k) for k in keys)`（repr 恒 str，排序 str 恒 type-stable）；DB + deletion 两 diagnostic 同改 |
| **2B（dict 子类）** | `isinstance(tally, dict)` 接受 dict 子类；子类可覆写 keys()/values()/get() 抛异常；shape 校验在 try 块外 → 逃逸 exit 1 | **round-4 修**：`_valid_tally` 改 `type(tally) is dict`（非 isinstance）—— 协调器契约是 plain dict（`{}` literal + dict 返回）；与 `type() is bool/int/str` 纪律一致；子类走 "non-dict <SubclassName>" 分支不调其 keys() |
| **3（DB tally 边界未校验）** | `run_maintenance` 校验 deletion tally 形状但**不**校验 db_tally 形状 —— 畸形 DB 返回（truthy non-dict / 错形 dict）流入 report，CLI formatter `db.get(...)` 抛 AttributeError → exit 1（`clean` 虽校验但 formatter 先于 `clean` 读） | **round-4 修**：DB pass 后边界校验 `_valid_tally(db_tally, _DB_TALLY_KEYS)` → 畸形则 db_recovery_failed=True + network_skipped=True + 结构化 skip（exit 2 非 1，网络不在半恢复 journal 上跑）；formatter 再加 `isinstance(db, dict)` 守卫 defense-in-depth |
| **#1（非撤回 manual 不可见）** | `manual_reconciliation_required` 不限于撤回 op —— 冻结 24h 重放窗口过期（op_repo:2637）/ 上传失败（op_repo:2855）都产生；但 `recover_withdrawn_asset_cleanups` 只 SELECT withdrawn receipts（op_repo:3080）→ 两 tally 都不见 → exit 0 谎报 clean（fail-closed 违反：宁可少报绝不虚报） | **round-4 修**：加 read-only `count_recovery_attention()` 后置审计（mode=ro URI，不写）—— 计全表 `heygen_asset_uploads.status='manual_reconciliation_required'`；`clean` + exit 0 gate 其上；CLI 浮出 counts |
| **#2（manual_force 不可见）** | deletion 候选 SELECT 排除 manual_force（op_repo:4030，operator-only）→ 非 deleted manual_force 资源永不进 recover_deletions → exit 0 谎报 | **round-4 修**：同审计计 `heygen_remote_resources WHERE deletion_reason='manual_force' AND deletion_status!='deleted'`；deleted 的 manual_force 不计（已解决） |
| #3（stranded cleanup 耦合）| **PUSH BACK**（8/10）—— 撤回 op 的 cleanup_required 资源**会**进 deletion 候选（non-deleted + non-reusable → op_repo:4011 SELECT 选出 → deletion pass 删；失败则 deletion tally `failed/skipped` → `attempted!=deleted` → 不 clean）。asset-behind-unverified-video 是 §3.5 正常 video-first 在途排序（非卡死），下趟 video 删后即释放；计它会令正常多资源撤回清理永不到 exit 0（把"在途"误判"卡死"） | **不改**（文档化：在途 ≠ 卡死；video 被卡时**那**是 attention 态，由 video 自身状态浮现，asset 是正确等待，非重复计） |

**round-4 改动** ——
1. `src/lecturecast/operation_repository.py`：新增 `count_recovery_attention()` 只读方法（op_repo recover_withdrawn_asset_cleanups 之后）—— mode=ro URI（mirror `_journal_state` capabilities.py:340），不创建/迁移/写；COUNT manual_uploads + manual_force_resources；raise 则 maintenance fail-closed（attention_audit_failed → exit 2）。**纯加法只读诊断方法，不改任何 locked mutation primitive**（claim/apply/recover 不变；constraint d 关于不旁路 locked 删除链——只读 COUNT 不旁路任何删除）。
2. `src/lecturecast/maintenance.py`：`_valid_tally` 改 `type(tally) is dict`（2B）；两 diagnostic 改 `sorted(repr(k) ...)`（2A）；DB pass 后边界 `_valid_tally(db_tally, _DB_TALLY_KEYS)` 校验 → 畸形结构化 skip（3）；`_ATTENTION_KEYS` 2 键 frozenset；`MaintenanceReport` 加 `attention` + `attention_audit_failed` 字段；`clean` 加 (h) attention gate（audit 未失败 + 形状合规 + 全零）；recover_deletions 后 + DB 校验后调 `count_recovery_attention()`（try/except → attention_audit_failed）。
3. `src/lecturecast/commands/maintenance.py`：exit-code docstring 扩（exit 2 含 attention 态 / attention_audit_failed / DB 畸形；exit 1 永不经 CLI）；`_format_message` 加 `isinstance(db/d/attention, dict)` 守卫 + 浮出 attention counts + audit 失败 ⚠ 行。

**round-4 测试加固（82 → 96 测，+14）** —— 矩阵扩展（attention_audit_failed / attention 非法形 / manual_uploads / manual_force_resources 各维 → 不 clean）；`test_db_tally_malformed_wrapped_to_skip`（5 例参数化：truthy non-dict list/str + 错形/extra/negative dict → exit 2 非 1，network 不跑）；`test_recover_deletions_mixed_type_keys_wrapped_to_skip`（2A：8 str 键 + int 键 → repr 排序 type-stable → exit 2 非 TypeError）；`test_valid_tally_rejects_dict_subclass`（2B：dict 子类被拒，plain dict 通过）；`test_attention_audit_non_withdrawn_manual_gates_clean`（#1 端到端：grant 非 withdraw + dock manual → 真 DB pass 返空 + 真 audit 计 manual_uploads=1 → exit 2）；`test_attention_audit_manual_force_resource_gates_clean`（#2：pending manual_force 计 1 / deleted manual_force 不计 → exit 2）；`test_attention_audit_failure_wrapped_to_skip`（audit 抛 → attention_audit_failed + 两 pass 已提交 → exit 2）；`test_attention_audit_clean_journal_zero_attention_exit_0`（happy path：空 journal → audit 2 键零 → exit 0，pin audit 形状）。全量 **1150 测全绿**（1139 + 11 新，零回归）。

**待 Codex round-4 锁定复审**（effort=low，invariant-completeness framing：5 blocker 闭合是否穷举 + `count_recovery_attention` 只读方法是否引入新不变量回归 / SQL 注入面 / TOCTOU + #3 push-back 的 §3.5 在途论证是否成立 + attention audit 是否漏计其他 operator-attention 态）。

**Codex round-4 复审结果（2026-08-02，effort=low）—— NOT LOCKABLE，3 gap**（2 blocker + 1 residual；2A/2B/3/#1/#2/#3 全部 CONFIRMED closed）：

| # | finding | 闭合方式（round-5） |
|---|---------|---------------------|
| 2A 残留（hostile `__repr__`）| `_valid_tally` 已拒混合型键 + diagnostic 已改 `repr(k)`；但 `repr()` **非 total** —— 键可实现 `__repr__` 抛异常（`class BadKey: def __repr__(self): raise RuntimeError`）；diagnostic 在 try 块外 → RuntimeError 逃逸 exit 1 | **round-5 修**：抽 `_safe_key_repr(k)` helper（try/except 包 repr，抛则返 `"<unprintable key>"`，total 不抛）；DB + deletion 两 diagnostic 改 `sorted(_safe_key_repr(k) ...)` —— 与 `type() is dict` / `type() is bool` 同一 type-stability 纪律（诊断路径与校验路径同等 type-stable） |
| 残留 1（attention tally 边界未校验）| DB + deletion 两 tally 在 `run_maintenance` 边界 `_valid_tally` 校验，但 attention **不**校验 —— `count_recovery_attention` 返回 dict 子类 / 非法形时流入 report，CLI formatter `isinstance(at, dict)` 通过后 `at.get(...)` 抛 → exit 1（`clean` 虽校验 attention 形状但 formatter 先于 `clean` 读）| **round-5 修**：audit 后边界校验 `_valid_tally(attention, _ATTENTION_KEYS)` → 畸形则 `attention_audit_failed=True` + `attention={}` + 结构化 skip（exit 2 非 1）；formatter 三守卫 `isinstance` → `type() is dict`（db/deletion/attention defense-in-depth 一致） |
| 残留 2（unrecoverable 异常态不可见）| attention audit 只计 manual_uploads + manual_force；但 schema CHECK 允许 **schema-legal 异常 (status, reason) 对**（`deletion_pending+NULL` / `deletion_failed+NULL` / `not_started+reason`）—— 候选 SELECT 的 witness STATE-MATRIX 门禁**自身**对这些 fail-closed（op_repo:4190-4322 显式 "schema-legal corrupt/直插 states"），即删除子系统已认定它们 attention-needed；但两 recovery primitive + attention audit 都不碰 → exit 0 谎报 clean | **round-5 修**：`count_recovery_attention` 加第 3 键 `unrecoverable_resources` —— 计非 deleted、非 manual_force、非 reusable 的资源中：(a) `not_started + non-NULL reason`、(b) `(pending|failed) + NULL reason`、(c) `created_by_operation_id IS NULL`（孤儿）。三类均 primitive-unreachable（claim 同 UPDATE 设 status+reason；heygen_operations 永不 DELETE → FK SET NULL 不触发），即仅 corruption/直插 产生；计它们不破正常 exit 0（producer-valid journal 上恒为 0）。与候选 SELECT 的 fail-closed 姿态**一致**（exit-0 契约不能对删除子系统自身拒绝驱动的行谎报 clean） |

> **round-5 关键论证（为何 unrecoverable 计数是正确的，而非 TOCTOU 类）**：候选 SELECT 的 docstring 明确把这些状态标为 "schema-legal corrupt/直插 states, not just producer-reachable ones" 并 fail-closed。即删除子系统**已承认**它们 attention-needed。maintenance exit-0 契约是「journal 最终态无需人介入」—— 若审计时此类行存在（无论怎么产生的），exit-0 就是谎报。fail-closed 覆盖**最终 journal 态**，非仅 primitive-reachable 态。这与已文档化的 journal-replacement TOCTOU 类**不同**：TOCTOU 是维护运行**期间**并发替换 journal；unrecoverable 是审计**时刻**已存在的行。`原则陈述正确 ≠ 实现穷举` —— 候选 SELECT 已穷举这些态，attention audit 必须镜像同一穷举。
>
> **范围裁定（为何不全量镜像候选谓词的 6 类拓扑）**：broken-topology 资源（合法 state-matrix 但缺 ref / credential 不匹配 / role-kind 错配）若其 op 仍被候选 SELECT 选出（有合法 witness 兄弟），per-op pass 会 claim 它 → 抛 OperationIntegrityError → `ops_alerted` → exit 2（**已可见**，非 invisible）。仅当 broken-topology 资源是 op 的**唯一**资源且非合法 witness 时才 invisible —— 这要求直接破坏 ref/credential（primitives 先校验再 INSERT），属已文档化的 journal-replacement TOCTOU 类。state-matrix 异常 + 孤儿是**廉价、明确正确**的子集（schema CHECK 直接可查），不需要镜像 6 类拓扑谓词。

**round-5 改动** ——
1. `src/lecturecast/maintenance.py`：新增 `_safe_key_repr(k)` helper（try/except 包 repr → total）；DB + deletion 两 diagnostic 改 `sorted(_safe_key_repr(k) ...)`（2A 残留）；audit 调用后加 `_valid_tally(attention, _ATTENTION_KEYS)` 边界校验 → 畸形则 `attention_audit_failed=True` + `attention={}` + 结构化 skip（残留 1）；`_ATTENTION_KEYS` 扩 3 键（加 `unrecoverable_resources`）；`clean` (h) gate 加 `unrecoverable_resources` 维；`MaintenanceReport.attention` docstring 同步。
2. `src/lecturecast/operation_repository.py`：`count_recovery_attention()` 加第 3 个 COUNT（`unrecoverable_resources`）—— 镜像候选 SELECT 的 state-matrix 门禁 + 孤儿子句；返回 3 键 dict。**纯加法只读诊断，不改任何 locked mutation primitive**。
3. `src/lecturecast/commands/maintenance.py`：exit-code docstring 扩（exit 2 含 unrecoverable）；`_format_message` 三守卫 `isinstance` → `type() is dict`（defense-in-depth）；浮出 `unrecoverable_resources` count。

**round-5 测试加固（92 → 105 测，+13）** —— `test_db_tally_malformed_hostile_repr_key_wrapped_to_skip`（2A 残留：`__repr__` 抛的键 → `_safe_key_repr` 兜住 → exit 2 非 1）；`test_deletion_tally_malformed_hostile_repr_key_wrapped_to_skip`（deletion 对称）；`test_attention_tally_malformed_wrapped_to_audit_failed`（6 例参数化：dict 子类 / None / list / 错键 / 负值 / extra 键 → 边界校验 → `attention_audit_failed` + exit 2 非 1）；`test_attention_audit_unrecoverable_pending_null_reason_gates_clean`（残留 2-a：`pending+NULL` → unrecoverable=1 → exit 2）；`test_attention_audit_unrecoverable_not_started_with_reason_gates_clean`（残留 2-b：`not_started+post_download` → exit 2）；`test_attention_audit_unrecoverable_failed_null_reason_gates_clean`（残留 2-c：`failed+NULL` → exit 2）；`test_attention_audit_unrecoverable_orphan_resource_gates_clean`（孤儿：`created_by_operation_id IS NULL` → exit 2）；`test_attention_audit_normal_inflight_states_not_counted_unrecoverable`（control：5 种正常态 + reusable_avatar 全不计 → 正常 journal 仍达 exit 0，不破在途可达性）。全量 **1163 测全绿**（1150 + 13 新，零回归）。注：round-4 设计稿曾误记 maintenance "96 测"（实际 92，已修正）；全量 1150 数字正确。

**待 Codex round-5 锁定复审**（effort=low，invariant-completeness framing：3 gap 闭合是否穷举 —— `_safe_key_repr` 是否 total、attention 边界是否对称 DB/deletion、unrecoverable SQL 是否正确镜像候选 state-matrix 门禁且不破正常 exit 0；残留猎：是否还有 operator-attention 态未被三 tally + unrecoverable 覆盖；broken-topology 范围裁定是否成立）。

### 1.13a round-6 闭合（Codex round-5 NOT LOCKABLE，2 blocker）

Codex round-5 复审裁定 **NOT LOCKABLE**，2 blocker（G2 attention 边界 + G3 state-matrix 域确认闭合；G3 rationale 确认 sound）。两者均成立：

| # | Codex round-5 blocker | round-6 闭合 |
|---|----------------------|--------------|
| 1 | `_safe_key_repr` 的 `except Exception` **不能**兜住 `BaseException` 子类（`KeyboardInterrupt` / `SystemExit`）—— 一个 `__repr__` 抛 `KeyboardInterrupt` 的键会逃逸为 exit 1；helper 的 "CANNOT raise / ANY exception" docstring 是**假的**。catch `BaseException` 被禁（会吞用户的 Ctrl+C） | **严格 total 修**：diagnostic **完全不调 `repr()`** —— 改 `len()` on plain dict（builtin，不能抛）。畸形 tally 的精确键对 operator action 无操作价值（run doctor），键数 + 期望键集已足够描述 shape mismatch。删 `_safe_key_repr` helper；DB + deletion 两 diagnostic 改 `got = f"dict 含 {len(tally)} 键"`（non-dict 分支用 `type(x).__name__`，同 lines 379/384/389 的 builtin-safe 模式） |
| 2 | **broken-topology gap**：一个**唯一**资源，state-matrix 正常（pending/failed + post_download/consent_withdrawal）但拓扑断裂（缺/外 ref、credential 不匹配、active op-lease、缺 upload-binding、kind 错配）→ 候选 SELECT 的 witness 谓词**拒绝整个 op**（无 r2 满足）→ op 从不被选出 → `ops_alerted=0` AND round-5 `unrecoverable_resources=0`（domain b 只查 state-matrix，漏拓扑）→ **exit 0 谎报**。round-5 的范围裁定（"broken-topology = TOCTOU 类，不计"）**被 Codex 反驳**：静态 corruption 审计时已存在，与 state-matrix 异常同属 direct-corruption 威胁模型（direct INSERT 绕 validate-then-INSERT），**非** journal-replacement TOCTOU 类（后者是维护运行**期间**并发替换） | **共享谓词 + domain (c)**：(1) 抽 `_DELETION_WITNESS_SUBQUERY_SQL` 模块级常量（候选 SELECT witness 子查询的逐字拷贝，含全部 round-6..13 注释）—— `recover_deletions` 候选 SELECT + `count_recovery_attention` unrecoverable COUNT **共用同一谓词**，消除 Codex round-4/5 反复警告的手工镜像 6+ 拓扑类的 drift 风险；(2) unrecoverable COUNT 加 domain (c)：`status IN (pending,failed) AND reason IN (post_download,consent_withdrawal) AND created_by_operation_id IS NOT NULL AND NOT EXISTS(<共享谓词>)`。外层表别名 `r`（共享谓词相关于 `r.created_by_operation_id`，同候选 SELECT） |

> **round-6 关键设计裁决（范围反转）**：round-5 我裁定 "broken-topology 是 TOCTOU 类、不计" 并用 "primitives 先校验再 INSERT" 论证。Codex 正确反驳：**静态 corruption 审计时已存在**就属于 direct-corruption 威胁模型 —— 与 state-matrix 异常（domain b）**同一**模型（两者都需 direct INSERT 绕过 validate-then-INSERT）。我的 "TOCTOU 类" 标签是**不一致**的：state-matrix 异常也需 direct INSERT，我却计了它们。要么都不计（回到 round-4 谎报），要么都计。round-6 选都计，且用**共享谓词**（非手工镜像）消除 drift。`原则陈述正确 ≠ 实现穷举` —— "fail-closed 覆盖最终态" 这一原则 round-5 已陈述，但实现（domain b 只查 state-matrix）没穷举到拓扑域。

> **domain (c) 范围限定（避免假阳性）**：domain (c) **只**覆盖 claim-eligible 态（pending/failed + post_download/consent_withdrawal），**不**覆盖 `not_started+NULL` 的正常在途 pre-video portrait。否则每个未生成 video 的 op 上的 pre-video 资产都会被计（op 无 witness）→ 正常多资源 pre-video 处理期间 exit 0 不可达。claim-eligible 限定保持 default sweep 刻意的 pre-video 排除（resolver 未释 tail）。

> **共享谓词的 drift 防御**：candidate SELECT（`recover_deletions`，op_repo:~4394）与 attention audit（`count_recovery_attention` unrecoverable COUNT）现在引用**同一** `_DELETION_WITNESS_SUBQUERY_SQL` 常量。任何 witness 谓词的演化（新增拓扑类、收紧门禁）只改一处，两处自动一致。这是 Codex round-4/5 反复建议的 "factor into a shared SQL fragment / read-only selector"。常量 docstring 记录两 caller + 相关变量 `r.created_by_operation_id` + 内部别名（r2/o/ref/ref2/rv/refv/u）局限于子查询 SQLite scope。

**round-6 改动** ——
1. `src/lecturecast/maintenance.py`：**删** `_safe_key_repr` helper；DB-tally diagnostic（`got = f"dict 含 {len(db_tally)} 键"`）+ deletion-tally diagnostic（对称 `len(del_tally)`）—— 两处都不调 `repr()`，`len()` on plain dict 是 builtin-safe；non-dict 分支保留 `type(x).__name__`。
2. `src/lecturecast/operation_repository.py`：新增模块级常量 `_DELETION_WITNESS_SUBQUERY_SQL`（候选 SELECT witness 子查询逐字拷贝，`class DeletionCoordinator:` 之前）；`recover_deletions` 候选 SELECT 内联子查询改 `"AND EXISTS (" + _DELETION_WITNESS_SUBQUERY_SQL + ") "`（行为零变化，纯机械抽取，c3 68 测验证）；`count_recovery_attention` 第 3 COUNT 加 domain (c) `NOT EXISTS(<共享谓词>)`，外层别名 `r`；docstring 扩三域 (a 孤儿 / b state-matrix 异常 / c broken-topology) + 共享谓词 + 范围限定。**纯加法只读诊断，不改任何 locked mutation primitive**（候选 SELECT 行为零变化）。
3. `src/lecturecast/commands/maintenance.py`：exit-code docstring + attention 注释 + operator message 扩 "断裂拓扑资源"（domain c）。

**round-6 测试加固（105 → 109 测，+4）** —— `test_db_tally_malformed_baseexception_repr_key_wrapped_to_skip`（blocker 1 严格 total：`__repr__` 抛 `KeyboardInterrupt`（BaseException）→ round-5 `except Exception` 兜不住会逃逸 exit 1；round-6 `len()` 不调 repr → exit 2 非 1）；更新两 hostile-repr 测试 docstring（round-5 `except Exception` → round-6 no-repr）；`test_attention_audit_unrecoverable_broken_topology_no_witness_gates_clean`（blocker 2 RED：唯一 pending+post_download portrait、无 video、无 upload-binding → 无 witness → unrecoverable=1 → exit 2，round-5 会 exit 0 谎报）；`test_attention_audit_unrecoverable_broken_topology_with_video_witness_not_counted`（blocker 2 control：同一断裂 portrait + `not_started+NULL` video witness → op 可选 → domain c 不计 → exit 0，证明无假阳性 + per-op pass 是 selectable op 的 authority）；`test_attention_audit_unrecoverable_pre_video_asset_no_witness_not_counted`（范围限定 guard：唯一 `not_started+NULL` pre-video portrait、无 witness → domain c 不计（只覆盖 claim-eligible）→ exit 0，防假阳性破坏在途可达性）；修 `test_attention_audit_normal_inflight_states_not_counted_unrecoverable`（加 `not_started+NULL` video witness 让 op 可选，否则 n2/n3/n4 被 domain c 误计）。全量 **1167 测全绿**（1163 + 4 新，零回归）。

**待 Codex round-6 锁定复审**（effort=low，invariant-completeness framing：2 blocker 闭合是否穷举 —— (1) diagnostic 是否真的无 `repr()` 残留（grep 确认）、`len()` on plain dict 是否 builtin-safe；(2) domain (c) 的 `NOT EXISTS(<共享谓词>)` 是否与候选 SELECT 的 `EXISTS(<共享谓词>)` 严格对偶、外层别名 `r` 是否一致、范围限定（只 claim-eligible）是否避免假阳性；残留猎：共享常量抽取是否引入新 drift、是否有 claim-eligible 态仍被 domain c 漏计、是否有正常态被 domain c 误计）。

### 1.13b round-7 闭合（Codex round-6 NOT LOCKABLE，1 blocker + 1 doc nuance）

Codex round-6 复审裁定 **NOT LOCKABLE**，1 code blocker（B2 共享谓词 + domain c 确认 sound；scope-reversal 确认 consistent；control test 确认 correct）+ 1 doc-only nuance。blocker 成立：

| # | Codex round-6 blocker | round-7 闭合 |
|---|----------------------|--------------|
| B1 | round-6 删了 diagnostic 的 `repr()`，但 `_valid_tally`（在 diagnostic **之前**跑、且在 `_valid_tally → True` 后 diagnostic 才跑）仍调 `set(tally.keys())` —— 这会**重新 hash 每个键**。一个 `__hash__` 抛 `KeyboardInterrupt`（BaseException）的键会在 `set()` 构造期逃逸为 exit 1（`except Exception` 兜不住；catch `BaseException` 被禁）。同理 diagnostic 的 non-dict 分支 `type(x).__name__` —— 一个 metaclass 把 `__name__` 实现为抛 `KeyboardInterrupt` 的 descriptor 时，`.__name__` 读取也逃逸。即 round-6 的 "diagnostic 不调 repr" 只堵了 **repr** 这一个洞，`__hash__` / `__name__` 两个同族洞仍在 | **C-builtin-only 严格 total 修**（治本，非 whack-a-mole）：`_valid_tally` 在 hash 任一键**之前**加守卫 `if any(type(k) is not str for k in tally): return False`。论证链（全部 C-builtin，provably 不能抛）：(i) 迭代 plain dict 的 builtin `__iter__` 按 stored slot 产出，**不**调键的 `__hash__`/`__eq__`；(ii) `type(k) is not str` 是**身份比较**（`is`），不调 `__eq__`/`__hash__`；(iii) 一旦确认每个键都是 builtin `str`（`type(k) is str`），后续 `set(tally.keys())` 只 hash `str.__hash__`（C builtin，不能抛）。diagnostic non-dict 分支：`type(x).__name__` → 固定字符串 `"non-dict"`（不读 metaclass descriptor）。两 probe 先复现（`_StatefulHashKey`：`__hash__` 成功一次后抛 `KeyboardInterrupt`；`_HostileNameValue(metaclass=_HostileNameMeta)`：metaclass `__getattribute__("__name__")` 抛 `KeyboardInterrupt`）确认 round-6 下两洞都逃逸 exit 1，round-7 修后两 probe 都 exit 2 |

> **round-7 元教训（totality whack-a-mole 的治本）**：round-4 `repr()` → round-5 `except Exception` → round-6 删 repr 改 `len()` → round-6 仍漏 `__hash__`/`__name__`。每次堵一个 dunder，下一个冒头。治本不是 "再堵一个"，而是**只调 provably 不能抛的 C-builtin 操作**（`type()` / `len()` / 身份 `is` / plain-dict builtin `__iter__`），并守卫在调任何可能触发用户 dunder 的操作**之前**。`_valid_tally` 的 str-guard 就是这个模式：先 `type(k) is str`（builtin 身份，不触发 dunder），确认全是 builtin str 后才允许后续 hash（此时只可能是 `str.__hash__`，C builtin）。这与 c3 的 "claim↔apply 跨 tx 逐字段镜像" 同构 —— 不能只堵被点名的那个字段/dunder，要穷举整族。

> **doc-only nuance（domain c 文档过度声称）**：Codex round-6 item-4 指出 round-6 docstring/SQL COMMENT 把 domain (c) 描述为 "exclusively static corruption / primitive-unreachable, zero on any producer-valid journal" —— 这**不准确**。⚠️ **本段为 round-7 当时记录；其自身仍含两处 round-8 才发现的不准确**（"只 UPDATE receipt" 应为 "UPDATE receipt + op consent-lifecycle 字段，但不动 lease"；"无 video witness" 应为 "active lease 是唯一 non-selectability 因由，B2 允许 zero-video"），round-8 已纠正，见 §1.13c/§1.13d。保留原文以存历史轨迹：~~`withdraw`（consent.py:591）只 UPDATE receipt，**不清 op lease**；若 op 仍 leased 且无 video witness，被 enqueue 的 `consent_withdrawal` cleanup 资产会停在 `deletion_pending+consent_withdrawal` 且无 witness，直到 lease 清除 + resolver 释 tail。~~ 准确版本（round-8）：`withdraw` UPDATE receipt + op consent-lifecycle 字段但**不动 lease**；唯一 c2 non-selectability 因由是 active lease（branch B `o.lease_owner IS NULL AND o.lease_expires_at IS NULL` gate），无 video 非独立因由（B2 允许 zero-video）。这是 **producer-valid 的瞬态 blocked-pending**（非 corruption），count **不保证**在每个 producer-valid journal 上为 0 —— 只在 SETTLED journal 上为 0。Codex 裁定 "no code fix required for the stated invariant; a documentation correction is warranted"。round-7 把 domain (c) 拆成 (c1) static corruption + (c2) transient blocked-pending；round-8 纠正 c2 的三处 doc 不准确。

**round-7 改动** ——
1. `src/lecturecast/maintenance.py`：`_valid_tally`（~line 122）加 `if any(type(k) is not str for k in tally): return False`（在 `set(tally.keys()) != keys` **之前**）；docstring 记 C-builtin 论证链（iter 不 hash / `is` 不调 dunder / builtin str 的 `str.__hash__` 不能抛）。DB-tally diagnostic non-dict 分支 `type(x).__name__` → `"non-dict"`（不读 metaclass）。
2. `src/lecturecast/operation_repository.py`：`count_recovery_attention` docstring（~3121-3185）domain (c) 拆 c1/c2 + "NOT counted" 段加 selectability 交叉引用；SQL COMMENT（~3217）同步 c1/c2 + 删 "a producer-valid journal has ZERO such rows" 过度声称（改为 (a)/(b) zero on producer-valid, (c) splits）。
3. 无 CLI 改动（round-6 已加 "断裂拓扑" 措辞，c1/c2 是 lib 内部区分，operator 仍只见 unrecoverable_resources 计数 + run-doctor skip_reason）。

**round-7 测试加固（109 → 111 测，+2）** —— `test_db_tally_malformed_stateful_hash_key_wrapped_to_skip`（B1 hash 洞：`_StatefulHashKey` 的 `__hash__` 成功一次后抛 `KeyboardInterrupt` → round-6 `set(keys)` 在第二次 hash 逃逸 exit 1；round-7 str-guard 先拒（非 str 键）→ exit 2 非 1）；`test_db_tally_malformed_hostile_name_value_wrapped_to_skip`（B1 name 洞：`_HostileNameValue(metaclass=_HostileNameMeta)` 的 metaclass `__getattribute__("__name__")` 抛 `KeyboardInterrupt` → round-6 diagnostic `type(x).__name__` 逃逸 exit 1；round-7 改固定 `"non-dict"` → exit 2 非 1）。两 RED-then-GREEN probe 先复现 round-6 漏洞再验证 round-7 闭合。全量 **1169 测全绿**（1167 + 2 新，零回归）。

**待 Codex round-7 锁定复审**（effort=low，invariant-completeness framing：B1 闭合是否穷举 —— (1) `_valid_tally` 的 str-guard 是否真的在 hash 任何键之前跑、`type(k) is str` 是否真的不调 dunder、builtin str 的 `str.__hash__` 是否真的不能抛；(2) diagnostic non-dict 分支改 `"non-dict"` 后是否还有任何残留 `.__name__` / `repr()` / 用户 dunder 调用；残留猎：`_valid_tally` 是否还有第 3 个 totality 洞（如 `tally.values()` 的 `type(v) is int` 是否 builtin-safe、`v >= 0` 是否调 `__ge__`）；doc c1/c2 区分是否与 consent.py:591 实际行为一致）。

### 1.13c round-8 闭合（Codex round-7 NOT LOCKABLE，1 doc-only blocker）

Codex round-7 复审裁定 **NOT LOCKABLE**，**唯一**剩余 blocker 是 round-7 c2 文档的三处不准确（**executable invariant + 两 strict-totality blocker B1 hash/name + residual totality 全部 CONFIRMED closed**；7 个其他 `.__name__` site CONFIRMED scoped —— 我的 scope reasoning 成立，**非** c3 asymmetry；111 测全过；两 RED/GREEN probe 经 Codex 在 `fef403f^` 实测确认 round-6 下确抛 `KeyboardInterrupt`）。三处 doc 不准确（Codex 逐条点名，我已 against source 核实）：

| # | round-7 doc 不准确 | round-8 修正 |
|---|-------------------|--------------|
| 1 | c2 docstring 说 `withdraw` "UPDATE only the receipt"（consent.py:591）| **不准确**：`_withdraw_in_tx`（consent.py:657-673）UPDATE receipt **AND** `heygen_operations` —— pristine op 改 `status='cancelled'` + 清 `consent_receipt_digest` + `updated_at`；非 pristine 清 `consent_receipt_digest` + `updated_at`。**窄claim 才真**：**不动 lease**（`lease_owner`/`lease_expires_at`/`lease_fence`/`attempt_started_at` 全不变）。修：c2 文案改 "UPDATEs the receipt AND the op's consent-lifecycle fields (status for pristine, consent_receipt_digest, updated_at) but does NOT clear/modify the op lease"，引 consent.py:591-673 |
| 2 | c2 说 op 非 selectable 是因 "active lease **和/或无 video**" | **夸大**：branch B2 witness **显式允许 zero-video op**（`COUNT(video) <= 1` 非 `== 1`；常量 @4091-4134 注释明言 "zero is allowed"，"B2's witness is a non-video ASSET, so the op may legitimately have 0 video rows"）。所以**无 video 单独不构成 non-selectability**——一个 properly-bound asset 在 lease 清后即使 0 video 也经 B2 witness。**唯一** c2 non-selectability 因由是 active lease（branch B 的 `o.lease_owner IS NULL AND o.lease_expires_at IS NULL` gate @4031）。修：删 "and/or that has no video yet" / "no video yet on a pre-video op"，改为 "the active lease makes branch B's `o.lease_owner IS NULL AND o.lease_expires_at IS NULL` gate reject"；"NOT counted" 段同改（"no witness — leased or pre-video" → "no witness — typically an ACTIVE LEASE; `no video` alone does NOT make an op non-selectable, since branch B2 admits zero-video ops"）|
| 3 | c2 说 auto-resolve 需 "lease 清 **且 video 出现**（for post_download）" | **过强**：download-verified op 上一个 properly-bound B2 `post_download` asset 可在 0 video 行下 witness（video 可能已被 hard-purged）。新 video 非必需。修：auto-resolve 改 "branch B 的 gate 要 `lease_owner IS NULL AND lease_expires_at IS NULL` —— 单纯时间过期（expiry）**不**通过（列必须被清成 NULL）；fenced recovery/apply/finalize/release 路径才 NULL 它们（download finalize/fail ~1652/1702/1718，`_clear_operation_lease` ~1959/1972）。一旦某路径清了两列，gate 通过；properly-bound asset 经 B2 witness（`consent_withdrawal` delivery-independent；`post_download` 需 `download_status='verified'` + `COUNT(video) <= 1`，均可 0 video 满足）"。⚠️ round-8 初稿误写 "(expired / fenced / released)"，Codex round-8 抓出 "expired" 误导（implies 时间过期 alone 通过 gate），round-9 已纠正 |

> **round-8 元教训（doc 也要 against source 核实，不能想当然）**：round-7 我写 c2 docstring 时，凭"withdraw 不清 lease"的正确直觉，顺手写了"只 UPDATE receipt"和"无 video 也阻塞"—— 两处都没 against `_withdraw_in_tx` 实际 SQL + B2 witness 常量核实。Codex 读了源码（consent.py:657-673 + 常量 @4091-4134）当场抓到。这和 c3 的 "原则陈述正确 ≠ 实现穷举" 同构：**doc 也不能只凭原则直觉写，每个事实claim 要 against ground-truth source 核实**（"先查已有参考别试错" 纪律对 doc 同样适用）。幸而纯 doc，无 runtime/SQL 改动，executable invariant 不受影响。

**round-8 改动**（纯 doc，零 runtime/SQL/test 变化，1169 全绿不变）——
1. `src/lecturecast/operation_repository.py`：`count_recovery_attention` docstring (c) 段重写（删 "no video yet on a pre-video op" cause；c2 重写为 "active lease 是唯一 non-selectability 因由 + withdraw UPDATE receipt+consent-lifecycle 但不动 lease + B2 允许 zero-video + auto-resolve 不需新 video"）；SQL COMMENT (c2) 同步；"NOT counted" 段改 selectability 交叉引用（删 "pre-video"，明言 "no video alone does NOT make an op non-selectable"）。

**待 Codex round-8 锁定复审**（effort=low，invariant-completeness framing：三处 doc 修正是否 against source 准确 —— (1) `_withdraw_in_tx` consent.py:657-673 是否真的 UPDATE receipt+op consent-lifecycle 但不动 lease；(2) B2 常量 @4091-4134 是否真的 `COUNT(video) <= 1` 允许 zero-video；(3) "NOT counted" 段 selectability 区分是否与 candidate SELECT 严格一致；残留猎：是否还有 c2 相关 doc 不准确、round-7 的 c1 描述是否也有同类问题）。

### §1.13d round-9（Codex round-8 NOT LOCKABLE → 全闭合，待 Codex round-9 锁定复审）

Codex round-8 裁定 **NOT LOCKABLE**，2 个纯 doc-only blocker（executable invariant 不受影响）：

| # | Codex round-8 指出的不准确 | round-9 修正（against source 核实） |
|---|---|---|
| 1 | round-8 的 c2 auto-resolve 写 "once the lease clears independently (**expired** / fenced / released)" —— "expired" 误导：branch B 的 lease gate 要求 `lease_owner IS NULL AND lease_expires_at IS NULL`，单纯时间 EXPIRY 不通过（两列必须被显式清成 NULL）。 | grep 全仓 `lease_owner=NULL`/`lease_expires_at=NULL` 命中的**清 lease 原语**：download finalize/fail inline SQL（~line 1644/1652/1702/1718）+ `_clear_operation_lease` helper（def @~2514，由 video-deletion fenced-apply 路径 @~1959/1972/1983 调用）。docstring c2 auto-resolve + SQL COMMENT c2 改为："branch B's gate requires `lease_owner IS NULL AND lease_expires_at IS NULL` — mere time EXPIRY does NOT pass it (the columns must be cleared); a fenced recovery/apply/finalize/release path is what NULLs them." |
| 2 | `e5cd-design.md:313`（§1.13b）仍保留 round-7 的两处过时描述："UPDATE receipt only" + zero-video/no-witness 暗示。 | §1.13b 加 ⚠️ superseded 标记，原文用 ~~strikethrough~~，给出 round-8/9 准确版本指针（withdraw UPDATE receipt+op consent-lifecycle 但不动 lease；唯一 c2 non-selectability 因由是 active lease，无 video 非独立因由）。 |

**round-9 改动清单**（纯 doc，零 runtime/SQL/test）：
- `src/lecturecast/operation_repository.py` `count_recovery_attention` docstring c2 auto-resolve 段（~3180）。
- `src/lecturecast/operation_repository.py` unrecoverable COUNT 上方 SQL COMMENT c2 段（~3274）。
- `docs/e5cd-design.md` §1.13c 表第 3 行（"(expired/fenced/released)" → fenced-path NULL 描述 + round-9 注）。
- `docs/e5cd-design.md` §1.13b（line 313）⚠️ superseded 标记。

**元教训（#round-9，强化 round-8 #2）**："先查已有参考别试错"对 doc 同样适用，且每次 doc 修正后必须**grep 全仓的同义表述**（不只 Codex 点名的那个 site）——round-8 只修了 docstring/SQL COMMENT/"NOT counted"段，漏了 §1.13b 那段历史记录里仍嵌着 round-7 的过时描述；round-8 prompt 自己也写了 "expired" 这个不准确的词。同 c3 round-12/13 的"原则陈述正确 ≠ 实现穷举"模式：doc 逐 site 验，不凭原则直觉假设"那一段是历史记录所以不影响"。

**待 Codex round-9 锁定复审**（effort=low，invariant-completeness/doc-accuracy framing：两 blocker 是否真闭合 —— (1) "fenced recovery/apply/finalize/release path NULLs lease columns" 是否 against grep 命中的清 lease 原语（~1652/1702/1718/1959/1972）准确，单纯时间 expiry 是否真不通过 branch B gate；(2) §1.13b superseded 标记 + §1.13c 第 3 行是否消除了所有过时表述；残留猎：grep 全仓是否还有 "expired 单独通过 gate" / "只 UPDATE receipt" / "无 video 即无 witness" 同义残留；纯 doc，零 runtime/SQL/test 变化，1169 全绿不变）。

### §1.13e round-10（Codex round-9 NOT LOCKABLE → 全闭合，待 Codex round-10 锁定复审）

Codex round-9 裁定 **NOT LOCKABLE**，1 个纯 doc-only blocker（items 1–5 全部 CONFIRMED accurate —— auto-resolve、SQL COMMENT、§1.13b superseded、§1.13c row 3、三 claim 全仓猎无残留；item 7 CONFIRMED doc-only，1169 测试计数不变，sandbox 里 10 个 fail 纯属 loopback `HTTPServer.bind()` 网络禁，非代码漂移）。唯一 blocker 是 **item 6 内部一致性**：5 处把 branch B 简写成 **owner-only gate** `o.lease_owner IS NULL`（漏 `AND o.lease_expires_at IS NULL` 合取项）。

| # | site | round-10 修正 |
|---|------|---------------|
| 1 | `operation_repository.py:3147`（docstring c intro）| `branch B's ``o.lease_owner IS NULL`` gate` → `branch B's ``o.lease_owner IS NULL AND o.lease_expires_at IS NULL`` gate` |
| 2 | `operation_repository.py:3172`（docstring c2 sub-bullet）| 同上合取化 |
| 3 | `operation_repository.py:3271`（SQL COMMENT c2）| 同上合取化 |
| 4 | `e5cd-design.md:331`（§1.13c row 2，2 处）| 两处 `o.lease_owner IS NULL` gate 均合取化 |
| 5 | `DIGITAL-HUMAN-PROGRESS.md:83`（handoff round-8 closure (b)）| 合取化 |

against source 核实：branch B 真实谓词 @`operation_repository.py:4065` = `(o.lease_owner IS NULL AND o.lease_expires_at IS NULL`（合取，双列；非时间比较）。grep 全仓 `lease_owner IS NULL` 不跟 `AND`：**规范性 branch-B 描述零 owner-only 残余**；其余命中（本节 before→after 表的"前"文本 / round-9 blocker 历史记录 / 进度文档历史段）均为**明确标记的历史/错误语境**，非当前规范描述（round-11 修：round-10 原写 "零命中（exit 1）" 自指不准 —— 本节自身的 before-text 就会产生命中）。auto-resolve 段（docstring @~3180、SQL COMMENT @~3277）round-9 已是合取；`e5b0c3c-c3-design.md:295`（SQL 常量原文）+ `e5cd-design.md:313`（§1.13b accurate 版本）本就合取。round-10 改完后全仓**规范性**描述 branch B gate 的 site **全部** 合取一致。

**元教训（#round-10，#3 次同一模式）**：这是 round-8→9 模式的**第 3 次重复** —— round-7 凭直觉写 c2 三处不准（round-8 修）；round-8 修了 auto-resolve 但 prompt 自己写 "expired" + 漏 §1.13b 历史段（round-9 修）；round-9 修了 auto-resolve 合取但漏了 c2 intro/sub-bullet/SQL-COMMENT 的 owner-only 简写（round-10 修）。**根因**：每次修 doc 时只盯 Codex 点名的那个 claim 的"主表述段"，没意识到**同一个谓词会在 docstring 的 intro、sub-bullet、auto-resolve 三个位置分别表述一次**，每处都需独立合取化。同 c3 "原则陈述正确 ≠ 实现穷举" 的 doc 版：**一个谓词在 N 处表述，修 1 处 ≠ 修 N 处**。修正动作：grep 全仓该谓词的每一种简写/全写形态，逐处核对。

**待 Codex round-10 锁定复审**（effort=low，invariant-completeness/doc-accuracy framing：(1) 5 site 是否全合取化、against @4065 真实谓词准确；(2) grep 全仓是否还有 owner-only 简写残留（含 maintenance.py / consent.py / 其他 design doc）；(3) 合取化是否引入新内部不一致（如某处合取项笔误）；(4) 纯 doc，零 runtime/SQL/test 变化，1169 全绿不变）。

### §1.13f round-11（Codex round-10 NOT LOCKABLE → 全闭合，待 Codex round-11 锁定复审）

Codex round-10 裁定 **NOT LOCKABLE**，1 个纯 doc-only blocker（items 1–6 + 8 全 CONFIRMED accurate —— 5 site 全合取化、全仓猎无规范性 owner-only 残留、doc-only/1169 不变）。唯一 blocker（item 7）：§1.13e 写 "grep 全仓 `lease_owner IS NULL` 不跟 `AND` 的残余 —— 修正后 src/ + docs/ **零命中**（exit 1）" —— 这句话**自指不准**：本节自身的 before→after 表 "前" 文本、round-9 blocker 历史记录、进度文档历史段都含 owner-only 串，会产生 grep 命中。**实质结论（零规范性残留）正确，但 "零命中（exit 1）" 字面 claim 错。**

round-11 修正（3 site，纯 doc）：
- `docs/e5cd-design.md` §1.13e（@~372）："零命中（exit 1）" → "规范性 branch-B 描述零 owner-only 残余；其余命中均为明确标记的历史/错误语境"。
- `docs/DIGITAL-HUMAN-PROGRESS.md:83`（handoff round-10 闭合段）：同改（Codex 只点了 §1.13e，但同一 false claim 也在 PROGRESS:83 —— 按元教训 grep 全仓同义表述，三处一起改）。
- `docs/DIGITAL-HUMAN-PROGRESS.md:72`（"还没做" round-10 闭合括号）：原 "grep 全仓零 owner-only 残留"（Codex round-10 把 :72 归为历史 round-9 blocker 记录、未当 blocker 点名；"残留" 非 "命中" 但同族 phrasing）→ 加 "规范性描述" 限定 + "历史/before-text 命中除外"。（round-12 修：本节原误记 "2 site" 漏列 PROGRESS:72，Codex round-11 item 5 抓；见 §1.13g。）

**元教训（#round-11，#4 次同一模式 + 新变体）**：前三次是"修谓词漏同义 site"；这次是**自指陷阱** —— 一条描述 grep 结果的陈述，其本身的成立条件会被同节内的历史/before 文本破坏。写 "grep 零命中" 这类**关于自身文档的元陈述**时，必须把本节将引入的 before/历史文本计入命中集，否则自相矛盾。同 c3 "原则陈述正确 ≠ 实现穷举"：**关于文档的元 claim 也要 against 文档实际内容核实**。

**待 Codex round-11 锁定复审**（effort=low，invariant-completeness/doc-accuracy framing：(1) §1.13e + PROGRESS:83 的 "零命中" 是否改为准确的 "零规范性残留 + 历史命中标注"；(2) 改后是否引入新不准（如把 "规范性" 误写成全量）；(3) 全仓是否还有其他 "零命中/zero hits/exit 1" 类自指元 claim（含 round-9 §1.13d 的 prompt、round-8 历史段）；(4) 纯 doc，零 runtime/SQL/test 变化，1169 全绿不变）。

### §1.13g round-12（Codex round-11 NOT LOCKABLE → 全闭合，待 Codex round-12 锁定复审）

Codex round-11 裁定 **NOT LOCKABLE**，1 个纯 doc-only blocker（items 1–4 + 6 全 CONFIRMED accurate —— §1.13e/PROGRESS:83/PROGRESS:72 三处 "零命中" 已改为准确的 "零规范性残留 + 历史命中标注"、全仓猎无其他同类自指元 claim、doc-only/1169 不变）。唯一 blocker（item 5）：§1.13f 记录段写 "round-11 修正（2 site，纯 doc）" 但 commit `4c1c4fd` 实际改了 **3 site**（§1.13e + PROGRESS:83 + PROGRESS:72）—— PROGRESS handoff（@83）正确写 "三处"，§1.13f 却写 "2 site"，内部不一致。

round-12 修正（in-place 改 §1.13f site-count + 本 §1.13g 记录段，纯 doc）：
- §1.13f 本节（@~382）："round-11 修正（2 site，纯 doc）" → "round-11 修正（3 site，纯 doc）"；补第三 bullet 记 `PROGRESS:72`（原 "零残留"→加 "规范性描述" 限定 + "历史/before-text 命中除外"，"残留" 非 "命中"，Codex round-10 归为历史记录未当 blocker 但属同族）；第二 bullet 末 "两处一起改" → "三处一起改"。

**元教训（#round-12，#5 次同一模式 + 又一变体）**：round-11 的自指陷阱是 "grep 结果元 claim"；这次的变体是**记录段自身的 site-count vs commit diff 对不上** —— 写 "N site" 这类关于上轮改动的元 claim 时，必须 against `git show <commit> --stat` 实际 diff 核实，不能凭上轮记忆里"Codex 点名了几个"来记（PROGRESS handoff 写 "三处" 正确，恰恰因为它是逐文件列的；§1.13f 写 "2 site" 错，因为它是凭 Codex round-10 只点了 §1.13e 的印象）。同 c3 "原则陈述正确 ≠ 实现穷举" 的 doc 元层版：**关于文档自身改动历史的元 claim，也要 against commit diff 实际核实**。

**待 Codex round-12 锁定复审**（effort=low，invariant-completeness/doc-accuracy framing：(1) §1.13f site-count 现是否准确（3 site + 3 bullet，against commit `4c1c4fd` diff）；(2) 第二 bullet "三处一起改"、第三 bullet PROGRESS:72 描述是否准确无新不准；(3) 全仓是否还有其他 "N site/N 处" 类记录段与对应 commit diff 对不上的不一致（含 §1.13d/§1.13e 的 site-count）；(4) 纯 doc，零 runtime/SQL/test 变化，1169 全绿不变）。

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
