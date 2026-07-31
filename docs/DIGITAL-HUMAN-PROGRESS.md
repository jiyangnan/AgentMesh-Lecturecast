# Digital-Human Edition — 实现进度

> 技术规格 v1.4（`lecturecast-server/docs/DIGITAL-HUMAN-TECH-SPEC.md`）
> 三仓库：lecturecast-server / agentmesh-core / AgentMesh-Lecturecast
> Codex 审阅驱动：每个子步骤写完 → 发 Codex 审 → 改完再发 → 锁定后进下一块

## lecturecast-server（服务端）— §5.1–5.3 全部完成

- §5.1 canonical boundary 文档
- §5.2 v1.0/v1.1 双版本 Pydantic 模型 + protocol 导出（v1.0 冻结）
- §5.3.1–10d 数据库迁移 + 产物提交管线 + 摘要链 + 恢复拓扑 + 里程碑充电状态机 + 租约仓库 + Core 扣费适配器 + V1.1 生成视图 + resume 协调器 + HTTP resume 路由 + ChargeProcessor + charge recovery + refund + maintenance 调度

服务端 530+ 测试全绿。分支 `feat/digital-human-edition`。

## agentmesh-core — §5.4 完成

3 个里程碑 ProductAction（manifest / presenter_plan / orchestration 各 10）。分支 `feat/digital-human-milestone-actions`。

## AgentMesh-Lecturecast（客户端）— §5.5

### 已完成并锁定

| 子步 | 内容 | 状态 |
|------|------|------|
| §5.5a | v1.1 协议 bundle + 版本协商 + 双版本解析 + 版本感知 Director 响应 | ✅ |
| §5.5b | 能力采集（F5/HeyGen 默认 fail-closed） | ✅ |
| §5.5c | 定价预估验证器 + 余额门禁移除 + 工作流投影 | ✅ |
| §5.5d0–d4 | ManifestGenerationOutV1_1 + generation parser + resume + state schema 1.2 + CLI + 错误映射 | ✅ |
| §5.5e1 | journal SQLite（四表 + WAL/FK/权限/symlink 拒绝 + v1→v4 migration） | ✅ |
| §5.5e2a | consent 纯模型（disclosure/identity/prepare_operation 128-bit） | ✅ |
| §5.5e2b | record_decision 原子落库 + 幂等/conflict/integrity | ✅ |
| §5.5e2c | withdraw + validate_submit_consent guard（全链重算 + PresenterPlan） | ✅ |
| §5.5e3a | OperationRepository claim/lease/fence + SubmitCoordinator | ✅ |
| §5.5e3b | submit outcome 状态机 + adapter domain types + video resource | ✅ |
| §5.5e3c | known-ID poll processor + anti-hotloop + exclusive ownership | ✅ |
| §5.5e3d1 | title reconciliation 候选发现 + claim | ✅ |
| §5.5e3d2 | title 判决三态 + 崩溃恢复 + cancellation topology | ✅ |
| §5.5e4a1 | download claim/lease/fence + journal v3 + 守卫 + 崩溃恢复 | ✅ |
| §5.5e4a2 | 两阶段 download processor（stage→finalize + URL re-poll + 严格校验 + consent withdrawal cleanup） | ✅ |
| §5.5e4b | per-resource 删除生命周期（journal v4 + DeleteResult 封闭 + per-reason 门禁 + 完整拓扑重验） | ✅ |
| §5.5e5a | StdlibVideoDownloader + FfprobeMediaProbe（HTTPS-only/DNS pin/private HTTPS/流式 sha256/no-redirect/lexical containment/reader-thread timeout） | ✅ |
| §5.5e5b0a | HeyGenHttpTransport（host 锁 api.heygen.com/v3-only path/API key fresh/ProxyHandler/no-redirect/1MiB cap/headers whitelist/error body oversized guard） | ✅ |

| §5.5e5b0b | asset upload adapter + 安全 multipart（digest re-verify/streaming iterator/shared path helper/forged command/error matrix/MIME binding） | ✅ |
| §5.5e5b1 | HeyGen Videos v3 adapter（封闭 descriptor + 响应资源绑定 + fail-closed reconcile + token cycle 检测 + canonical title 校验；4 轮 Codex 审阅后锁定） | ✅ |
| §5.5e5b0c1 | asset journal v5 表 + consent guard + repo 原语（canonical 身份派生 + fenced lease/apply + 冻结 24h deadline + 崩溃超窗防重传 + 完整 resource 拓扑校验 + v5 迁移契约；5 轮 Codex 审阅后锁定） | ✅ |
| §5.5e5b0c2 | AssetUploadProcessor（guard+claim 同 tx → adapter 事务外 → fenced apply；claim 状态原样转发 + result-vs-command 校验 + 确定性双 worker/崩溃超窗契约；3 轮 Codex 审阅后锁定） | ✅ |
| §5.5e5b0c3a | asset GET/DELETE adapter（GET 验存在+id/type 无 digest；DELETE 验回显 data.id；独立 AssetReadError + AssetProbeResult 严格不变量；2 轮 Codex 审阅后锁定） | ✅ |
| §5.5e5b0c3b | asset 删除 repo + withdrawal/in-flight-upload 竞态闭合（journal v6 + fenced-apply consent 重检 + AssetApplyOutcome + enqueue_consent_withdrawal_cleanup + withdraw 接线 + recover maintenance；deletion_reason/error_code 矩阵 + enqueue fail-closed 边界 + _mark_asset_cleanup 四 guard；6 轮 Codex 审阅后锁定） | ✅ |
| §5.5e5b0c3c-c1 | asset deletion fenced claim/apply 原语 + AssetDeletionProcessor.delete_once（asset 自身 lease fence；claim 同 tx 翻 uploaded→cleanup_required + not_started→deletion_pending(post_download)；apply CAS 绑 resource_id + expected_remote_id + 矩阵 + reason gate；manual_force 不自动删；4 轮 Codex 审阅后锁定） | ✅ |

## §6 跨仓 contract tests（commit bcec6f3，待 Codex 复审）

`tests/test_cross_repo_contracts.py`，40 条，验证三仓磁盘真实产物的一致性：

| §6 条目 | 测试 | 覆盖 |
|---------|------|------|
| #2 protocol export↔client lock | `TestProtocolLockCrossRepoIdentity` + `TestVendoredLockSelfConsistency` | server v1.0/v1.1 lock 与 client vendored lock 逐字节一致；每文件 bytes 一致；bundle_digest 重算防篡改 | 
| #1 Core registry↔milestone 收费 | `TestCoreRegistryMilestoneCostContract` | 3 action 各 10；registry==server `MILESTONE_LOCKED_COST`==`app/products.py` |
| #11 verified 不上传 | `TestVerifiedNeverUploaded` | 递归扫所有 schema 无 `verified` 字段 |
| #12 resource FK SET NULL | `TestHeyGenJournalContracts` | `created_by_operation_id` 用 ON DELETE SET NULL |
| #15 completed≠verified | `TestHeyGenJournalContracts` | download_status CHECK：downloaded≠verified，bogus 被拒 |

**测试抓到的真实漂移（已修）**：client v1.0 error-envelope 缺 `unsupported_protocol`（server `84dd8e5` 加的加性 code，export.py 明确允许 ErrorEnvelope 加性演化）。用 `update_protocol` 从 server canonical 重新 vendor → 两边 bundle_digest 一致 `f0e73b41`。

**§6 剩余条目的归属**：#5/6/7/8/13（digest 链/里程碑适用性/awaiting_credits/external_id/deducted）是 server 内部行为，由 server 自身 530+ 测试覆盖，非跨仓契约；#14 RecoveryDirectiveCatalog 依赖未建的 e6；#9 定价下发、#10 M1 门禁独立待 Codex 判定是否本轮补。

### 还没做

- §5.5e5b0c3c-c2 消费门禁 resolver（给定 op，按 §3.5 返回有序可删 resource 列表 + force 模式：正常 video verified→deleted→audio→portrait；force 跳过 video；reusable_avatar 的 portrait 跳过）
- §5.5e5b0c3c-c3 正常顺序 coordinator（c2 决策 × c1/video processor）+ maintenance 恢复接线（recover 调度入口 + deletion 恢复 pass）
- §5.5e5c/d capability wiring + doctor/canary
- §5.5e6 RecoveryDirectiveCatalog 验签 + failure mapping + 宿主 workflow（§6 #14 依赖它）
- §6 收尾：补 #9 定价下发 / #10 M1 门禁跨仓契约 + #14（依赖 e6）+ 三仓 CI gate

客户端 **896 测试全绿**。分支 `feat/digital-human-protocol-v1_1`。

---

## 下次会话接续点（交接）—— e5b0c3c-c1 已锁定，c2 起步

**当前状态**：e5b0c3c-c1 已锁定（4 轮 Codex 审阅后锁定，commit `333b6e7`+`a3c`，896 测试全绿）。审阅轨迹：round-1（2 blocker：apply 未绑 resource_id + claim/apply 未调矩阵）→ round-2（remote_id 篡改：apply 未绑远端身份）→ round-3（必填 kwarg 仍可显式传 None）→ round-4（可锁）。

### c1 已落地的关键不变量（改动时不能放松）

- **claim 同 tx 自洽**：claim 在一个 tx 内翻 asset `uploaded→cleanup_required` + resource `not_started→deletion_pending(post_download)`，矩阵始终自洽；claim 在任何 eligibility 分支前调 `_check_asset_resource_consistency`（非法起点 fail-closed，不静默修正）。
- **apply 三重身份绑定**：lease CAS 绑 `remote_resource_id=resource_id`（本地行身份）；`_validate_asset_binding(expected_remote_id=...)` 绑远端身份（claim/apply 间改名 remote_id 被拒）；`expected_remote_id` 必填 + 运行时 regex 守卫（防显式传 None 绕过）。所有维度（resource_id / remote_id / kind / created_by / credential / retention / refs / deletion_status / reason / manual-force marker）在 claim/apply 两 tx 间都被重验。
- **矩阵 + reason gate**：apply 落库前重读 resource 全状态调矩阵 + reason allowlist（只 post_download/consent_withdrawal；manual_force 永不自动删、永不清 integrity marker）。
- **幂等 + 崩溃安全**：claim(tx1)→adapter.DELETE(tx 外，AssetReadError 捕获为 outcome)→apply(tx2)；200/404→deleted；retryable→deletion_failed+backoff；耗尽/永久→terminal manual code；非 AssetReadError 异常传播留 lease 到期恢复。

### c1 审阅中暴露的系统性教训（适用于 c2/c3）

每轮 Codex 都找到一个**同类的身份/契约绕过**，模式高度一致——值得记取：
1. **round-1**：apply 信任了入参但没把它绑进 CAS（resource_id 被忽略）；"必填"原语跳过了已锁的矩阵。
2. **round-2**：CAS 绑了本地行身份（resource_id）却漏了远端身份（remote_id）。
3. **round-3**：必填 kwarg 只防"省略"不防"显式传 None"，而 None 在下游语义是"跳过"。
→ **c2/c3 写任何 fencing/门禁原语时，每个身份维度都要：(a) 绑进 CAS 或显式校验，(b) 运行时拒 None/空/非法，(c) 复用已锁的不变量而不是绕过，(d) 写直接篡改测试而非只测 happy path。**

### e5b0c3c-c2 范围（下一步）

消费门禁 resolver：`resolve_deletion_plan(conn, operation_id, *, force=False) → 有序可删 resource 列表`。
- 正常模式（§3.5）：video（需 download_status=verified）→ audio_asset → portrait_asset（reusable_avatar 的 portrait 跳过，retention）。
- force 模式：跳过 video 阶段（用户主动/隐私紧急/video 持续下载失败）。
- 进 c2 前先写盲预测 + 设计稿，再发 Codex round-1（**建议新开 Codex session**，019fb840 已跑 4 轮上下文较长）。

### 恢复操作（下次会话）

```bash
cd ~/AgentMesh-Lecturecast
git log --oneline -6   # 最近：333b6e7 round-3 None 守卫（c1 锁定）
UV_CACHE_DIR=/tmp/lc-uv-cache uv run --project . pytest tests/ -c pyproject.toml -q   # 应 896 passed
```

Codex e5b0c3c-c1 会话（4 轮）: `019fb840-a93b-73e1-b56c-a29b07a15e3d`；c2 建议开新 session。发审命令同前（`codex exec resume <session> "<prompt>" -c 'model_reasoning_effort="low"' --json`，**务必 effort=low**，medium 在新 session 会挂）。

### 关键不变量（已落地的安全边界，改动时不能放松）

- **canonical 身份**：`upload_id` / `idempotency_key` 由 `derive_asset_identity(parent, role, digest)` 单源派生，adapter 与 repository 共用，claim 前 INSERT 重派生拒伪造。
- **fenced lease/apply**：两个 apply 方法都带 `lease_owner + expected_fence` CAS（`status='uploading' AND lease_owner=? AND lease_fence=? AND attempt_started_at IS NOT NULL`），rowcount!=1 → fence conflict。
- **冻结 24h 窗口**：`maybe_sent_at` / `idempotency_expires_at` 锚定首次发送，reclaim 用 COALESCE 不覆盖；连续 crash 不延长窗口；过窗 → `manual_reconciliation_required`（绝不盲重传 multipart）。
- **apply 时 consent 重检**：fenced-apply 同事务调 `_validate_existing_integrity`——withdrawn_at / JSON / binding 异常**全部包装成 `ConsentIntegrityError`**，绝不泄漏 raw `ValueError`/`JSONDecodeError` 绕过 `except ConsentError` 回滚事务丢 remote asset（round-4 #1）；granted→uploaded，withdrawn→cleanup_required/consent_withdrawal，declined/missing/corrupt→cleanup_required/manual_force + `consent_integrity_failure`。remote asset 永远记录不失踪。
- **严格 asset↔resource 矩阵**：`_check_asset_resource_consistency`（uploaded↔not_started，cleanup_required↔deletion_pending/failed，deleted↔deleted）+ deletion_reason 矩阵（`manual_force` 在**所有** deletion state 都要求 `last_error_code=consent_integrity_failure`——resource row 的 deletion_reason 是 generic cause marker、不持久编码该 cause，asset error code 是唯一 durable marker；round-4 #2）。
- **_mark_asset_cleanup 四 fail-closed guard**：拒绝 resurrect deleted resource / touch foreign resource / missing parent / missing-or-shared `heygen_resource_operation_refs`，每条都有直接回归（`TestMarkAssetCleanupGuards` 5 测，失败时 asset+resource 双表不变；round-4 #3 + round-5）。
- **enqueue fail-closed**：未知 asset status 不落入 catch-all `kept`（只显式允许 `manual_reconciliation_required`）；half-lease 拒绝；写库用 `_canonical(now)`（round-3 #3）。
- **resource_kind / retention / credential 派生**：`apply_asset_outcome` 从父 op 读 credential、从 role 派生 kind/retention（不接受 caller 传）；UNIQUE 冲突 → `OperationIntegrityError` 不泄漏裸 sqlite3。
- **journal v6 rebuild**：`_migrate_v5_to_v6` 表 rebuild（CREATE _new→INSERT SELECT→DROP→RENAME），asset_uploads 无入站 FK 故安全；幂等（双字符串形态检测）；整事务回滚。fresh install 直接用最新 DDL 不 double-rebuild。

### Codex 审阅工作流

每个子步骤：
1. 实现 + 测试（全绿）
2. `codex exec resume <session>` 发 Codex 审
3. 按 Codex 反馈修改 → 再发 → 直到 Codex 说"可锁"
4. 锁定后进下一块

Codex 会话 ID（e5b0c3b，6 轮）: `019fa2e9-0a36-7f50-ab1b-0e223a366540`；e5b0c3c 建议开新 session。

