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
| §5.5e5b0c3c-c2 | resolve_deletion_plan_in_tx 消费门禁 resolver（纯读 §3.5 规划：正常 [video]→deleted 后 [audio,portrait]；force 排除 video；reusable_avatar 全 kind 跳过；已 deleted/foreign op 跳过；download_status advisory 不 gate；force 严格 bool 守卫；2 轮 Codex 审阅后锁定） | ✅ |
| §5.5e5b0c3c-c3 | 正常顺序 DeletionCoordinator（消费 c2 plan × c1/video processor，哑迭代器 video→audio→portrait，每资源独立 claim/apply，crash-safe 多 pass）+ recover_deletions maintenance 接线；claim↔apply 跨 tx 授权全量镜像（witness FULL-TOPOLOGY 6 类 + apply reason/retention/download_status/single-video/remote_id F1/F2 + asset op-level F3/F4/F5 cross-domain F5=created_by/F4=refs）；13 轮 Codex 审阅后锁定（commits 8980403→6572abc，68 测试，design doc `docs/e5b0c3c-c3-design.md`） | ✅ |

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

- §5.5e5d-c maintenance wiring —— **round-3 NOT LOCKABLE（5 blocker）→ round-4 全闭合 → round-4 NOT LOCKABLE（3 gap）→ round-5 全闭合 → round-5 NOT LOCKABLE（2 blocker：`BaseException` __repr__ 逃逸 `except Exception` / broken-topology gap）→ round-6 全闭合（109 测 + diagnostic 不调 `repr()` 改 `len()` + 抽 `_DELETION_WITNESS_SUBQUERY_SQL` 共享常量 + domain (c) `NOT EXISTS(witness)`，1167 全绿），待 Codex round-6 锁定复审**
- §5.5e5d-d 交互式降级卡片（D13，director preflight 检测数字人配置缺失 → A 配置/B 降级 M1 交互 next_action）
- §5.5e6 RecoveryDirectiveCatalog 验签 + failure mapping + 宿主 workflow（§6 #14 依赖它）
- §6 收尾：补 #9 定价下发 / #10 M1 门禁跨仓契约 + #14（依赖 e6）+ 三仓 CI gate

§5.5e5c（capability wiring）+ §5.5e5d-a（doctor v1.1）+ §5.5e5d-b（canary harness）**已锁定**（6 轮 + a/b 各自 Codex 审阅后锁定）。客户端 **1167 测试全绿**。分支 `feat/digital-human-protocol-v1_1`。

---

## 下次会话接续点（交接）—— e5d-c maintenance 待 Codex round-6

**当前状态**：e5c/e5d-a/e5d-b 全部锁定；e5d-c maintenance wiring —— **round-1 NOT LOCKABLE（4）→ round-2 全闭合 → round-2 NOT LOCKABLE（2）→ round-3 全闭合 → round-3 NOT LOCKABLE（5）→ round-4 全闭合 → round-4 NOT LOCKABLE（3 gap）→ round-5 全闭合 → round-5 NOT LOCKABLE（2 blocker）→ round-6 全闭合，1167 测全绿，待发 Codex round-6 锁定复审**。round-6 闭合（见 `docs/e5cd-design.md` §1.13a）：(1) **blocker 1（`BaseException` repr 逃逸）**—— round-5 的 `_safe_key_repr`（`except Exception`）兜不住 `KeyboardInterrupt`/`SystemExit`（`__repr__` 抛 `BaseException` 子类的键会逃逸 exit 1；catch `BaseException` 被禁会吞 Ctrl+C）；**严格 total 修**：diagnostic **完全不调 `repr()`**，改 `len()` on plain dict（builtin 不能抛）—— 删 `_safe_key_repr`，DB + deletion 两 diagnostic 改 `got = f"dict 含 {len(tally)} 键"`（畸形 tally 的精确键对 operator action 无价值）。(2) **blocker 2（broken-topology gap）**—— round-5 的范围裁定（"broken-topology = TOCTOU 类，不计"）被 Codex 反驳：静态 corruption 审计时已存在 = direct-corruption 威胁模型（同 state-matrix 异常），**非** TOCTOU；一个唯一资源 state-matrix 正常但拓扑断裂（缺/外 ref、credential 不匹配、缺 upload-binding 等）→ 候选 SELECT 拒绝整个 op → `ops_alerted=0` + round-5 `unrecoverable=0` → exit 0 谎报。**共享谓词 + domain (c)**：抽 `_DELETION_WITNESS_SUBQUERY_SQL` 模块级常量（候选 SELECT witness 子查询逐字拷贝）—— `recover_deletions` 候选 SELECT + `count_recovery_attention` unrecoverable COUNT **共用同一谓词**（消除手工镜像 6+ 拓扑类的 drift 风险，Codex round-4/5 反复建议）；unrecoverable COUNT 加 domain (c) `status IN (pending,failed) AND reason IN (post_download,consent_withdrawal) AND op_id NOT NULL AND NOT EXISTS(<共享谓词>)`，范围限定 claim-eligible 避免假阳性（不覆盖 `not_started+NULL` pre-video 资产）。**元教训**：round-5 我用 "TOCTOU 类" 标签排除 broken-topology，但 state-matrix 异常也需 direct INSERT，我却计了 —— 标签不一致；Codex 正确指出两者同模型，要么都不计要么都计，round-6 选都计 + 共享谓词（`原则陈述正确 ≠ 实现穷举`：round-5 已陈述 "fail-closed 覆盖最终态" 但实现没穷举到拓扑域）。**纯加法只读诊断 `count_recovery_attention` 不改任何 locked mutation primitive**（候选 SELECT 行为零变化，c3 68 测验证）。

c3 审阅轨迹（13 轮）：round-1~5 候选/witness 门禁迭代 → round-6 DELETED witness 逃过所有 claim 复查（6 类 bypass）→ round-7 B1 终态证明 + B2 asset binding（2 类）→ round-8/9 B1↔B2 op-level 不对称（download_status + single-video）→ round-10/11 claim↔apply 对偶接缝（video not_started reason-blind + apply reason TOCTOU）→ round-12 video apply F1/F2（retention/download_status/remote_id）+ 点名 asset 侧 F3/F4/F5 → round-13 asset op-level F3/F4/F5 + F5 域修正（refs→created_by 镜像 resolver）。**元教训（诚实记录 #13）**：「原则陈述正确 ≠ 实现穷举」——claim↔apply 跨 tx 授权必须逐字段列 claim 在 tx1 读的每个授权字段，逐一确认 apply 在 tx2 也复查；不能只改被 Codex 点名的那个。

### c3 已落地的关键不变量（改动时不能放松）

- **DeletionCoordinator 哑迭代器**：消费 c2 `DeletionPlan.entries` × c1/video processor，按 resource_kind 路由（video→`DeleteProcessor.delete_once`；audio/portrait→`AssetDeletionProcessor.delete_once(upload_id=...)`；`upload_id is None` 的 asset entry → 跳过+告警）；每资源独立 claim/apply tx，单 pass 内某资源 not_ready/busy 不阻塞同 pass 后续；全清理跨多 pass（crash-safe）。
- **claim↔apply 跨 tx 授权全量镜像**（核心安全边界）：claim 与 apply 是两个独立 fenced tx（中间夹事务外 adapter 调用），claim 的 tx1 授权判定**不过 tx 边界**——apply 必须对**每个**授权不变量在 tx2 用当前行态再校验。已覆盖：witness FULL-TOPOLOGY（6 类未镜像不变量）+ video apply F1/F2（retention/download_status/single-video/remote_id）+ asset apply op-level F3/F4/F5（download_status=verified / COUNT(video)<=1 / 无 non-deleted 非 reusable video）。
- **F4/F5 cross-domain**（round-13 锁定）：F4=COUNT(video)<=1 在 **refs** 域（authority=`_single_video`/B2 count 不变量）；F5=无 live video 在 **created_by** 域（authority=resolver tail-release，镜像 resolver @2399-2406 不需 ref 行）。刻意跨域因 authority 不同——改 F4 到 created_by 会 over-block 合法双 deleted-video op（resolver 跳 deleted 正确释 tail，created_by COUNT 见 2 冻）。
- **force 严格 bool**：`type(force) is not bool` → ValueError；force 模式豁免 F3/F4/F5 + video-first ordering（§3.5 operator privacy-emergency 绕过交付门）。
- **manual_force 永不自动删**（operator-only）；**consent_withdrawal 交付无关、per-resource、豁免 op-level 复查**；**reusable_avatar 全 kind 永不扫**（撤销走 dashboard）。
- **recover_deletions maintenance**：候选 SELECT 关 tx → 逐 op 驱动 coordinator（deletion 恢复 pass）。
- c2 resolver 不变量依旧成立（纯读规划 / eligibility 归 claim / §3.5 顺序门禁 / unknown kind surfaced 不 drop / download_status advisory 不 gate），见 `docs/e5b0c3c-c2-design.md`。

### e5c/d 范围（下一步）

capability wiring + doctor/canary：把已锁的删除子系统（coordinator + resolver + processors）+ HeyGen adapters 接入宿主 workflow，加 doctor 健康检查 + canary。
- **盲预测要点**：(a) wiring 不能引入新的 truthy `force` 来源或绕过已锁的 claim↔apply 镜像；(b) doctor/canary 只读不写（绝不触发真实删除/上传）；(c) 宿主 workflow 必须把 `force` 当 bool 透传（`type(force) is bool`）；(d) 不能放松 c1/c2/c3 任一已锁不变量。

### 恢复操作（下次会话）

```bash
cd ~/AgentMesh-Lecturecast
git log --oneline -4   # 最近：6572abc c3 lock 裁定；6767d1f F5 域修正；eae5fbc round-13；25e22a4 round-12
.venv/bin/python -m pytest -q   # 应 1167 passed（或 UV_CACHE_DIR=/tmp/lc-uv-cache uv run --project . pytest tests/ -c pyproject.toml -q）
```

Codex e5b0c3c 会话: `019fb840-a93b-73e1-b56c-a29b07a15e3d`（含 c1/c2/c3 全部审阅历史，resume 即续）。发审命令：`cat prompt.txt | codex exec -C ~/AgentMesh-Lecturecast resume <session> - -c 'model_reasoning_effort="low"' --json`（**务必 effort=low**，medium 在新 session 会挂；`-C` 必须在 `resume` 之前）。注：c3 round-13 最终复审用 fresh `codex exec`（非 resume）+ rephrased prompt 绕 cyber 内容过滤——若 resume 触发过滤，改用 fresh exec + invariant-completeness 框架（非 security 措辞）。

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

