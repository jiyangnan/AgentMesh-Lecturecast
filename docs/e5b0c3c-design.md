# §5.5e5b0c3c 设计稿 + 盲预测（待 Codex 设计审）

> 正常顺序 coordinator + maintenance 恢复接线 + 消费门禁 resolver + asset 网络 DELETE 编排
> 规格依据：tech spec §3.4（resource 生命周期）、§3.5（删除策略：正常批量顺序 + force-cleanup）
> 前置：e5b0c3b 已锁定（6 轮 Codex），850 测试绿。

## 1. 现状（已落地、可复用的原语）

| 原语 | 位置 | 说明 |
|------|------|------|
| video deletion fenced claim/apply | `claim_deletion_in_tx` / `apply_deletion_outcome_in_tx` | **video-only**（SQL 硬编码 `resource_kind='video'`），用 **operation lease**（`heygen_operations.lease_*`）做 fence CAS |
| `DeleteProcessor.delete_once` | operation_repository:3007 | video 删除 processor：claim→`deleter.delete_video`(tx 外)→fenced apply |
| asset upload fenced claim/apply | `claim_asset_upload_in_tx` / `apply_asset_outcome_in_tx` | 用 **asset 自身 fence**（`heygen_asset_uploads.lease_fence`） |
| `AssetUploadProcessor.upload_once` | operation_repository:2660 | asset 上传 processor |
| consent-withdrawal cleanup enqueue | `enqueue_consent_withdrawal_cleanup_in_tx` + `recover_withdrawn_asset_cleanups` | receipt withdrawn → asset cleanup_required + resource deletion_pending/consent_withdrawal；recover 已是 maintenance 入口 |
| asset GET/DELETE adapter | `heygen_asset_adapter.get_asset` / `delete_asset` | DELETE 200→`AssetDeleteResult(deleted)`，404→`already_absent`；其余→`AssetReadError(code, retryable)` |
| `_check_asset_resource_consistency` | 状态矩阵 uploaded↔not_started / cleanup_required↔deletion_pending\|failed / deleted↔deleted + deletion_reason 矩阵 |
| `_validate_asset_binding` | resource topology（kind/created_by/credential/retention + 单一 ref）|

**缺**（e5b0c3c 要建）：
1. asset deletion fenced claim/apply 原语（asset fence，**不复用** video 的 operation-lease 路径）
2. `AssetDeletionProcessor.delete_once`（镜像 `DeleteProcessor`）
3. 消费门禁 resolver（§3.5 顺序 + force 的 eligibility）
4. 正常顺序 coordinator（编排 video→audio→portrait 删除）
5. maintenance 接线（recover 的调度入口 + deletion 恢复 pass）

## 2. 拆分建议（每子步：实现+测试→Codex 审→锁→下一块）

- **c1**：asset deletion fenced claim/apply 原语 + `AssetDeletionProcessor.delete_once` + 单元/回归测试
- **c2**：消费门禁 resolver（给定 op，按 §3.5 返回有序可删 resource 列表 + force 模式）
- **c3**：正常顺序 coordinator（c2 决策 × c1/video processor）+ maintenance 恢复接线

## 3. c1 详细设计

### 3.1 状态矩阵自洽性（关键决策）

`_check_asset_resource_consistency` 已锁的矩阵：`uploaded↔not_started`、`cleanup_required↔deletion_pending|failed`、`deleted↔deleted`。

正常 post-download 路径起点是 `asset=uploaded, resource=not_started`。一旦开始删除，resource 要进 `deletion_pending`——但 `uploaded↔deletion_pending` **不匹配**。所以：

**claim 时同步把 asset `uploaded→cleanup_required`**（进入待删语义），resource `not_started→deletion_pending(reason=post_download)`。这样矩阵始终自洽：

| 阶段 | asset status | resource deletion_status | 矩阵 |
|------|--------------|--------------------------|------|
| 起点（granted 上传完） | uploaded | not_started | ✓ |
| claim 后 | cleanup_required | deletion_pending | ✓ |
| apply 成功 | deleted | deleted | ✓ |
| apply 失败（可重试） | cleanup_required | deletion_failed | ✓ |

> 对比 video：video deletion 不改 `heygen_operations.status`（operation 有独立生命周期），只动 resource。asset 不同——`heygen_asset_uploads` 的 status 字段本身就是删除生命周期载体，必须随 resource 同步。

### 3.2 `claim_asset_deletion_in_tx`

```
claim_asset_deletion_in_tx(conn, *, upload_id, lease_owner, now_iso, lease_seconds,
                           max_attempts=DELETION_MAX_ATTEMPTS) -> AssetDeletionClaim
```

- fence = `asset.lease_fence + 1`（upload apply 不清 lease_fence，复用）
- gate（任一不满足 → 返回 not_ready/busy，不抛）：
  - asset 存在 + status ∈ {uploaded, cleanup_required}（uploading/upload_pending/failed/cancelled/manual_reconciliation_required/deleted 都不删）
  - resource 存在 + deletion_status ∈ {not_started, deletion_pending, deletion_failed(retry 条件)}
  - resource `deleted` → not_ready（绝不 resurrect）
  - `_validate_asset_binding`（kind/created_by/credential/retention + 单一 ref）→ foreign/missing ref 拒
  - half-lease（owner XOR expires）→ `OperationIntegrityError`
  - active lease（owner 非空且未过期）→ busy
- eligibility by reason（镜像 video claim_deletion line 1654-1700）：
  - `deletion_failed` + `last_deletion_error ∈ _DELETION_MANUAL_CODES` → not_ready
  - `deletion_failed` + attempts ≥ max → not_ready
  - `deletion_failed` + next_retry > now → retry_wait
  - `manual_force` reason → not_ready（integrity 路径产物，不自动删，留人工）—— **待 Codex 确认**：manual_force 资源是否真不自动删，还是按 last_error_code 走？
- 写入（gated UPDATE，rowcount 校验）：
  - `heygen_asset_uploads SET status='cleanup_required', lease_owner=?, lease_expires_at=?, lease_fence=fence+1, attempt_started_at=?, next_retry_at=NULL, updated_at=?`（uploaded→cleanup_required；已 cleanup_required 则只设 lease）
  - `heygen_remote_resources SET deletion_status='deletion_pending', deletion_reason=<post_download if was not_started else inherit>, deletion_attempts=+1`
- 返回 `AssetDeletionClaim(upload_id, resource_id, status, fence, remote_id, asset_role)`

### 3.3 `apply_asset_deletion_outcome_in_tx`

```
apply_asset_deletion_outcome_in_tx(conn, *, upload_id, lease_owner, fence, now_iso,
                                   result, max_attempts=...) -> AssetDeletionOutcome
```

- CAS on `lease_owner=? AND lease_fence=? AND status='cleanup_required'`（rowcount 0 → fence_conflict）
- **重验 topology**（claim 与 apply 是两个 tx）：`_validate_asset_binding` + resource deletion_status='deletion_pending'
- idempotent replay（asset 已 deleted）：重算 consistency，返回真实 outcome
- 结果映射：
  - `AssetDeleteResult(deleted|already_absent)` → resource `deletion_status='deleted', deleted_at=now` + asset `status='deleted'`，清 lease；返回 deleted
    - **already_absent 也算 deleted**（spec §3.5：200/404→deleted，幂等）
  - `AssetReadError` + retryable + attempts < max → resource `deletion_failed, last_deletion_error=code, deletion_next_retry_at=now+backoff`；asset 保持 cleanup_required；清 lease；返回 failed(retry)
  - `AssetReadError` 不可重试 / attempts 耗尽 → resource `deletion_failed, last_deletion_error=deletion_retry_exhausted|deletion_reconciliation_required, next_retry=NULL`；asset 保持 cleanup_required；返回 failed(terminal)
- 所有 resource UPDATE 都带 gate `deletion_status='deletion_pending'`（防 race）

### 3.4 `AssetDeletionProcessor.delete_once`（镜像 DeleteProcessor）

```
delete_once(*, upload_id, lease_owner, adapter, now_iso, lease_seconds) -> AssetDeletionOnceResult
  claim = claim_asset_deletion_in_tx(...)  # tx1
  if claim.status != "claimed": return ...
  try: result = adapter.delete_asset(claim.remote_id)       # tx 外
  except AssetReadError as exc: result = exc
  outcome = apply_asset_deletion_outcome_in_tx(..., result)  # tx2
  return AssetDeletionOnceResult(claim, outcome)
```

> 注意：asset adapter `delete_asset` 失败抛 `AssetReadError`（**非** `DeleteAdapterError`，非 `AssetUploadError`）—— 不同于 video 的 `DeleteAdapterError`。

## 4. c2/c3 概要（c1 锁后细化）

- **c2 resolver**：`resolve_deletion_plan(conn, operation_id, *, force=False) → ordered list of (resource_id, kind, processor)`。正常：video(需 download_status=verified) → audio_asset → portrait_asset；force：跳过 video。reusable_avatar 的 portrait 跳过（retention）。门禁 = §3.5。
- **c3 coordinator**：遍历 c2 plan，video 走 `DeleteProcessor`，asset 走 `AssetDeletionProcessor`；maintenance 把 `recover_withdrawn_asset_cleanups` + 新 `recover_asset_deletions` 接到调度入口（CLI/scheduler 留 e5c/d）。

## 5. 盲预测（会踩的坑 / 待确认）

1. **manual_force reason 是否自动删？** apply integrity 路径把 resource 标 deletion_pending/manual_force + asset cleanup_required。video claim_deletion 对 manual_force 返回 not_ready（不自动删，留人工）。asset 应镜像。但 spec §3.5 force-cleanup 是"用户主动"——manual_force 算 force？**待 Codex 确认**：asset manual_force 资源走人工还是 force-cleanup 自动删。
2. **already_absent 的完整性含义**：404 幂等成功，但若 resource 之前是 not_started（从未删过）却 404，说明远端状态与本地不一致。spec 说 200/404→deleted。倾向于幂等成功，但不静默——是否记 last_deletion_error？**待确认**。
3. **fence 单调性**：upload(fence=1) → apply(保留 1) → delete claim(2) → apply(保留 2)。若 delete 失败后 reclaim，fence=3。每次 claim +1，单调。但若中间又 upload（不可能，uploaded 后不重传）。OK。
4. **deletion_attempts 列**：resource 表有 `deletion_attempts`（video 用）。asset resource 共用同表，复用此列。✓
5. **coordinator 原子性**：coordinator 串行删多个 resource，每个独立 claim/apply（各自 tx）。整体非原子——video 删成功但 audio 失败时，video 已删，audio deletion_failed 留 retry。这是 spec 允许的（逐资源删除）。✓
6. **正常模式 audio 门禁**：是否必须等 video deleted？spec 说"固定顺序 video→audio→portrait"。若 video 删除 deletion_failed 卡住，audio 永远等 → 需要 force fallback 或降级。**待 Codex 确认**：正常模式 audio/portrait 门禁是"video 已 deleted"还是"video 删除已 attempt（无论成败）"。

## 6. 关键不变量（c1 不能放松）

- **绝不 resurrect**：resource `deleted` → claim not_ready；apply UPDATE gate `deletion_status='deletion_pending'`
- **topology 重验**：claim 与 apply 是两个 tx，apply 必须重验 `_validate_asset_binding`（防 foreign resource 跨 tx 篡改）
- **fence CAS**：apply 必须带 `lease_owner + expected_fence`，rowcount 0 → fence_conflict（不盲覆盖）
- **状态矩阵自洽**：任何写操作后 asset↔resource 满足 `_check_asset_resource_consistency`
- **adapter 调用在 tx 外**：claim(tx1)→delete(outside)→apply(tx2)，崩溃任一点可恢复
- **remote 操作幂等**：already_absent → deleted；404 不当错误

## 7. 给 Codex 设计审的问题

1. 拆分 c1/c2/c3 合理吗？c1 边界（claim/apply/processor）是否自洽可独立锁？
2. §3.1 状态矩阵自洽性方案（claim 时 uploaded→cleanup_required）正确吗？还是另设 asset 状态？
3. §5.1 manual_force asset 走人工还是 force-cleanup？
4. §5.6 正常模式 audio/portrait 删除门禁：video 已 deleted vs 已 attempt？
5. fence 复用 asset_uploads.lease_fence（upload 后保留）有隐患吗？
