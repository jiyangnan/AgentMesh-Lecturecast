# e5b0c3c-c3 设计稿 — 正常顺序 DeletionCoordinator + maintenance 接线

> 技术规格 §3.5（`lecturecast-server/docs/DIGITAL-HUMAN-TECH-SPEC.md`）
> 消费：c2 `resolve_deletion_plan_in_tx`（已锁）× c1 `AssetDeletionProcessor` / video `DeleteProcessor`（已锁）
> 前置：c1 4 轮 + c2 2 轮 Codex 审阅后锁定；921 测试全绿
> 本稿含：设计 + 盲预测 + 12 bypass risks 映射 + 21 测试矩阵 + Codex 问题

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
        rows = conn.execute(
            "SELECT DISTINCT r.created_by_operation_id AS op_id "
            "FROM heygen_remote_resources r "
            "WHERE r.deletion_status != 'deleted' "
            "  AND r.retention_mode != 'reusable_avatar' "
            "  AND r.created_by_operation_id IS NOT NULL "
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

候选查询是**超近似**（resolver + claim 精细化）：有任意 non-deleted non-reusable resource 的 op；resolver 对无作用域的 op 返回空 plan → `ops_empty`。已完全删除的 op 不进候选（`deletion_status != 'deleted'`）。**幂等重跑安全**（T21）：已删除资源 resolver 跳过 → 空 plan → no-op。

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

## 4. 12 bypass risks × 防御映射（来自 c3 调研 workflow 综合）

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

## 5. 测试矩阵（21，新文件 `tests/test_deletion_coordinator.py`）

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
- **T9** untyped 异常（`_Boom`）→ `alerted_exception`，loop 续跑；**关键** fresh-conn 断言该资源零写入（`deletion_status` 不变、`last_error` None、`next_retry_at` None、无 `deleted_at`）——lease 过期重 claim，无 phantom。
- **T10** force 错型入口：`force='false'`（truthy str）→ `pytest.raises(ValueError)` 在 resolver 前触发，零写入、零 deleter/adapter 调用。
- **T11** force 错型 parametrize `[1, None, [], 0]`（int 0 也必须拒——`type(force) is not bool`），全 raise，零写入。
- **T12** force 篡改不静默放 video：非 deleted video 在 + `force='false'` → 入口先拒，video 绝不被删（守卫先于路由）。
- **T13** 无共享 tx 跨 loop/adapter：instrument `begin_immediate`/fakes 快照 adapter 调用期间是否有 coordinator 级 conn 开着 → 断言**零**（processor 的 claim tx 已 COMMIT+close）。
- **T14** 每 pass 只 resolve 一次：计数 `resolve_deletion_plan_in_tx` 调用 → 每 `delete_pass_for_operation` 恰好 1，与 entry 数无关。
- **T15** lease_owner 身份绑定：spy 记录每条 `delete_once` 的 `lease_owner` → 全等于 coordinator kwarg（无逐条漂移、无 plan-tx vs processor-tx owner 错位）。
- **T16** now_iso 透传：spy 断言同 pass 每条 `delete_once` 收**同一** `now_iso`（无逐条漂移）。
- **T17** maintenance `recover_deletions` 形状：插 3 op（一 verified-ready、一 withdrawn receipt、一已全删）→ 只候选 op 被驱动；返聚合 tally（`ops_driven`/`deleted`/`failed`/`skipped`/`alerted`/`ops_empty`）；每 driven op 独立 tx。
- **T18** maintenance 候选 SELECT tx **不**跨网络：instrument 断言候选 tx 在任何 `delete_pass_for_operation` 前已 close（R5 直接测试）。
- **T19** maintenance `force=False` 默认全 sweep 尊重 video-verified 门禁：sweep 内任一 `download_status!='verified'` 的 op → 其 video 要么不在 plan、要么 `claim_status=not_ready`，绝不 deleted。
- **T20** crash 中途恢复：stub 一 op 的 `delete_once` 在前面 op 成功后 `_Boom` → 前面 op 写入**已持久化**（独立 tx 已 commit），异常 op 零 phantom 写入，后续 op 仍被驱动（CONTINUE+告警）。
- **T21** 幂等重跑/无热循环：`recover_deletions` 跑两遍 → 第二遍只驱动仍有 non-deleted 资源的 op；已删 → 空 plan/no-op，无 double-delete、无错、无死循环。

## 6. Codex 问题（round-1）

1. **force 入口守卫是否足够**：coordinator 入口 `type(force) is not bool` + 原样透传（不 coerce、不 `if force:` 判真值），defense in depth 在 c2 resolver 同款守卫之上。你看还有没有 truthy 绕过路径（R1）？
2. **eligibility 不重判**：coordinator verbatim 遍历冻结 entries，不重排/重筛/跳"已 deleted"/自判 verified 门禁。这是 c1 "reuse locked invariants, no parallel gate" 的延续。是否到位（R2）？
3. **tx 隔离**：resolve 在 with 内、with 退出即 close、processor 自拥 tx、coordinator 全程不持 tx；maintenance 候选 SELECT 关 tx 后才驱动。是否守住 R4/R5/R12？
4. **untyped 异常 = 零写入**：catch+`alerted_exception`+该资源**什么都不写**（不 apply/不标 deleted/不设 next_retry_at），lease 自然过期。是否正确遵循"远端结果不可知绝不猜"（R6）？
5. **upload_id=None / unknown kind 不静默 drop**：分别 `skipped_no_upload_id` / `skipped_unknown_kind` 告警。是否覆盖（R3/R11）？
6. **lease_owner / now_iso 单 pass 内恒定**：每条 `delete_once` 传同一值。是否到位（R9/R10）？
7. **recover_deletions 的 force 语义**：默认 False；force=True 作用于每个被扫 op（operator-only/audited）。这个 sweep-force 形态是否符合 §3.5，还是你认为 maintenance 应完全无 force 参数（R8）？我倾向保留（T19 测默认尊重门禁），但想听你的。
8. **候选查询超近似**：`deletion_status != 'deleted' AND retention_mode != 'reusable_avatar' AND created_by_operation_id IS NOT NULL`，resolver + claim 精细化。这个候选集是否合理，还是该收紧（如只扫 cleanup_required/deletion_pending/not_started 的）？

## 7. 实现顺序

1. 加 `DeletionEntryAttempt` / `DeletionPassResult` dataclass（counts 派生 property）。
2. 加 `DeletionCoordinator.__init__` + `delete_pass_for_operation` + `_attempt_entry`（路由 + per-item 异常）。
3. 加 `DeletionCoordinator.recover_deletions`（候选 SELECT 关 tx → 逐 op 驱动）。
4. 写 `tests/test_deletion_coordinator.py`（21 测）。
5. 全量 `pytest`（应 921 + 21 = 942）。
6. commit → 发 Codex round-1（resume `019fb840-a93b-73e1-b56c-a29b07a15e3d`，effort=low，`-C` 在 `resume` 前）。
