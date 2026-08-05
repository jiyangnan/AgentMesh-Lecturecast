# §5.5e5b0c3c-c2 设计稿 + 盲预测（消费门禁 resolver）

> 范围：`resolve_deletion_plan_in_tx` —— 给定 operation + force 标志，按 §3.5 返回"当前可尝试删除"的有序 resource 列表。
> 规格依据：tech spec §3.5（正常批量顺序 video(需 verified)→audio→ephemeral portrait+avatar；force-cleanup 绕过 video 阶段；reusable_avatar 保留）。
> 前置：c1 已锁（4 轮 Codex，896 测试绿）。c2 只做**纯规划**，不 claim/delete（那是 c3 coordinator）。

## 1. 职责边界（关键，吸取 c1 教训）

c1 教训（`feedback_fencing_identity_binding`）：**复用已锁不变量，不要新造一套松的**。

每资源的 **eligibility**（verified 门禁、manual_force、retry backoff、矩阵、topology）已经权威地封在各自 claim 里：
- video：`claim_deletion_in_tx`（operation_repository:1638）—— not_started+ephemeral+`download_status=verified`+single_video → post_download；consent_withdrawal；manual_force→not_ready。
- asset：`claim_asset_deletion_in_tx`（c1）—— asset status∈{uploaded,cleanup_required} + 矩阵 + reason gate。

**c2 resolver 不重判 eligibility。** 它只做三件 claim 做不了的事：
1. **跨资源排序**（§3.5 固定顺序 video→audio→portrait）。
2. **结构化作用域过滤**：reusable_avatar 跳过（retention）、force 模式排除 video、已 deleted 跳过（无事可做）、非本 op 资源跳过。
3. **§3.5 顺序门禁**：正常模式下 audio/portrait 必须等 video 先删（保护交付物）—— resolver 把它编码进"当前可尝试"列表。

eligibility 仍由 c3 coordinator 调 claim 时权威决定（claim 返回 not_ready/retry_wait 等）。resolver 报告的是"结构上可尝试 + 顺序正确"，不是"claim 必成功"。

## 2. 签名

```python
@dataclass(frozen=True)
class DeletionPlanEntry:
    resource_id: int
    resource_kind: str            # video / audio_asset / portrait_asset
    upload_id: str | None         # asset 走 AssetDeletionProcessor；video 为 None
    retention_mode: str           # ephemeral（reusable 已被过滤）
    deletion_status: str          # 当前 resource 状态（advisory，claim 权威）
    order_key: int                # 0=video, 1=audio, 2=portrait（确定性序）

@dataclass(frozen=True)
class DeletionPlan:
    operation_id: str
    force: bool
    video_download_status: str | None   # op 的 video 下载状态（上下文）
    entries: tuple[DeletionPlanEntry, ...]   # 有序

class OperationRepository:
    def resolve_deletion_plan_in_tx(
        self, conn, *, operation_id, force=False,
    ) -> DeletionPlan: ...
```

`_require_tx(conn)`；纯读，无写，无 now_iso（排序与时间无关，retry_wait 由 claim 管）。

## 3. 算法

1. 读 op 行（`heygen_operations`）。op 不存在 → `DeletionPlan(operation_id, force, None, ())`（空，不抛）。
2. 枚举本 op 的 video resource：`heygen_remote_resources` JOIN refs，`resource_kind='video' AND created_by_operation_id=operation_id`，单 ref（`_single_video` 契约：至多一个）。读 `deletion_status`、`retention_mode`。
3. 枚举本 op 的 asset resources：`heygen_asset_uploads.parent_operation_id=operation_id`，读 `upload_id/asset_role/status/remote_resource_id`，用 `_asset_resource_kind(role)`/`_asset_retention_mode(role)` 派生 kind/retention（**复用，不重造**）。asset 的 resource 行由 `remote_resource_id` 关联，读其 `deletion_status`。
4. **过滤**（结构化作用域）：
   - `retention_mode='reusable_avatar'` → 跳过（spec：保留，撤销走 dashboard）。
   - `deletion_status='deleted'` → 跳过（已完成）。
   - asset `remote_resource_id IS NULL` → 跳过（防御，无绑定资源）。
   - asset `status='deleted'` 但 resource 未 deleted → 矩阵非法，**不在 resolver 处理**（claim/matrix 会 fail-closed）；resolver 只看 resource deletion_status。
5. **排序 + 顺序门禁**：
   - **force=True**：video **全部排除**（spec：绕过 video 阶段）。entries = audio(order_key=1) → portrait(order_key=2)，各自按 upload_id 字典序确定性排列。无 video 门禁。
   - **force=False（正常）**：
     - 若 video 存在且 `deletion_status != 'deleted'` → entries = **[video]（仅）**。audio/portrait 被门禁挡住（必须等 video 先删）。
     - 若 video 不存在或已 deleted → entries = audio(1) → portrait(2)。
6. 返回 `DeletionPlan`。

## 4. c3 coordinator 如何消费（c2 锁后细化，此处仅定契约）

coordinator 遍历 `plan.entries`，按 `resource_kind`/`upload_id` 路由：video→`DeleteProcessor.delete_once`，asset→`AssetDeletionProcessor.delete_once`。因 resolver 已保证"正常模式下 audio/portrait 仅在 video 删除后才出现"，coordinator 是**哑迭代器**，无需自己判序。force 模式下 plan 无 video，直接删 audio/portrait。每资源独立 claim/apply（各自 tx），逐资源可恢复。

## 5. 盲预测（会踩的坑 / 待 Codex 确认）

1. **force 模式是否也删 video？** spec §3.5 force "独立删 audio/portrait/avatar"，字面排除 video。但 force 触发条件之一是"video 持续下载失败"——这种 video 留在远端不删？**待确认**：force plan 是否完全排除 video，还是 video 走单独路径。当前设计：force 完全排除 video（字面 spec）。**Codex 问题 #1**。
2. **正常模式顺序门禁的粒度**：audio/portrait 等"video 已 deleted"还是"video 本次 claim 成功（即使本 pass 才删）"？当前设计：resolver 等"video resource 已 deleted"（跨 pass），所以正常全清理需多 pass（pass1 删 video，pass2 删 audio/portrait）。是否要支持单 pass 内 video 删成功后继续删 audio？**Codex 问题 #2**：单 pass 串行 vs 多 pass。倾向多 pass（resolver 纯、coordinator 哑、crash-safe 不变）。
3. **reusable_avatar 的判定维度**：按 `retention_mode`（权威）还是 `resource_kind`？avatar_look/avatar_group 通常 reusable，但理论上可 ephemeral。当前按 `retention_mode='reusable_avatar'` 过滤（与 spec "行 retention_mode=reusable_avatar 跳过" 一致），不看 kind。**Codex 问题 #3**：是否还要按 kind 兜底。
4. **同一 op 多个 audio/portrait**：resolver 应支持 N 个（不假设各 1）。当前按 upload_id 字典序。若业务保证各 1，测试仍覆盖 N。
5. **asset resource 状态 vs asset upload 状态**：resolver 以 **resource deletion_status** 为准（resource 是删除生命周期载体）。asset upload status='deleted' 但 resource 未 deleted 是矩阵非法（claim/matrix fail-closed），resolver 不单独处理。
6. **op 存在但无任何 resource**：plan.entries=()（空）。coordinator no-op。✓
7. **video resource 存在但 retention=reusable?** video 都是 ephemeral（交付物）。但若数据非法（video+reusable），resolver 当前的 video 分支不查 retention（video 不被 reusable 过滤拦截）。是否要对 video 也查 retention？spec 只说 ephemeral video 删。**Codex 问题 #4**：video 是否也走 retention 过滤。倾向：video 也按 retention 过滤（reusable video 跳过），统一规则。
8. **确定性**：entries 按 (order_key, upload_id/resource_id) 严格排序，测试可断言精确序列。
9. **resolver 与 claim 的 eligibility 重复**：刻意不重复（c1 教训）。resolver 不读 download_status 来决定 video 是否"可删"——它只看 video resource 是否已 deleted 来决定 audio/portrait 门禁。verified 门禁完全交给 video claim。**Codex 问题 #5**：确认这个分工无漏洞（例如 resolver 把未 verified 的 video 放进 plan，coordinator claim 返回 not_ready，下 pass resolver 仍 [video] —— 正确，audio/portrait 继续等）。

## 6. 关键不变量（c2 不能放松）

- **纯读**：resolver 不写 DB，不开 claim，不删任何东西。
- **复用派生**：kind/retention 用 `_asset_resource_kind`/`_asset_retention_mode`，不自造映射。
- **顺序确定性**：同输入 → 同 plan（无随机/无时间依赖）。
- **§3.5 顺序**：正常模式 audio/portrait 绝不在 video 删除前进入 plan。
- **reusable 永不被自动删**：retention_mode=reusable_avatar 一律跳过。
- **未知 op 不抛**：返回空 plan（coordinator 安全 no-op）。

## 7. 给 Codex round-1 的问题

1. §5.1 force 是否完全排除 video？（当前：是）
2. §5.2 正常模式多 pass（resolver 等 video deleted）vs 单 pass 串行？
3. §5.3 reusable 判定按 retention_mode 还是 kind？
4. §5.7 video 是否也走 retention 过滤？
5. §5.9 resolver 不判 eligibility（verified 交给 claim）、只判"顺序+作用域"——这个分工有漏洞吗？
6. c2 边界（纯 resolve_deletion_plan_in_tx）是否自洽可独立锁？
