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

- §5.5e5b0c3b asset 删除 repo + withdrawal/in-flight-upload 竞态闭合 —— **进行中（功能完整，Codex round-3 收尾中）**。详见下方「下次会话接续点」。
- §5.5e5b0c3c 正常顺序 coordinator（video verified→deleted→audio→portrait）+ maintenance 恢复接线 + 消费门禁 resolver（receipt granted + asset status=uploaded + resource deletion_status=not_started + 拓扑）+ 网络 DELETE 编排（asset-specific claim/apply，用 asset upload 自身 fence，原子更新 asset/resource）
- §5.5e5c/d capability wiring + doctor/canary
- §5.5e6 RecoveryDirectiveCatalog 验签 + failure mapping + 宿主 workflow（§6 #14 依赖它）
- §6 收尾：补 #9 定价下发 / #10 M1 门禁跨仓契约 + #14（依赖 e6）+ 三仓 CI gate

客户端 **823 测试全绿**。分支 `feat/digital-human-protocol-v1_1`。

---

## 下次会话接续点（交接）—— e5b0c3b 收尾

**当前状态**：e5b0c3b 功能已完整实现（journal v6 + apply fenced-apply consent 重检 + AssetApplyOutcome + enqueue_consent_withdrawal_cleanup + withdraw 接线 + recover maintenance），并已过 Codex round-1（5 blocker）→ round-2（5 blocker 全闭）→ round-3（**#1 已闭并提交 `2251af5`，#2/#3/#4 + v6 修正待做**）。823 测试全绿。

### e5b0c3b Codex round-3 剩余 4 项（必做，做完即可锁 e5b0c3b）

1. **#2 cleanup 拓扑校验 deletion_reason**（`_check_asset_resource_consistency` 旁加 reason 矩阵）：
   - `uploaded / not_started` → `deletion_reason IS NULL`
   - `cleanup_required / deletion_pending|failed` → reason ∈ `{consent_withdrawal, post_download, manual_force}`
   - `manual_force`（integrity 路径）→ asset 必须保留 `last_error_code=consent_integrity_failure`
   - 未知/null reason → `OperationIntegrityError`
2. **#3 enqueue fail-open 边界**（`enqueue_consent_withdrawal_cleanup_in_tx`）：
   - 未知 asset status 不能落入 catch-all `kept`；只显式允许 `manual_reconciliation_required`，其余抛 `OperationIntegrityError`
   - `upload_pending/failed` 的"provably clean"证明要加 `lease_expires_at`（防 half-lease 被误判 cancelled）
   - 写库用 `_canonical(now)`，不写原 `now_iso` 字符串（已 `now=_parse_utc(now_iso)`，只需把 UPDATE 的 now_iso 换成 `_canonical(now)`）
3. **#4 直接回归测试**（新增到 `tests/test_asset_journal.py`）：
   - apply 时 receipt digest/JSON/binding 篡改 → manual cleanup 且 remote asset 已记录
   - granted receipt 误调 enqueue → OperationStateError 拒绝
   - asset/resource 状态矩阵不一致 → OperationIntegrityError
   - resource deletion_reason 缺失/错误 → OperationIntegrityError
   - expired uploading → manual；active lease → left_uploading；half lease → error
   - 未知 asset status → error
4. **v6 UNIQUE 测试假阳性**（`tests/test_asset_journal_v6.py::test_v6_rebuild_preserves_fk_unique_index`）：重复 idempotency_key 用了不存在的 `op_f2`（FK 先拒）。先 INSERT `op_f2` 再验 UNIQUE；并补验两个 outbound FK + `remote_resource_id ON DELETE SET NULL`。

做完上述 4 项 → 发 Codex round-4 → 若"可锁" → e5b0c3b 锁定 → 进 e5b0c3c。

### 恢复操作（下次会话）

```bash
cd ~/AgentMesh-Lecturecast
git log --oneline -8   # 最近：2251af5 round-3 part 1
UV_CACHE_DIR=/tmp/lc-uv-cache uv run --project . pytest tests/ -c pyproject.toml -q   # 应 823 passed
```

发 Codex 审阅（同一 session，含全部历史上下文）：
```bash
codex exec resume 019fa2e9-0a36-7f50-ab1b-0e223a366540 "<round-4 prompt>" -c 'model_reasoning_effort="medium"' --json 2>/dev/null \
  | python3 -c "import json,sys
for l in sys.stdin:
 l=l.strip()
 if l:
  ev=json.loads(l)
  if ev.get('type')=='item.completed' and ev.get('item',{}).get('type')=='agent_message': print(ev['item']['text'])"
```

### 关键不变量（已落地的安全边界，改动时不能放松）

- **canonical 身份**：`upload_id` / `idempotency_key` 由 `derive_asset_identity(parent, role, digest)` 单源派生，adapter 与 repository 共用，claim 前 INSERT 重派生拒伪造。
- **fenced lease/apply**：两个 apply 方法都带 `lease_owner + expected_fence` CAS（`status='uploading' AND lease_owner=? AND lease_fence=? AND attempt_started_at IS NOT NULL`），rowcount!=1 → fence conflict。
- **冻结 24h 窗口**：`maybe_sent_at` / `idempotency_expires_at` 锚定首次发送，reclaim 用 COALESCE 不覆盖；连续 crash 不延长窗口；过窗 → `manual_reconciliation_required`（绝不盲重传 multipart）。
- **apply 时 consent 重检**：fenced-apply 同事务调 `_validate_existing_integrity`（含 JSON 解析异常包装 + withdrawn_at 一致性）；granted→uploaded，withdrawn→cleanup_required/consent_withdrawal，declined/missing/corrupt→cleanup_required/manual_force + `consent_integrity_failure`。remote asset 永远记录不失踪。
- **严格 asset↔resource 矩阵**：`_check_asset_resource_consistency`（uploaded↔not_started，cleanup_required↔deletion_pending/failed，deleted↔deleted）；`_validate_asset_binding` 只验身份拓扑，deletion 状态由矩阵单独验。
- **resource_kind / retention / credential 派生**：`apply_asset_outcome` 从父 op 读 credential、从 role 派生 kind/retention（不接受 caller 传）；UNIQUE 冲突 → `OperationIntegrityError` 不泄漏裸 sqlite3。
- **journal v6 rebuild**：`_migrate_v5_to_v6` 表 rebuild（CREATE _new→INSERT SELECT→DROP→RENAME），asset_uploads 无入站 FK 故安全；幂等（双字符串形态检测）；整事务回滚。fresh install 直接用最新 DDL 不 double-rebuild。

### Codex 审阅工作流

每个子步骤：
1. 实现 + 测试（全绿）
2. `codex exec resume <session>` 发 Codex 审
3. 按 Codex 反馈修改 → 再发 → 直到 Codex 说"可锁"
4. 锁定后进下一块

Codex 会话 ID: `019fa2e9-0a36-7f50-ab1b-0e223a366540`

