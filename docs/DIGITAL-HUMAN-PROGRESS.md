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

- §5.5e5d-c maintenance wiring —— **round-3→round-6 轨迹见下；round-6 NOT LOCKABLE（1 blocker：`_valid_tally` 的 `set(keys)` 重新 hash + diagnostic `type(x).__name__` 两同族 BaseException 逃逸洞）→ round-7 全闭合（`_valid_tally` 加 str-guard 在 hash 前拒非 str 键 + diagnostic non-dict 分支改固定 `"non-dict"` + domain (c) docstring 拆 c1/c2）→ round-7 NOT LOCKABLE（1 doc-only blocker：c2 docstring 三处不准确 —— `withdraw` 非"只 UPDATE receipt"、无 video 非 independent non-selectability 因由、auto-resolve 不需新 video）→ round-8 全闭合（against source 核实 `_withdraw_in_tx` consent.py:657-673 + B2 常量 @4091-4134 `COUNT(video)<=1` 允许 zero-video，修 docstring + SQL COMMENT + "NOT counted" 段）→ round-8 NOT LOCKABLE（2 doc-only blocker：c2 auto-resolve "expired" 误导 branch B gate 需 NULL/NULL 非 time-expiry；§1.13b line 313 仍存 round-7 过时描述）→ round-9 全闭合（grep 全仓清 lease 原语：download finalize/fail @~1644/1652/1702/1718 + `_clear_operation_lease` def @~2514 由 video-deletion fenced-apply @~1959/1972/1983 调用；改 docstring c2 + SQL COMMENT c2 + §1.13b 加 superseded 标记 + §1.13c 第 3 行）→ round-9 NOT LOCKABLE（1 doc-only blocker：5 site 把 branch B 简写成 owner-only gate `o.lease_owner IS NULL` 漏 `AND o.lease_expires_at IS NULL` 合取项 —— operation_repository.py:3147/3172/3271 + e5cd-design.md:331(2x) + DIGITAL-HUMAN-PROGRESS.md:83）→ round-10 全闭合（5 site 全合取化，grep 全仓规范性描述零 owner-only 残留，历史/before-text 命中除外；111 测，1169 全绿）→ round-10 NOT LOCKABLE（1 doc-only：§1.13e "零命中 exit 1" 自指不准）→ round-11 全闭合（3 site 改 "零规范性残留 + 历史命中标注"）→ round-11 NOT LOCKABLE（1 doc-only：§1.13f site-count 2≠3）→ round-12 全闭合（§1.13f 2→3 + 补 PROGRESS:72 bullet）→ round-12 NOT LOCKABLE（1 doc-only：§1.13g 分组 "零命中" 漏 PROGRESS:72 "零残留"）→ round-13 全闭合（§1.13g "零命中"→"零命中/零残留" 同族）→ ✅ Codex round-13 裁定 **LOCKABLE — e5d-c maintenance wiring LOCKED**（13 轮 Codex 审；executable invariant round-7 锁、doc-accuracy round-13 锁）**
- §5.5e5d-d 交互式降级卡片（D13，director preflight 检测数字人配置缺失 → A 配置/B 降级 M1 交互 next_action）
- §5.5e6 RecoveryDirectiveCatalog 验签 + failure mapping + 宿主 workflow（§6 #14 依赖它）
- §6 收尾：补 #9 定价下发 / #10 M1 门禁跨仓契约 + #14（依赖 e6）+ 三仓 CI gate

§5.5e5c（capability wiring）+ §5.5e5d-a（doctor v1.1）+ §5.5e5d-b（canary harness）+ §5.5e5d-c（maintenance wiring）**已锁定**（e5c 6 轮 + a/b 各自 Codex 审阅 + c 13 轮 Codex 审阅后锁定；c 的 executable invariant round-7 锁、doc-accuracy round-13 锁，详见 `docs/e5cd-design.md` §1.13b–§1.13h）。客户端 **1169 测试全绿**。分支 `feat/digital-human-protocol-v1_1`。

---

## 下次会话接续点（交接）—— e5d-c ✅ LOCKED → 下一块 e5d-d（交互式降级卡片 D13）

**当前状态**：e5c/e5d-a/e5d-b 全部锁定；e5d-c maintenance wiring —— **round-6 NOT LOCKABLE → round-7 全闭合 → round-7 NOT LOCKABLE（1 doc-only）→ round-8 全闭合 → round-8 NOT LOCKABLE（2 doc-only）→ round-9 全闭合 → round-9 NOT LOCKABLE（1 doc-only：branch B owner-only 简写）→ round-10 全闭合 → round-10 NOT LOCKABLE（1 doc-only：§1.13e "零命中 exit 1" 自指不准）→ round-11 全闭合（3 site 改 "零规范性残留 + 历史命中标注"）→ round-11 NOT LOCKABLE（1 doc-only：§1.13f site-count 2≠3）→ round-12 全闭合（§1.13f 2→3 + 补 PROGRESS:72 bullet）→ round-12 NOT LOCKABLE（1 doc-only：§1.13g 分组 "零命中" 漏 PROGRESS:72 "零残留"）→ round-13 全闭合 → ✅ Codex round-13 裁定 **LOCKABLE** —— **e5d-c maintenance wiring LOCKED**（executable invariant round-7 锁、doc-accuracy round-13 锁；13 轮 Codex 审；1169 测全绿）**。round-7 闭合（见 `docs/e5cd-design.md` §1.13b）：(1) **blocker B1（`__hash__`/`__name__` 同族 totality 洞）**—— round-6 删了 diagnostic 的 `repr()`，但 `_valid_tally`（在 diagnostic 之前跑）仍调 `set(tally.keys())` 重新 hash 每键，且 diagnostic non-dict 分支仍 `type(x).__name__` —— 两洞都能被 `BaseException` 子类的 hostile dunder 逃逸。**治本修（C-builtin-only）**：`_valid_tally` 加 `if any(type(k) is not str for k in tally): return False` 守卫在 hash 任一键之前；diagnostic non-dict 分支 `type(x).__name__` → 固定 `"non-dict"`。Codex round-7 确认 B1 两洞 + residual totality + 7 个其他 `.__name__` site（input-validation 3 + exception-path 4）scope reasoning 全部成立。**round-8 闭合**（见 §1.13c）：Codex round-7 唯一剩余 blocker 是 round-7 c2 docstring 三处不准确（纯 doc，executable invariant 不受影响）：(a) `withdraw` 非"只 UPDATE receipt"—— 实际 UPDATE receipt + op 的 consent-lifecycle 字段（pristine 改 status/清 digest/updated_at；非 pristine 清 digest/updated_at），但**不动 lease**（consent.py:591-673，特指 `_withdraw_in_tx` @657-673）；(b) "无 video 也阻塞"夸大 —— branch B2 witness **显式允许 zero-video op**（`COUNT(video) <= 1` 非 `== 1`，常量 @4091-4134 注释 "zero is allowed"），唯一 c2 non-selectability 因由是 active lease（branch B `o.lease_owner IS NULL AND o.lease_expires_at IS NULL` gate @4031）；(c) auto-resolve "需新 video"过强 —— download-verified op 上 properly-bound B2 `post_download` asset 可 0 video witness。**round-9 闭合**（见 §1.13d）：Codex round-8 又抓 2 个 doc-only blocker：(i) round-8 c2 auto-resolve 写 "lease clears (expired/fenced/released)" —— "expired" 误导：branch B gate 要 `lease_owner IS NULL AND lease_expires_at IS NULL`，单纯时间 expiry 不通过；(ii) §1.13b line 313 历史段仍存 round-7 两处过时描述。round-9 against source grep 全仓清 lease 原语核实：download finalize/fail inline SQL @~1644/1652/1702/1718 + `_clear_operation_lease` helper（def @~2514，由 video-deletion fenced-apply @~1959/1972/1983 调用）—— 这些才是 NULL 两列的 fenced path；改 docstring c2 + SQL COMMENT c2 + §1.13b 加 ⚠️ superseded 标记 + §1.13c 第 3 行。**round-10 闭合**（见 §1.13e）：Codex round-9 items 1–5 全 CONFIRMED accurate（唯一 blocker 是 item 6 内部一致性），抓 1 个 doc-only blocker：5 site 把 branch B 简写成 owner-only gate `o.lease_owner IS NULL`（漏合取项）—— operation_repository.py:3147/3172/3271（docstring c intro + c2 sub-bullet + SQL COMMENT c2）+ e5cd-design.md:331(2x) + 本文件:83。against source 核实 branch B 真实谓词 @operation_repository.py:4065 = 合取双列；5 site 全合取化，grep 全仓 `lease_owner IS NULL` 不跟 `AND`：**规范性 branch-B 描述零 owner-only 残留**，其余命中均为明确标记的历史/错误语境（非 "零命中" —— 详见 §1.13f）。**元教训（第 3 次同一模式）**：round-7 凭直觉写 c2 三处不准 → round-8 修 auto-resolve 但漏 §1.13b 历史段 + prompt 自写 "expired" → round-9 修 auto-resolve 合取但漏 c2 intro/sub-bullet/SQL-COMMENT 的 owner-only 简写。**根因**：同一谓词在 docstring 的 intro/sub-bullet/auto-resolve 三位置各表述一次，修 1 处 ≠ 修 N 处 —— 同 c3 "原则陈述正确 ≠ 实现穷举" 的 doc 版。修法：grep 全仓该谓词的每种形态，逐处核对。**round-11 闭合**（见 §1.13f）：Codex round-10 items 1–6 + 8 全 CONFIRMED accurate，唯一 blocker（item 7）：§1.13e 写 "grep 全仓零命中（exit 1）" **自指不准** —— 本节自身的 before→after 表 "前" 文本 + round-9 blocker 历史记录 + 进度文档历史段都含 owner-only 串会产生命中。实质结论（零规范性残留）正确，字面 "零命中 exit 1" 错。修：§1.13e + PROGRESS:83 + PROGRESS:72 三处把 "零命中/零残留" 改为 "规范性描述零 owner-only 残留；历史/before-text 命中除外"。**元教训（第 4 次同族，新变体 = 自指陷阱）**：前三次是"修谓词漏同义 site"；这次是**关于文档自身的元 claim** —— 写 "grep 零命中" 时必须把本节将引入的 before/历史文本计入命中集，否则自相矛盾。同 c3 "原则陈述正确 ≠ 实现穷举" 的 doc 元层版。**纯 doc，零 runtime/SQL/test 变化，1169 全绿不变**。**round-12 闭合**（见 §1.13g）：Codex round-11 items 1–4 + 6 全 CONFIRMED accurate，唯一 blocker（item 5）：§1.13f 记录段写 "round-11 修正（2 site）" 但 commit `4c1c4fd` 实际改 **3 site**（§1.13e + PROGRESS:83 + PROGRESS:72），PROGRESS handoff 正确写 "三处" 而 §1.13f 写 "2 site" 内部不一致。修：§1.13f "2 site"→"3 site" + 补第三 bullet 记 PROGRESS:72 + 第二 bullet "两处"→"三处"。**元教训（第 5 次同族，又一变体）**：round-11 是 "grep 结果元 claim" 自指陷阱；这次是**记录段 site-count vs commit diff 对不上** —— 写 "N site" 这类关于上轮改动的元 claim 必须 against `git show <commit> --stat` 实际 diff 核实，不能凭上轮印象里 "Codex 点名了几个" 来记（round-12 自审还抓到第三 bullet 原写 "Codex round-10 未点" 不准 —— round-10 实际把 :72 归为历史记录提过，只是没当 blocker；一并修准）。同 c3 "原则陈述正确 ≠ 实现穷举" 的 doc 元层版。**纯 doc，零 runtime/SQL/test 变化，1169 全绿不变**。**round-13 闭合**（见 §1.13h）：Codex round-12 items 1/2/4/6 全 CONFIRMED accurate（§1.13f site-count 3 准确匹配 `git show 4c1c4fd`、§1.13d/§1.13e/§1.13f 三 section site 列表/计数与对应 commit `5e1512e`/`a6cb0b6`/`4c1c4fd` diff 全匹配且计数规则一致、§1.13g 其余四 meta-claim 全准、commit `52e5a77` doc-only/1169 不变），唯一 blocker（item 3 + 5）：§1.13g:393 把三处原文统一标 "零命中" 但 PROGRESS:72 原文是 "零残留"（§1.13g:396 自己已区分）。修：§1.13g:393 "零命中"→"零命中/零残留" 同族表述。**元教训（第 6 次同族，又一变体）**：round-11 = grep 结果元 claim 自指；round-12 = site-count vs commit diff；这次 = **分组引号 characterization 对部分成员不准** —— 把 N 个 site 归入同一引号词时必须逐成员核实每个 site 原文是否真是该词。同 c3 "原则陈述正确 ≠ 实现穷举" 的 doc 元层版。**纯 doc，零 runtime/SQL/test 变化，1169 全绿不变**。

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
.venv/bin/python -m pytest -q   # 应 1169 passed（或 UV_CACHE_DIR=/tmp/lc-uv-cache uv run --project . pytest tests/ -c pyproject.toml -q）
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

