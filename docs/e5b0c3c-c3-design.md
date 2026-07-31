# e5b0c3c-c3 设计稿 — 正常顺序 DeletionCoordinator + maintenance 接线

> 技术规格 §3.5（`lecturecast-server/docs/DIGITAL-HUMAN-TECH-SPEC.md`）
> 消费：c2 `resolve_deletion_plan_in_tx`（已锁）× c1 `AssetDeletionProcessor` / video `DeleteProcessor`（已锁）
> 前置：c1 4 轮 + c2 2 轮 Codex 审阅后锁定；921 测试全绿
> 本稿含：设计 + 盲预测 + 15 bypass risks 映射（12 调研 + R13 round-1 + R14 round-2 + R15 round-3）+ 34 测试矩阵 + Codex 问题

## 0. 范围

c3 把已锁的两块原语接起来：

1. **`DeletionCoordinator.delete_pass_for_operation`** —— 单 op 的正常顺序"哑迭代器"：开一次 tx 调 c2 resolver 拿冻结 plan → **关 tx** → 按 `resource_kind` 逐条路由到 c1/video processor。每条独立 claim/apply（各自 tx），逐资源可恢复。
2. **`DeletionCoordinator.recover_deletions`** —— maintenance 恢复入口：列出候选 op（tx 内 SELECT → **关 tx**）→ 逐 op 驱动 `delete_pass_for_operation`（网络调用在 tx 外）→ 聚合 tally。

**不放松任何 c1/c2 已锁不变量**：eligibility 归 claim、resolver 只管 ORDER/作用域、force 严格 bool、fenced lease/apply 隔离、reusable_avatar 全 kind 跳过、§3.5 顺序门禁。

## 1. 调研结论（ground truth，已逐行核对 `operation_repository.py`）

### 1.1 已锁 processor 签名（c1/video，精确）

```python
# line 3501
class DeleteProcessor:
    def __init__(self, project_dir): ...
    def delete_once(self, *, operation_id, resource_id, lease_owner, deleter,
                    now_iso, lease_seconds, max_attempts=DELETION_MAX_ATTEMPTS) -> DeletionOnceResult
        # claim tx1 (begin_immediate) → deleter.delete_video(claim.remote_id) 事务外
        # → apply tx2。非 claimed 早退 DeletionOnceResult(claim, outcome=None)。
        # DeleteAdapterError 被捕获为 result；其他异常传播（lease 自然过期，不猜）。

# line 3527
class AssetDeletionProcessor:
    def __init__(self, project_dir): ...
    def delete_once(self, *, upload_id, lease_owner, adapter, now_iso, lease_seconds,
                    max_attempts=DELETION_MAX_ATTEMPTS) -> AssetDeletionOnceResult
        # claim tx1 → adapter.delete_asset(claim.remote_id) 事务外（AssetReadError 捕获为 result，
        # 其他异常传播）→ apply tx2。非 claimed 早退。
```

**关键**：processor 自拥 tx（`begin_immediate` 在 `delete_once` 内开/关），coordinator **绝不**把 conn 传进去，也**绝不**在 processor 调用期间持有任何 tx。

### 1.2 数据类（已锁）

```python
# video 侧
DeletionOnceResult{claim: DeletionClaim, outcome: DeletionOutcome | None}
DeletionClaim{operation_id, resource_id, status, fence, remote_id}      # status: claimed|busy|retry_wait|not_ready
DeletionOutcome{operation_id, resource_id, status, fence, last_error, next_retry_at}  # status: deleted|failed|fence_conflict

# asset 侧（fence 在 asset 自身 lease 列，非 operation lease）
AssetDeletionOnceResult{claim: AssetDeletionClaim, outcome: AssetDeletionOutcome | None}
AssetDeletionClaim{upload_id, resource_id, status, fence, remote_id}
AssetDeletionOutcome{upload_id, resource_id, status, fence, last_error, next_retry_at}
```

### 1.3 c2 plan（已锁）

```python
DeletionPlan{operation_id, force: bool, video_download_status: str | None, entries: tuple[DeletionPlanEntry,...]}
DeletionPlanEntry{resource_id, resource_kind, upload_id: str|None, retention_mode, deletion_status, order_key}
# order_key: video=0, audio_asset=1, portrait_asset=2, unknown=9（surfaced，不 drop）
# 正常模式：非 deleted video 存在 → [video]；否则 [audio,portrait]。force：排除 video。
# reusable_avatar 全 kind 跳过；已 deleted/foreign op 跳过；download_status 只 advisory。
```

### 1.4 `begin_immediate`（line 361）

`@contextmanager`：开新 conn → `BEGIN IMMEDIATE` → yield → `COMMIT`/异常 `ROLLBACK` → `finally` `_chmod_secure` + **`conn.close()`**。退出 with 块即关闭，**无法跨 with 复用 conn**。

### 1.5 maintenance 参照：`recover_withdrawn_asset_cleanups`（line 2848）

```python
def recover_withdrawn_asset_cleanups(self, *, now_iso) -> dict[str,int]:
    aggregate = {...}
    with self.begin_immediate() as conn:           # 一个 tx 覆盖整个 loop
        rows = conn.execute("SELECT DISTINCT operation_id ... WHERE status='withdrawn'").fetchall()
        for r in rows:
            tally = self.enqueue_consent_withdrawal_cleanup_in_tx(conn, ...)  # DB-only per-op
            ...
    return aggregate
```

**它能在 loop 内持 tx，因为 per-op 调用（`enqueue_..._in_tx`）是纯 DB。c3 deletion maintenance 不能照抄**——per-op `delete_pass_for_operation` 是网络调用（remote DELETE），持 tx 跨网络会死锁/嵌套 `BEGIN IMMEDIATE` 报错（R5，最可能的复制粘贴错误）。

### 1.6 常量

`DELETION_MAX_ATTEMPTS = 3`（line 85）、`DELETION_BACKOFF_SECONDS = 120`（line 86）。

## 2. 设计

### 2.1 `DeletionCoordinator` 类

```python
class DeletionCoordinator:
    """§3.5 正常顺序删除协调器：消费 c2 DeletionPlan × c1/video processor。
    哑迭代器——不重判 eligibility（归各自 claim），只按 resource_kind 路由。
    每资源独立 claim/apply（各自 tx），逐资源 crash-safe。"""

    def __init__(self, project_dir: str | Path) -> None:
        self._project_dir = Path(project_dir)
        self._repository = OperationRepository(self._project_dir)
        self._video_processor = DeleteProcessor(self._project_dir)
        self._asset_processor = AssetDeletionProcessor(self._project_dir)
```

### 2.2 `delete_pass_for_operation` —— 单 op 一遍

```python
def delete_pass_for_operation(self, *, operation_id: str, force: bool = False,
                              deleter, adapter, lease_owner: str, now_iso: str,
                              lease_seconds: int,
                              max_attempts: int = DELETION_MAX_ATTEMPTS) -> DeletionPassResult:
    # 入口守卫（defense in depth，在 resolver 同款守卫之上）：
    if not isinstance(operation_id, str) or not operation_id:
        raise ValueError("operation_id must be a non-empty string")
    if type(force) is not bool:                      # 防 truthy 非布尔（R1）
        raise ValueError("force must be a bool")
    if not isinstance(lease_owner, str) or not lease_owner:
        raise ValueError("lease_owner must be a non-empty string")

    # 1) 在自己的 tx 里调 resolver 拿冻结 plan，然后【关 tx】（纯读，不能持跨网络）
    with self._repository.begin_immediate() as conn:
        plan = self._repository.resolve_deletion_plan_in_tx(
            conn, operation_id=operation_id, force=force)
    # 到这里 plan-resolution conn 已 close。下面全程无 coordinator 级 tx。

    attempts = []
    for entry in plan.entries:                       # verbatim，冻结顺序，不重排/重筛/重判
        attempt = self._attempt_entry(
            plan_operation_id=plan.operation_id, entry=entry,
            deleter=deleter, adapter=adapter, lease_owner=lease_owner,
            now_iso=now_iso, lease_seconds=lease_seconds, max_attempts=max_attempts)
        attempts.append(attempt)
    return DeletionPassResult(
        operation_id=plan.operation_id, force=plan.force,
        video_download_status=plan.video_download_status,
        attempts=tuple(attempts))
```

### 2.3 路由矩阵（`_attempt_entry`）

严格按 `entry.resource_kind` 分派；**不重判 eligibility**（c1 教训）。

| resource_kind | upload_id | 路由 | routed 标签 |
|---|---|---|---|
| `video` | （忽略） | `DeleteProcessor.delete_once(operation_id=plan.operation_id, resource_id=entry.resource_id, deleter=..., ...)` | `"video"` |
| `audio_asset` / `portrait_asset` | 非 None | `AssetDeletionProcessor.delete_once(upload_id=entry.upload_id, adapter=..., ...)` | `"asset"` |
| `audio_asset` / `portrait_asset` | None | **跳过 + 告警**（asset processor 拿不到 upload_id 无法执行；c2 观察遗留 #2） | `"skipped_no_upload_id"` |
| 其他（order_key=9） | — | **跳过 + 告警**（不猜路由） | `"skipped_unknown_kind"` |

每条：
- **非 claimed**（busy/retry_wait/not_ready）→ 记录 `claim_status`、`outcome_status=None`，continue。
- **untyped 异常**（processor 传播出来的非 AssetReadError/DeleteAdapterError）→ **catch + 告警**（`routed="alerted_exception"`）+ **该资源什么都不写**（lease 自然过期，下遍重 claim；绝不伪造 phantom outcome，R6），continue。
- 正常 → 记录 claim/outcome 的 status/last_error/next_retry_at。

**单 pass 内某资源 not_ready/busy/failed 不阻塞同 pass 后续资源**（各自独立 tx）。

### 2.4 返回类型（新冻结 dataclass）

```python
@dataclass(frozen=True)
class DeletionEntryAttempt:
    entry: DeletionPlanEntry
    routed: str                       # video | asset | skipped_no_upload_id | skipped_unknown_kind | alerted_exception
    claim_status: str | None          # processor claim 的 status；skipped/alerted 为 None
    outcome_status: str | None        # processor outcome 的 status；非 claimed/skipped/alerted 为 None
    last_error: str | None
    next_retry_at: str | None

@dataclass(frozen=True)
class DeletionPassResult:
    operation_id: str
    force: bool
    video_download_status: str | None
    attempts: tuple[DeletionEntryAttempt, ...]
    # 计数从 attempts 派生（property），杜绝 attempts 与计数漂移：
    @property
    def attempted(self) -> int: return len(self.attempts)
    @property
    def deleted(self) -> int: return sum(1 for a in self.attempts if a.outcome_status == "deleted")
    @property
    def failed(self) -> int: return sum(1 for a in self.attempts if a.outcome_status == "failed")
    @property
    def not_advanced(self) -> int:   # claimed 但 fence_conflict，或非 claimed（busy/retry_wait/not_ready）
        return sum(1 for a in self.attempts if a.claim_status is not None and a.outcome_status in (None, "fence_conflict"))
    @property
    def skipped(self) -> int: return sum(1 for a in self.attempts if a.routed.startswith("skipped"))
    @property
    def alerted(self) -> int: return sum(1 for a in self.attempts if a.routed == "alerted_exception")
```

### 2.5 force 决策（caller-supplied，默认 False）

- `force` 由 **caller 显式提供**，默认 `False`。coordinator 不从 DB/deletion_reason/retention 推断 force。
- 入口 `type(force) is not bool` 守卫（defense in depth，在 c2 resolver 同款守卫之上），原样透传给 resolver，**绝不预 coerce**（`bool(force)`）也**不**在转发前 `if force:` 判真值（R1）。
- consent_withdrawal 有自己的路径（`recover_withdrawn_asset_cleanups` 翻 cleanup_required/deletion_pending），不靠 force；manual_force 是 eligibility-only（c1/c2 已锁）。c3 coordinator 不改变这两条。

### 2.6 `recover_deletions` —— maintenance 恢复 pass

```python
def recover_deletions(self, *, deleter, adapter, lease_owner: str, now_iso: str,
                      lease_seconds: int, force: bool = False,
                      max_attempts: int = DELETION_MAX_ATTEMPTS) -> dict[str, int]:
    # 入口同款 force bool 守卫（R8：默认 False；force=True 是 operator-only/audited，
    # 作用于每个被扫的 op——这是 §3.5 force-cleanup 的 sweep 形态，默认绝不开启）。
    if type(force) is not bool:
        raise ValueError("force must be a bool")
    ...
    # 1) 候选 SELECT 在自己的 tx 里，然后【关 tx】（R5 核心：绝不在持 tx 时驱动网络删除）
    with self._repository.begin_immediate() as conn:
        if force:
            # 显式 force-cleanup（operator 授权）：每个 non-deleted non-reusable
            # resource 都在作用域；resolver 再排除 video、释放 tail。
            rows = conn.execute(
                "SELECT DISTINCT r.created_by_operation_id AS op_id "
                "FROM heygen_remote_resources r "
                "WHERE r.deletion_status != 'deleted' "
                "  AND r.retention_mode != 'reusable_avatar' "
                "  AND r.created_by_operation_id IS NOT NULL "
                "ORDER BY r.created_by_operation_id").fetchall()
        else:
            # 默认 sweep：只扫【删除已授权】的 op（Codex round-1 blocker 修复）。
            # 授权 = 有 video resource（generation 产出了交付物；video claim 再门禁
            # download_status=verified，resolver 把 asset 挡在 video 后）OR 有资源
            # 已进入删除管线（deletion_pending/deletion_failed——consent withdrawal/retry）。
            # 这排除"只有 pre-video asset 在 not_started、无 video"的 in-flight op：
            # resolver 会释放这些 asset（无 video 可挡），删掉仍在生产使用的 asset。
            # 授权 witness r2 带【与外层相同】的 retention 门禁（Codex round-2 P1 修复）：
            # reusable_avatar 资源被 resolver 全 kind 跳过，所以它永远不能授权删除同 op
            # 的 ephemeral asset——既不能当"有 video"证据（reusable video 被跳过后 tail 失守），
            # 也不能当"在删除管线"证据（reusable pending/failed 不代表 sibling 该删）。
            # "在删除管线" witness 再限到【自动可恢复 reason】（Codex round-3 P1 修复）：
            # manual_force 是 operator-only integrity 路径（c1 claim not_ready，绝不自动删），
            # 它不能授权 sweep sibling——否则 manual_force asset 把 op 楔进候选集，resolver
            # 释放的 tail 被 asset claim 删掉（asset claim 的 not_started 分支不复查 download_status）。
            # post_download / consent_withdrawal 是 pending/failed 资源唯一 claim-eligible 的 reason。
            # video 分支不限 reason：manual_force video 不可达（manual_force 只由 asset consent-apply
            # 路径产生），且非 deleted video 把 tail 挡在身后，deleted video 的 tail 合法（c2 锁定）。
            rows = conn.execute(
                "SELECT DISTINCT r.created_by_operation_id AS op_id "
                "FROM heygen_remote_resources r "
                "WHERE r.deletion_status != 'deleted' "
                "  AND r.retention_mode != 'reusable_avatar' "
                "  AND r.created_by_operation_id IS NOT NULL "
                "  AND EXISTS ("
                "    SELECT 1 FROM heygen_remote_resources r2 "
                "    WHERE r2.created_by_operation_id = r.created_by_operation_id "
                "    AND r2.retention_mode != 'reusable_avatar' "
                "    AND (r2.resource_kind = 'video'"
                "         OR (r2.deletion_status IN ('deletion_pending','deletion_failed')"
                "             AND r2.deletion_reason IN ('post_download','consent_withdrawal'))))"
                "ORDER BY r.created_by_operation_id").fetchall()
    op_ids = [r["op_id"] for r in rows]   # tx 已关

    # 2) 逐 op 驱动（每 op 独立 txs；一 op 异常 → 告警 + continue，不阻塞后续 op）
    aggregate = {"ops_driven": 0, "ops_empty": 0, "ops_alerted": 0,
                 "attempted": 0, "deleted": 0, "failed": 0, "skipped": 0, "alerted": 0}
    for op_id in op_ids:
        try:
            result = self.delete_pass_for_operation(
                operation_id=op_id, force=force, deleter=deleter, adapter=adapter,
                lease_owner=lease_owner, now_iso=now_iso,
                lease_seconds=lease_seconds, max_attempts=max_attempts)
        except Exception:                       # 一 op 的编程错误等：告警 + continue
            aggregate["ops_alerted"] += 1
            continue
        if not result.attempts:
            aggregate["ops_empty"] += 1
            continue
        aggregate["ops_driven"] += 1
        aggregate["attempted"] += result.attempted
        aggregate["deleted"] += result.deleted
        aggregate["failed"] += result.failed
        aggregate["skipped"] += result.skipped
        aggregate["alerted"] += result.alerted
    return aggregate
```

候选查询分两套（**Codex round-1/2/3 三轮 blocker 修复**）：
- **默认（force=False）= 删除已授权集**：op 有 video resource（含已 deleted 的——video claim 门禁 verified，resolver 把 asset 挡在 video 后；video 已 deleted 则 tail 合法）OR 有资源已在删除管线（deletion_pending/deletion_failed）。这把"只有 pre-video asset 在 not_started、无 video"的 in-flight op 排除掉——那种 op 的 asset 仍在生产使用，resolver 会因"无 video"直接释放 tail 误删。
  - **授权 witness 带 retention 门禁（round-2 P1）**：EXISTS 子查询里的 `r2` 也必须 `retention_mode != 'reusable_avatar'`。否则一个 reusable video 会充当"有 video"的假授权证据——外层 `r` 取的是 ephemeral asset（过外层 retention 过滤），witness `r2` 取 reusable video（无 retention 过滤）→ op 进候选 → resolver 跳过 reusable video → tail 失守 → 删掉生产中的 ephemeral asset。reusable pending/failed 资源同理（`deletion_status IN (...)` 分支也要堵）。**回归 T17c（reusable video witness）/ T17d（reusable pending witness）**，两测在未修代码上均失败复现。
  - **"在删除管线" witness 限到自动可恢复 reason（round-3 P1）**：pending/failed 分支再加 `deletion_reason IN ('post_download','consent_withdrawal')`，排除 `manual_force`。manual_force 是 operator-only integrity 路径（c1 claim not_ready，绝不自动删），不能授权 sweep sibling——否则 manual_force asset 把 op 楔进候选集，resolver 释放的 tail 被 asset claim 删掉（asset claim 的 not_started 分支**不复查 download_status**，只看矩阵可推进）。**回归 T17e（pending/manual_force witness）/ T17f（failed/manual_force witness）**，两测在未修代码上均失败复现。video 分支不限 reason：manual_force video 不可达（manual_force 只由 asset consent-apply 路径产生），且非 deleted video 把 tail 挡在身后。
- **force=True = 宽集**：任意 non-deleted non-reusable resource（显式 operator 授权的 force-cleanup）。

resolver + claim 仍逐 op 精细化（无作用域的候选 op 返空 plan → `ops_empty`）。已完全删除的 op 不进候选（外层 `deletion_status != 'deleted'`）。**幂等重跑安全**（T21）：已删除资源 resolver 跳过 → 空 plan → no-op。

## 3. 盲预测（我预判 Codex 会探的点 + 我的防御）

1. **force truthy 绕过（R1）**：coordinator 入口若用 `if force:` 判真值或 `bool(force)` coerce，`force="false"`/`1`/`[]` 会进 force 分支排除 video。→ 入口 `type(force) is not bool` 守卫 + 原样透传 + 不 coerce（同 c1/c2 教训，bool flag 是"自身作用域授权"，下游兜不住）。
2. **coordinator 重判 eligibility（R2，c1 明令禁止的 parallel-gate）**：若 coordinator 自己按 deletion_status 重筛/跳"已 deleted"/重排，就造了第二个会漂移的门。→ verbatim 遍历冻结 entries，claim 是唯一 eligibility 权威。
3. **持 tx 跨网络 / 把 conn 传进 processor（R4/R12）**：plan-resolution 的 conn 绝不能在 `delete_once` 调用时还开着。→ resolve 在 with 块内，with 退出即 close；processor 自拥 tx；coordinator 全程不持 tx。
4. **maintenance 照抄 `recover_withdrawn_asset_cleanups` 在 loop 内持 tx（R5，最可能的复制粘贴错误）**：→ 候选 SELECT 在 with 内，op_ids 在 with 外组装，驱动 loop 在 with 之外。
5. **untyped 异常伪造 phantom outcome（R6）**：processor 传播非类型异常 = 远端结果不可知。→ catch + 告警 + 该资源**零写入**（lease 过期重 claim），绝不 apply/标 deleted/设 next_retry_at。
6. **upload_id=None asset 静默 drop（R11）**：→ 不静默跳，记 `skipped_no_upload_id` 告警；unknown_kind 同理 `skipped_unknown_kind`（resolver 已 surface，coordinator 不 drop）。
7. **lease_owner / now_iso 中途漂移（R9/R10）**：→ 同一 pass 内每条 `delete_once` 传**同一个** `lease_owner` 和**同一个** `now_iso`，逐条不重读时钟/不改 owner。
8. **plan 中途重 resolve 热循环（R7）**：→ 每 pass 只调一次 `resolve_deletion_plan_in_tx`，遍历冻结快照；非 claimed = continue（不是 retry-now）。
9. **blanket force sweep（R8）**：→ `recover_deletions(force=False)` 默认；force sweep 是 operator-only/audited；默认全 sweep 尊重 video-verified 门禁（T19）。
10. **错路由（R3）**：video→asset processor 会用错 lease 列/错 fence/错 remote_id 源；upload_id=None 的 asset 进 asset processor 无效。→ 严格按 kind 分派，asset 必须有 upload_id 才进 asset processor。

> **诚实记录**：下面这条是 **Codex round-1 抓到、我盲预测 + R1–R12 都漏掉的** blocker——我原候选查询把"有任意 non-deleted non-reusable resource"当作充分条件，没意识到 sweep 语境下"无 video"的歧义（video 已 deleted 合法 tail vs video 从未创建的 in-flight 生产）。归因：盲预测聚焦在 coordinator→processor 的 fencing/tx 隔离（c1/c2 同类），没把 resolver 的"无 video → 释放 tail"规则放回 sweep 语境重新审视。教训：**单 op 显式删除时合法的 resolver 规则，搬到自动 sweep 语境会改变语义**——sweep 的候选门禁必须是独立的一道安全边界，不能继承单 op 调用的隐含前提（caller 已选择删这个 op）。
11. **【round-1 才发现】sweep 候选门禁太宽 → 误删 in-flight asset（R13）**：默认 sweep 若把"任意 non-deleted non-reusable resource"当候选，一个 `submit_pending` + asset uploaded/not_started + 无 video 的 in-flight op 会被扫到，resolver 因"无 video"释放 tail → 删掉仍在生产使用的 asset。→ 默认候选集收紧为"删除已授权"（有 video resource OR 有 deletion_pending/deletion_failed）；force=True 保留宽集（显式授权）。回归测试：in-flight asset-only op 默认不扫 / force 扫。

> **诚实记录 #2（round-2）**：round-1 修了候选门禁的"外层"语义，但 EXISTS 授权 witness `r2` **漏带 retention 门禁**——这是 Codex round-2 抓到的 P1，我盲预测 + R1–R13 又一次漏掉。归因：round-1 修复时只盯着"外层候选行要过滤 reusable"，没把同样的不变量投影到 EXISTS 子查询的 witness 上；潜意识把 `r` 和 `r2` 当成"同一过滤的两步"，其实它们是**不同行**（外层 `r` = ephemeral asset，witness `r2` = reusable video），各需独立 retention 守卫。教训延续 R13：**reusable_avatar 全 kind 被 resolver 跳过这个不变量，必须在每一处"资源充当授权证据"的 SQL 里逐字重申**——不能假设外层过滤会传染到子查询。Codex 还实测复现（`adapter_calls: ["aLive"]`, asset deleted）。修复：EXISTS `r2` 加 `retention_mode != 'reusable_avatar'`，回归 T17c（reusable video witness）/ T17d（reusable pending witness），两测未修代码上均失败。
12. **【round-2 才发现】EXISTS 授权 witness 漏 retention 门禁 → reusable 资源假授权（R14）**：默认候选 EXISTS 的 `r2`（video OR pending/failed witness）若不带 `retention_mode != 'reusable_avatar'`，一个 reusable video（或 reusable pending/failed 资源）能充当授权证据，让"reusable video + ephemeral asset + 无真实 video"的 in-flight op 进候选，resolver 跳过 reusable video 后释放 tail → 删掉生产中的 ephemeral asset。→ witness `r2` 带与外层相同的 retention 门禁。回归 T17c/T17d。

> **诚实记录 #3（round-3）**：round-2 修了 retention，但"在删除管线"witness 只看 `deletion_status`、不看 `deletion_reason`——Codex round-3 抓的 P1，我盲预测 + R1–R14 第三次漏掉。归因：我把"pending/failed 资源 = 已进入删除管线 = 可恢复"当成等价类，没区分 reason；忘了 c1 早已锁定 `manual_force → not_ready，绝不自动删`这条更强的不变量。manual_force 资源自身虽被 claim 保护，却能当楔子把 op 楔进候选集，resolver 释放 tail 后，**asset claim 的 not_started 分支不复查 download_status**（只看矩阵可推进），于是 sibling 被删。这条机制（asset claim 不复查 download_status）是 c1 已锁的设计（comment: "c2/c3 resolver gates ordering + receipt; c1 only requires the matrix to be advanceable"），本身没错——错在候选门禁没把 manual_force 排除出 witness。教训延续 R13/R14：**"资源充当授权证据"的 SQL，每一处都要逐字重申所有已锁不变量**（这次轮到 reason 不变量）。Codex 实测：`adapter_calls: ["pLive"]`, attempted 2 / deleted 1，sibling portrait deleted/post_download。修复：pending/failed witness 加 `deletion_reason IN ('post_download','consent_withdrawal')`，回归 T17e（pending/manual_force）/ T17f（failed/manual_force），两测未修代码上均失败。round-3 同时确认 retention 修复闭合、T17c/T17d 真实覆盖、合法 tail / consent / retry / force 宽集不受影响。
13. **【round-3 才发现】"在删除管线" witness 漏 reason 门禁 → manual_force 假授权（R15）**：默认候选 pending/failed witness 若不限 reason，一个 `manual_force` 资源（c1 锁定：绝不自动删）能当授权证据，让它的 sibling（uploaded/not_started）被自动删（asset claim not_started 分支不复查 download_status）。→ pending/failed witness 加 `deletion_reason IN ('post_download','consent_withdrawal')`（自动可恢复 reason 全集 = 这两个；manual_force 排除）。回归 T17e/T17f。video 分支不限 reason（manual_force video 不可达 + tail 门禁保护）。

## 4. bypass risks × 防御映射（12 来自调研 + R13 round-1 + R14 round-2 + R15 round-3）

| Risk | 一句话 | 防御 | 测试 |
|---|---|---|---|
| R1 | force truthy 非布尔 | 入口 `type(force) is bool`，不 coerce | T10/T11/T12 |
| R2 | coordinator 重判 eligibility/顺序 | verbatim 冻结 entries，claim 唯一权威 | T1/T2/T14 |
| R3 | resource_kind 错路由 | 严格 kind 分派 + upload_id 守卫 | T4/T5/T6 |
| R4 | 共享 tx 跨 loop/adapter | resolve tx 关后再 loop；processor 自拥 tx | T13 |
| R5 | maintenance 持 tx 跨网络（复制粘贴陷阱） | 候选 SELECT 关 tx 后再驱动 | T18 |
| R6 | untyped 异常 phantom outcome | catch+告警+零写入 | T9 |
| R7 | 中途重 resolve/热循环 | 每 pass 一次 resolve，非 claimed=continue | T14/T21 |
| R8 | blanket force sweep | force 默认 False，operator-only | T19 |
| R9 | lease_owner 漂移 | 同 pass 同 owner | T15 |
| R10 | now_iso 中途漂移 | 同 pass 同 now_iso | T16 |
| R11 | upload_id=None 静默 drop | skipped_no_upload_id 告警 | T5 |
| R12 | 复用 resolve conn 给首个 claim | resolve conn 在 with 内随退出 close | T13 |
| **R13** | **sweep 候选门禁太宽 → 误删 in-flight asset（round-1）** | **默认候选 = 删除已授权（有 video OR pending/failed）；force 保留宽集** | **T17 + 回归×2** |
| **R14** | **EXISTS 授权 witness 漏 retention → reusable 假授权（round-2）** | **witness `r2` 带与外层相同的 `retention_mode != 'reusable_avatar'`** | **T17c/T17d** |
| **R15** | **"在删除管线" witness 漏 reason → manual_force 假授权（round-3）** | **pending/failed witness 加 `deletion_reason IN ('post_download','consent_withdrawal')`（排除 manual_force）** | **T17e/T17f** |

## 5. 测试矩阵（34，新文件 `tests/test_deletion_coordinator.py`）

> 跨 video+asset+routing，区别于 `test_deletion.py`（video-only）与 `test_asset_journal.py`（asset/planner）。
> House style：`_db()` tempdir / `tmp_path`；模块级 NOW/OWNER/LEASE；**重开 fresh `sqlite3.connect` row_factory=Row 断言最终 DB**（不读同 conn）；attribute access on result；`@parametrize` 矩阵。Fakes：`_StubDeleter`（`test_deletion.py:73`）、`_FakeAdapter`（`test_asset_journal.py:2174`）、`_Boom`（`test_asset_journal.py:2278`）。

- **T1** 正常模式单 video 门禁：verified download + audio/portrait rows → `force=False` → 只 video 进 attempts（audio/portrait 本遍不 attempt），video outcome=deleted，fresh-conn `deletion_status=deleted` + `deleted_at` 非空。
- **T2** 多 pass tail：T1 后再调一遍 → audio→portrait 按 order_key 顺序 attempt，均 deleted。
- **T3** force=True 排除 video：非 deleted video 在 → `force=True` → 无 video attempt，audio+portrait deleted。
- **T4** 路由矩阵（parametrize）：spy fake 记录被调方法。video→`delete_video` 调，`delete_asset` 不调；audio/portrait→反之。
- **T5** asset `upload_id=None` → `skipped_no_upload_id`，`delete_asset` 不调，该资源 DB 不变，loop 续跑后续 entry。（最可能抓 silent-drop 回归）
- **T6** unknown `resource_kind`（order_key 9）→ `skipped_unknown_kind`，无 processor 调用，无 DB 写。
- **T7** 逐资源独立：一 asset `AssetReadError(code='rate_limited', retryable=True)`，另一 deleted → 首个 failed+last_error+next_retry_at，第二个 deleted，**都 attempt 了**。
- **T8** 非 claimed（busy）续跑：一资源 lease 被别 owner 持 → `claim_status=busy`、`outcome_status=None`、无 adapter 调用；下一 entry 仍 attempt。
- **T9** untyped 异常（`_Boom`）→ `alerted_exception`，loop 续跑；fresh-conn 断言该资源**无 outcome 写入**（claim 合法写了 `deletion_pending`+lease，但 apply 没跑：`deleted_at` None、`last_deletion_error` None、`deletion_next_retry_at` None、asset 仍 `cleanup_required` 非 deleted）——lease 过期重 claim，无 phantom。（措辞按 round-1 Codex 提示精确化：claim 的合法写不算 phantom，真正保证的是"不写 outcome、不猜 deleted/failed"。）
- **T10** force 错型入口：`force='false'`（truthy str）→ `pytest.raises(ValueError)` 在 resolver 前触发，零写入、零 deleter/adapter 调用。
- **T11** force 错型 parametrize `[1, None, [], 0]`（int 0 也必须拒——`type(force) is not bool`），全 raise，零写入。
- **T12** force 篡改不静默放 video：非 deleted video 在 + `force='false'` → 入口先拒，video 绝不被删（守卫先于路由）。
- **T13** 无共享 tx 跨 loop/adapter：instrument `begin_immediate`/fakes 快照 adapter 调用期间是否有 coordinator 级 conn 开着 → 断言**零**（processor 的 claim tx 已 COMMIT+close）。
- **T14** 每 pass 只 resolve 一次：计数 `resolve_deletion_plan_in_tx` 调用 → 每 `delete_pass_for_operation` 恰好 1，与 entry 数无关。
- **T15** lease_owner 身份绑定：spy 记录每条 `delete_once` 的 `lease_owner` → 全等于 coordinator kwarg（无逐条漂移、无 plan-tx vs processor-tx owner 错位）。
- **T16** now_iso 透传：spy 断言同 pass 每条 `delete_once` 收**同一** `now_iso`（无逐条漂移）。
- **T17** maintenance `recover_deletions` 形状：插 3 op（op_A post-download video 可删、op_B video 已 deleted→audio tail 合法、op_C 全 deleted 非候选）→ 只候选 op 被驱动；返聚合 tally（`ops_driven`/`deleted`/`failed`/`skipped`/`alerted`/`ops_empty`）；每 driven op 独立 tx。（round-1 修复：op_B 从"audio-only in-flight"改成"video 已 deleted 的合法 tail"，原 T17 固化了误删漏洞。）
- **T17a**【round-1 回归】in-flight asset-only op（`submit_pending` + asset uploaded/not_started + 无 video）默认 sweep **不**驱动：adapter 不调，asset 仍 uploaded/not_started。
- **T17b**【round-1 回归镜像】同 op + `force=True` **被**驱动删除（显式 operator 授权用宽候选集）。
- **T17c**【round-2 回归】reusable video 假授权：`submit_pending` + reusable_avatar video + ephemeral asset uploaded/not_started → 默认 sweep **不**驱动（witness `r2` retention 门禁堵住 reusable video 当"有 video"证据），adapter 不调，asset 仍 uploaded/not_started。未修代码上失败复现（`ops_driven==1`）。
- **T17d**【round-2 回归镜像】reusable pending 资源假授权：同 op + reusable_avatar portrait 在 `deletion_pending` + ephemeral asset → 默认 sweep **不**驱动（`deletion_status IN (...)` witness 分支也堵 reusable），asset 不变。未修代码上失败复现。
- **T17e**【round-3 回归】manual_force pending 假授权：`submit_pending` + audio `cleanup_required/deletion_pending/manual_force`（带 consent_integrity_failure marker）+ portrait `uploaded/not_started` → 默认 sweep **不**驱动（pending/failed witness 加 `reason IN ('post_download','consent_withdrawal')` 排除 manual_force），adapter 不调，两 asset 均不变。未修代码上失败复现（`ops_driven==1`，sibling portrait 被删，复现 Codex `adapter_calls: ["pLive"]`）。
- **T17f**【round-3 回归镜像】manual_force failed 假授权：同 op + audio `cleanup_required/deletion_failed/manual_force` + portrait → 默认 sweep **不**驱动（failed 分支也排除 manual_force），asset 不变。未修代码上失败复现。
- **T18** maintenance 候选 SELECT tx **不**跨网络：instrument 断言候选 tx 在任何 `delete_pass_for_operation` 前已 close（R5 直接测试）。
- **T19** maintenance `force=False` 默认全 sweep 尊重 video-verified 门禁：sweep 内任一 `download_status!='verified'` 的 op → 其 video 要么不在 plan、要么 `claim_status=not_ready`，绝不 deleted。
- **T20** crash 中途恢复：stub 一 op 的 `delete_once` 在前面 op 成功后 `_Boom` → 前面 op 写入**已持久化**（独立 tx 已 commit），异常 op 零 phantom 写入，后续 op 仍被驱动（CONTINUE+告警）。
- **T21** 幂等重跑/无热循环：`recover_deletions` 跑两遍 → 第二遍只驱动仍有 non-deleted 资源的 op；已删 → 空 plan/no-op，无 double-delete、无错、无死循环。

## 6. Codex 问题（round-1 → 已回复，结论标 ❑）

1. ❑ **force 入口守卫**：Codex 确认 R1 正确（strict bool guard + 原样透传）。
2. ❑ **eligibility 不重判**：Codex 确认 R2/R7 到位（plan 只 resolve 一次，冻结顺序原样遍历）。
3. ❑ **tx 隔离**：Codex 确认 R4/R5/R12 守住（instrumentation 测试真实覆盖）。
4. ❑ **untyped 异常**：Codex 确认 R6 实现安全；提示措辞"不写任何东西"不精确（claim 合法写了 deletion_pending+lease）→ 已精确化为"不写 outcome、不猜 deleted/failed"（T9 断言 + 代码注释同步）。
5. ❑ **upload_id=None / unknown kind**：Codex 确认 R3/R11 覆盖。
6. ❑ **lease_owner / now_iso 恒定**：Codex 确认 R9/R10 到位。
7. ❑ **recover_deletions force 语义**：Codex 认可保留 force 参数（调用层保证 operator/audited），但要求默认 False 必须配 #8 的候选收紧。
8. ✅ **候选查询**（round-1 blocker）：Codex 判定原"超近似"候选集是 **P1 blocker**——会误删 in-flight op 的生产 asset。已收紧：默认 = 删除已授权（有 video OR pending/failed）；force = 宽集。回归 T17a/T17b。
9. ✅ **EXISTS witness retention**（round-2 P1）：Codex round-2 判"不可锁"——EXISTS 授权 witness `r2` 漏带 `retention_mode != 'reusable_avatar'`，reusable video / reusable pending-failed 资源能假授权，让"reusable video + ephemeral asset + 无真实 video"的 in-flight op 进候选 → resolver 跳过 reusable video → tail 失守 → 删生产 asset（Codex 实测复现）。已修：witness `r2` 带与外层相同的 retention 门禁；回归 T17c/T17d（未修代码均失败）。Codex 同时确认：合法 tail（deleted ephemeral video）/ consent-withdrawal（ephemeral pending）/ retry（ephemeral failed）/ force 宽集均不受影响。
10. ✅ **witness reason gate**（round-3 P1）：Codex round-3 判"不可锁"——retention 已闭合，但"在删除管线"witness 只看 `deletion_status`、不看 `deletion_reason`，`manual_force` 资源（c1 锁定：绝不自动删）能当授权证据，让 sibling uploaded/not_started 被自动删（asset claim not_started 分支不复查 download_status；Codex 实测 `adapter_calls:["pLive"]` / attempted 2 / deleted 1）。已修：pending/failed witness 加 `deletion_reason IN ('post_download','consent_withdrawal')`（自动可恢复 reason 全集），排除 manual_force；回归 T17e（pending/manual_force）/ T17f（failed/manual_force），未修代码均失败。Codex 同时确认 retention 修复闭合、T17c/T17d 真实覆盖、合法 tail / consent / retry / force 宽集不受影响。round-3 还复核：video 分支不限 reason 是安全的——manual_force video 不可达（manual_force 只由 asset consent-apply 路径产生），且非 deleted video 把 tail 挡在身后。

## 7. 实现顺序

1. 加 `DeletionEntryAttempt` / `DeletionPassResult` dataclass（counts 派生 property）。
2. 加 `DeletionCoordinator.__init__` + `delete_pass_for_operation` + `_attempt_entry`（路由 + per-item 异常）。
3. 加 `DeletionCoordinator.recover_deletions`（候选 SELECT 关 tx → 逐 op 驱动）。
4. 写 `tests/test_deletion_coordinator.py`（34 测）。
5. 全量 `pytest`（921 + 34 = 955 全绿）。
6. commit → 发 Codex round-1 → **round-1 抓 1 个 P1（候选门禁太宽）→ 修 + 回归×2 → round-2 锁定复审 → round-2 抓 1 个 P1（EXISTS witness 漏 retention）→ 修 + 回归×2 → round-3 锁定复审 → round-3 抓 1 个 P1（witness 漏 reason，manual_force 假授权）→ 修 + 回归×2 → round-4 锁定复审**。

