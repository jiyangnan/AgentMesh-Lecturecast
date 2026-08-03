# §5.5e6-client 设计稿 — RecoveryDirectiveCatalog 客户端消费（vendor + 验签 + failure_kind 映射 + workflow）

> 子步：§5.5e6-server（schema/registry/signing + step 4a/4b/step 5）**已锁**（commit `0ff4365`）。本步 = **client 侧全块**：vendor RecoveryDirectiveCatalog schema → 验签 → error→failure_kind 确定性映射 → 按 §7.4 emit `workflow.next_action`。§6 #14 contract test 依赖本步。
> 规格：`lecturecast-server/docs/DIGITAL-HUMAN-TECH-SPEC.md` v1.4 §7.3（预签两层下发）/ §7.4（Host Conformance Contract）/ §7.5（版本化 StableId + catalog-driven 通用 client）/ §0 Principle 6（provider 状态永不上传）。
> 工作流：调研 DONE（本会话后台 Explore + 逐条 against source 核实，见 §1）→ **本文 = 盲预测+设计稿** → 实现+测试（RED-first）→ Codex review → lock。
> 不变量（铁，承 server 已锁）：fail-closed / 宁可少报绝不虚报；§0 Principle 6 —— provider 状态永不上传；catalog 预签 + advisory（非计费门禁）。

---

## §0 step 契约（盲预测后修正版）

**上步产出**（server，已锁）：`RecoveryDirectiveCatalog` schema（protocol/v1.1/recovery-directive-catalog.schema.json，digest `240feda2`）+ base catalog（5 M1 directives，`is_main_blocker=true`）+ provider catalog（3 HeyGen side directives，`is_main_blocker=false`），全部 Ed25519 预签。base catalog 随 `DirectorSessionOutV1_1`（step 4a）与 resume V1_1 generation 投影（step 5）下发。

**本步调研发现（§1）**：

1. **client 无 recovery-directive-catalog schema**（v1.1 lock 8 文件无此文件），且 **manifest-generation-out vendored 副本过期**（client `6d93b77d` vs server `789f42a9`——server 已含 `recovery_catalog` 字段，client 未同步）。→ 需重新 vendor v1.1 bundle。
2. **client 有完整验签基础设施可复用**：`verify_manifest`（manifest.py:133，Ed25519 + keyring）+ `manifest_signing_bytes`（protocol/canonical.py:41，签名 payload 去掉 `signature.value`）+ `PublicKeyRing`（内置 `signing-keyring.json`，key_id `lecturecast-prod-202607-v1`）。
3. **server 端 key_id 是配置化的**（`manifest_signing_key_id`，生产 `lecturecast-prod-202607-v1`）。**生产配对成立**：server 用 `lecturecast-prod-*` key 签名 → client keyring 信任同前缀 key（`validate_for_release` 只信 `lecturecast-prod-`）。测试场景 server 用任意 key_id，client keyring 需可注入（与 `verify_manifest(keyring=...)` 同构）。
4. **error→failure_kind 映射现无**：`_error`（director.py:213）只把 ErrorEnvelope code 透传成 `LectureCastError.code`；`_resume_error_workflow`（commands/director.py:1251）按 code+http_status → phase。**无 failure_kind 中间层，无 catalog 查询，无 directive 消费。**
5. **`next_action` 现有三 kind**（command / host_choice / stop）。**§7.4 的"转述+options+推荐+steer_back"无预留**——但 `_d13_decision_action`（director.py:904）的 host_choice 形态是直接先例。
6. **⚠️ 关键发现（本会话 against source 核实）**：client frozen state schema（v1.0/v1.1/v1.2）**不持久化 `recovery_catalog`**——`DirectorStateStore.update()`（director.py:698）只从 session/generation 响应抽取 billing 指纹（billing_state/resume_available/billing_updated_at），把 catalog 丢弃。**错误时（generation-resume 的 except 块）本地无 catalog 可用**。且 base catalog 的 failure_kind 大多数是**本地 M1 失败**（`local_renderer_missing`/`own_voice_no_f5_no_stock`/`m1_schema_unsupported`/`m1_manifest_signing_failed`），只有 `m1_insufficient_credits` 对 server error code `insufficient_credits`（402）。→ **#121 wiring 需 v1.3 state 迁移持久化 catalog + 本地失败映射**（#120 不受影响）。

**∴ 本步契约（修正）**：五件事——①重新 vendor v1.1 bundle（+recovery-directive-catalog.schema.json，更新 manifest-generation-out）；②`RecoveryDirectiveCatalog` model（schema 校验 + `_validate_semantics`）；③`verify_recovery_catalog_signature`（复用 `verify_manifest` 的 keyring/Ed25519/`manifest_signing_bytes` 逻辑，注入 keyring）；④`recover_from_failure(failure_kind, catalog)` → §7.4 workflow；⑤接线进 `commands/director.py`（resume/generate 错误路径，发现 catalog 时 emit `recovery_directive_required` phase + host_choice action）。

四条硬约束（承 server 已锁，本步新增语义）：

1. **fail-closed 不消费未验签 catalog**：验签失败 → `LectureCastError(code="manifest_signature_invalid")`，绝不演未授信 directive 话术。
2. **§0 Principle 6 永不上传**：client 只本地映射 + 演话术；任何响应载荷不带 provider 状态。
3. **advisory 非计费**：directive 只是话术剧本；fallback 选项须用户明确批准（host_choice `requires_user_approval=True`），不自动执行。
4. **通用 client（catalog-driven）**：failure_kind 查表用 **exact-match fallback**——catalog 无该 failure_kind 时返回 None（上层走既有 `_resume_error_workflow` / generic fail），不硬编码新 failure_kind。

---

## §1 调研 grounding（file:line 锚点，逐条 against source 核实）

### §1.1 protocol vendor 机制（client）

- `src/lecturecast/protocol/`：`__init__.py` / `models.py` / `canonical.py` / `update.py` / `protocol.lock` / `schemas/{flat, v1.1/}`。
- `protocol.lock` 格式：`{"bundle_digest","bundle_version","files":{name→sha256},"generated_by"}`（`schemas/v1.1/protocol.lock:1-24`）。v1.1 bundle vendor **8 文件**：client-capabilities / creative-brief / decision-card-set / error-envelope / manifest-generation-out / orchestration-plan / presenter-plan / production-manifest（`schemas/v1.1/protocol.lock:8-17`）。**无 recovery-directive-catalog.schema.json**。
- 双版本分派：`models.py:353-374 documents_for_protocol_version()` + `models.py:245 _V1_1_SCHEMA_DIR`，v1.1 模型经 `schema_dir` ClassVar 指向 `schemas/v1.1/`（models.py:250）。
- vendor 脚本：`scripts/update_protocol.py:1-21` → `protocol/update.py:36 update_protocol(source, schema_dir, lock)`，先校验 source lock `bundle_digest` 与逐文件 sha256 再原子写（update.py:53-58）。**CLI 默认参数是 v1.0 的 schema_dir/lock**；v1.1 vendor 需显式传 `--schema-dir src/lecturecast/protocol/schemas/v1.1 --lock .../v1.1/protocol.lock`。
- 对比验证：server `app/protocol/export.py` 用 `model.model_json_schema()` + `canonical_digest(file_digests)` 生成 lock；client `tests/test_protocol_contract.py:51-58` 用同一 `canonical_digest` 校验 vendored lock 与磁盘字节一致。§6 #14 contract test 将锁 server↔client lock 逐字节一致。

### §1.2 验签基础设施（client，可复用）

- `manifest.py:17 KEYRING_PATH = signing-keyring.json`（随包内置）；`manifest.py:53 PublicKeyRing`（load/get/validate_for_release，只信 `lecturecast-prod-` key）。
- `manifest.py:133 verify_manifest()`：keyring 加载 → key_id/status/algorithm/时间窗校验 → `Ed25519PublicKey.verify(signature_bytes, manifest_signing_bytes(document))` → 失败 raise `LectureCastError(code="manifest_signature_invalid")`。
- `protocol/canonical.py:41 manifest_signing_bytes(value)`：从 `signature` dict 去掉 `value` 键 → `canonical_bytes`（sort_keys、compact separators、ensure_ascii=False）。**与 server `app/protocol/canonical.py:38-49` 逐字同构**。
- `protocol/canonical.py:19-38 canonical_bytes/canonical_digest`：同 server。
- `src/lecturecast/signing-keyring.json:1-18`：key_id `lecturecast-prod-202607-v1`，Ed25519，base64 public_key（32 字节）。

### §1.3 server 契约锚点（schema 结构）

- `lecturecast-server/protocol/v1.1/recovery-directive-catalog.schema.json`：顶层 `{catalog_version, directives, signature}` 全 required。directive required：`failure_kind` / `is_main_blocker` / `user_message` / `options` / `steer_back_line` / `signature`；optional `external_handoff` / `do_not`。`options` 1-3 项含 `option_id` / `label` / `recommended` / `resume_action`（RecoveryAction：`action_id` enum 6 值 + `args`≤8）。SignatureMetadata：`algorithm:const"Ed25519"` / `key_id` / `value`（88-char base64，`[A-Za-z0-9+/]{86}==`）。
- `recovery_catalog` 在 manifest-generation-out.schema.json:980-994 是 `anyOf [{$ref:#/$defs/RecoveryDirectiveCatalog}, null]`，**内联 def**（mgo 平铺全部 $defs）；**独立 catalog schema 自带嵌套 $defs**。两者顶层 `properties/required/type/additionalProperties` 完全一致（本会话逐键 diff 确认），`$ref` 均文件内解析 → **独立 vendoring 可行**。

### §1.4 server base/provider directives（failure_kind 全集）

- `app/recovery_catalog.py:94-180 _base_directives`：5 个 M1 directives —— `m1_insufficient_credits` / `m1_manifest_signing_failed` / `m1_schema_unsupported` / `local_renderer_missing` / `own_voice_no_f5_no_stock`，全 `is_main_blocker=True`。
- `app/recovery_catalog.py:204-263` provider：3 个 side directives —— `heygen_key_invalid` / `heygen_balance_insufficient` / `heygen_rate_limited`，全 `is_main_blocker=False`。

### §1.5 error→workflow 现状（client）

- `director.py:213-257 _error()`：ErrorEnvelope → `LectureCastError(code/message/next_action/retryable/http_status)`；v1.1 走 `ErrorEnvelopeV1_1.model_validate`（director.py:228-229）。
- `commands/director.py:1251-1305 _resume_error_workflow()`：402+insufficient_credits→`credit_top_up_required`；409+generation_in_progress→`billing_refresh_required`；409→`generation_blocked`；404→`generation_unavailable`；≥500 retryable→`generation_recovery_required`；其余 None（generic fail）。返回 `{phase, policy, next_action}`。
- `next_action` 三 kind：`command`（director.py:114-130 `_command_action`）/ `host_choice`（director.py:209-227 / `_d13_decision_action` @904）/ `stop`（`{"id":"workflow.stop","kind":"stop","mutates":False}`）。
- **无 failure_kind 中间层，无 catalog 查询**。`canary.py:559/613` 标注 "§5.5e6 out of scope"。

### §1.6 RecoveryDirectiveCatalog 现有引用

- 仅 3 处，全 out-of-scope 标注：`canary.py:559` / `canary.py:613` / `test_canary.py:235`。client 无 vendored 副本。

---

## §2 盲预测设计抉择

### §2.1 抉择 D1：重新 vendor 整个 v1.1 bundle vs 只加 recovery schema

| 方案 | 扰动 | 取舍 |
|------|------|------|
| **A. 从 server canonical 重新 vendor 整个 v1.1 bundle**（`update_protocol(server/protocol/v1.1, ...)`，一次更新 8→9 文件含 manifest-generation-out 789f42a9 + 新增 recovery-directive-catalog）（**采纳**） | lock digest 全变（预期）；`manifest-generation-out.schema.json` 从 6d93b77d→789f42a9（含 recovery_catalog）；`ManifestGenerationOutV1_1.model_validate` 现在能收含 recovery_catalog 的响应 | 采纳：与 server 保持一致，v1.1 bundle 自洽；manifest-generation-out 本身已过期，本就该同步 |
| B. 只把 recovery-directive-catalog.schema.json 拷进 v1.1/ 不动 lock | lock 缺文件 → `test_protocol_lock_covers_exact_schema_bytes` 型校验失败 | 否决：lock 必须原子覆盖全部 vendored 文件 |

→ **采纳 A**：从 server canonical 重新 vendor v1.1 bundle。`update_protocol` 已按 lock 校验 source 完整性 + 原子写，直接复用。**v1.0 bundle 零变化**（server v1.0 lock 未动）。

### §2.2 抉择 D2：catalog 消费是"解析+验签"vs"验签+映射+workflow"

| 方案 | scope | 取舍 |
|------|-------|------|
| **A. `RecoveryDirectiveCatalog` model + `verify_recovery_catalog_signature` + `recover_from_failure`（纯函数，含映射+workflow）+ director 接线**（**采纳**） | 一次交付完整消费链（§0 契约五件事） | 采纳：e6-client 定义即"消费"，拆两子步反而割裂验签/映射/演话术 |
| B. 只交付 model + 验签，映射/workflow 留后续 | 半块 | 否决：映射是消费的核心，split 无意义 |

→ **采纳 A**。

### §2.3 抉择 D3：`recover_from_failure` 的输入（failure_kind 从哪来）

error→failure_kind 映射是"每次调用时的确定性映射"，**不持久化 catalog**。两种来源：

| 来源 | 说明 | 取舍 |
|------|------|------|
| **A. `failure_kind` 由调用方显式传（director 错误路径把 `LectureCastError.code` 确定性映射成 failure_kind）**（**采纳**） | 消费链分层：director 把 error code→failure_kind，`recover_from_failure(failure_kind, catalog)` 查表演话术。**catalog 无该 failure_kind → 返回 None**（上层走既有 path） | 采纳：与 tech spec §7.4 一致（"adapter 本地把报错确定性映射成 failure_kind，从对应 catalog 取 directive 演"）；分层清晰，可测 |
| B. `recover_from_failure(error, catalog)` 内部做 error→failure_kind 映射 | 耦合 director 错误分类与 catalog 消费 | 否决：error 分类是 director 已有职责（_error 已产 code），不必塞进纯函数 |

→ **采纳 A**：映射表放 `commands/director.py`（或复用文件），`recover_from_failure` 只做 `catalog.directives.get(failure_kind)`。

### §2.4 抉择 D4：验签复刻 `verify_manifest` vs 抽通用

| 方案 | 复用 | 取舍 |
|------|------|------|
| **A. 新增 `verify_recovery_catalog_signature(catalog, *, keyring=None)`，逻辑复刻 verify_manifest（keyring/Ed25519/时间窗/manifest_signing_bytes），失败 raise 同 code**（**采纳**） | 复用 `manifest_signing_bytes` + `PublicKeyRing` + 相同错误码；不动已锁 verify_manifest | 采纳：catalog 无 `created_at`（schema 无此字段）→ **不做时间窗校验**（verify_manifest 的时间窗绑定 created_at，catalog 没有）；key_id/status/algorithm 校验照搬 |
| B. 泛化 verify_manifest 支持任意 payload | 改已锁函数签名/语义 + 已锁测试 | 否决：catalog 无 created_at，泛化需引入"无时间窗"分支，扰动已锁 verify_manifest |

→ **采纳 A**：独立 `verify_recovery_catalog_signature`，复用 `manifest_signing_bytes` / `PublicKeyRing` / 错误码，**不做时间窗校验**（catalog schema 无 created_at）。

### §2.5 抉择 D5：director 接线点

| 方案 | 触发路径 | 取舍 |
|------|---------|------|
| **A. `generation_resume` 错误路径 + `generate` 错误路径**：catch `LectureCastError` 后，先把 error.code→failure_kind，查 session/generation 里的 `recovery_catalog`（若验签通过），命中则 emit `recovery_directive_required` workflow，否则落回 `_resume_error_workflow`/`fail`（**采纳**） | 两处最可能的 M1/M2 失败点；base catalog 随 session/generation 下发 | 采纳：最小真实接线，覆盖 M1（base catalog）场景；M2 provider catalog 通道未建（server 无 M2 交付通道），provider directives 暂不可达——诚实标注 non-goal |
| B. 所有 `_error` 路径统一接 | 每个 LectureCastError 都查 | 否决：大部分 error 无 catalog 语义（preflight/probe），且 catalog 只在 session/generation 响应里 |

→ **采纳 A**：接线 `generation_resume` + `generate` 错误路径。**provider catalog 暂不可达**（server 无 M2 通道），本步 base catalog 消费为主。

---

## §3 不变量（实现 + Codex 审）

1. **vendor 自洽**：v1.1 lock digest == server lock digest（§6 #14 锁）；v1.0 bundle 零变化；`ManifestGenerationOutV1_1.model_validate` 能收含 recovery_catalog 的响应。
2. **fail-closed 不消费未验签 catalog**：`verify_recovery_catalog_signature` 失败 → `LectureCastError(code="manifest_signature_invalid")`，上层不演 directive 话术。
3. **§0 Principle 6 永不上传**：client 只本地映射 + 演话术；任何响应载荷不带 provider 状态。
4. **advisory 非计费 + 用户批准**：host_choice `requires_user_approval=True`；不自动执行 fallback（未经批准不得把 own_voice 改 stock）。
5. **通用 client（catalog-driven）**：catalog 无该 failure_kind → 返回 None，上层走既有 path；**不硬编码新 failure_kind**。
6. **is_main_blocker 语义**：`is_main_blocker=false` → 一条消息转述+options+推荐+steer_back（§7.4 #2）；`is_main_blocker=true` → 不宣称主线可继续，仅给修复/改 Brief/显式接受 fallback 三类选项（§7.3 true directive 规则）。
7. **已锁函数零改动**：`verify_manifest` / `_resume_error_workflow` / `_command_action` / `_session_workflow` 等已锁函数**完全不变**；只新增 + 在错误路径加前置 hook。

---

## §4 实现 map（RED-first）

### §4.1 vendor v1.1 bundle

- `cd AgentMesh-Lecturecast && .venv/bin/python scripts/update_protocol.py --source /Users/ferdinandji/lecturecast-server/protocol/v1.1 --schema-dir src/lecturecast/protocol/schemas/v1.1 --lock src/lecturecast/protocol/schemas/v1.1/protocol.lock`
- 预期：v1.1 lock 8→9 文件（+recovery-directive-catalog.schema.json）；manifest-generation-out 6d93b77d→789f42a9；lock digest 更新。
- **v1.0 不重 vendor**（server v1.0 未动）。

### §4.2 `src/lecturecast/protocol/models.py` 加 `RecoveryDirectiveCatalog` model

```python
@dataclass(frozen=True)
class RecoveryDirectiveCatalog(ProtocolDocument):
    """Pre-signed recovery-directive catalog (tech spec §7.3). Delivered with
    the v1.1 session (DirectorSessionOutV1_1.recovery_catalog) and the v1.1
    generation view (ManifestGenerationOutV1_1.recovery_catalog). The client
    maps a local failure to a failure_kind, looks it up here, and presents the
    directive's message/options/steer_back (Host Conformance Contract §7.4)."""

    schema_dir: ClassVar[Path] = _V1_1_SCHEMA_DIR
    schema_filename: ClassVar[str] = "recovery-directive-catalog.schema.json"

    @classmethod
    def _validate_semantics(cls, payload: dict[str, Any]) -> None:
        # Defense-in-depth: directives object keys MUST be valid failure_kind
        # strings that match their directive's failure_kind (self-consistency).
        directives = payload.get("directives")
        if not isinstance(directives, dict):
            return
        for key, directive in directives.items():
            if not isinstance(directive, dict):
                continue
            directive_failure_kind = directive.get("failure_kind")
            if isinstance(directive_failure_kind, str) and directive_failure_kind != key:
                raise ProtocolValidationError(
                    f"directives key {key!r} does not match failure_kind {directive_failure_kind!r}"
                )
```

- `__init__.py` 导出 `RecoveryDirectiveCatalog` + `documents_for_protocol_version("1.1")` 表加 `"recovery_catalog": RecoveryDirectiveCatalog`。

### §4.3 `src/lecturecast/manifest.py` 加 `verify_recovery_catalog_signature`

```python
def verify_recovery_catalog_signature(
    catalog: RecoveryDirectiveCatalog | dict[str, Any],
    *,
    keyring: PublicKeyRing | None = None,
) -> VerificationResult:
    """Verify the Ed25519 signature on a RecoveryDirectiveCatalog. Mirrors
    verify_manifest() but WITHOUT the created_at time-window check (the catalog
    schema has no created_at). Failures raise LectureCastError with the same
    manifest_signature_invalid code — never return a signed-looking result for
    an unsigned/tampered catalog (fail-closed, §3 invariant 2)."""
    # same try: import cryptography ... except ImportError
    document = (
        catalog if isinstance(catalog, RecoveryDirectiveCatalog)
        else RecoveryDirectiveCatalog.model_validate(catalog)
    )
    payload = document.model_dump()
    signature = payload["signature"]
    try:
        trusted_keyring = keyring or PublicKeyRing.load()
    except (OSError, ValueError, json.JSONDecodeError):
        raise LectureCastError(code="manifest_signature_invalid", ...)
    key = trusted_keyring.get(signature["key_id"])
    if key is None or key.status not in {"current", "previous"}:
        raise LectureCastError(code="manifest_signature_invalid", ...)
    if signature["algorithm"] != key.algorithm or key.algorithm != "Ed25519":
        raise LectureCastError(code="manifest_signature_invalid", ...)
    # NO created_at time-window check (catalog has no created_at).
    try:
        public_bytes = base64.b64decode(key.public_key, validate=True)
        signature_bytes = base64.b64decode(signature["value"], validate=True)
        Ed25519PublicKey.from_public_bytes(public_bytes).verify(
            signature_bytes, manifest_signing_bytes(document),
        )
    except (InvalidSignature, ValueError, binascii.Error):
        raise LectureCastError(code="manifest_signature_invalid", ...)
    return VerificationResult(valid=True, key_id=key.key_id,
                              key_status=key.status,
                              manifest_digest=canonical_digest(document))
```

### §4.4 `src/lecturecast/recovery.py`（新文件）— `recover_from_failure`

```python
from __future__ import annotations

from typing import Any

from .protocol import RecoveryDirectiveCatalog


def recover_from_failure(
    failure_kind: str,
    catalog: RecoveryDirectiveCatalog | dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Look up a directive for a failure_kind in a RecoveryDirectiveCatalog.
    Returns None when the catalog is missing/None or has no directive for this
    failure_kind (catalog-driven client: never hard-code new failure_kinds,
    §3 invariant 5). The caller is responsible for signature verification
    BEFORE this lookup (fail-closed: never act on an unverified catalog)."""
    if catalog is None:
        return None
    document = (
        catalog if isinstance(catalog, RecoveryDirectiveCatalog)
        else RecoveryDirectiveCatalog.model_validate(catalog)
    )
    directives = document.payload.get("directives") or {}
    if not isinstance(directives, dict):
        return None
    directive = directives.get(failure_kind)
    if not isinstance(directive, dict):
        return None
    return {
        "failure_kind": directive["failure_kind"],
        "is_main_blocker": directive["is_main_blocker"],
        "user_message": directive["user_message"],
        "options": directive["options"],
        "steer_back_line": directive["steer_back_line"],
        "external_handoff": directive.get("external_handoff"),
        "do_not": directive.get("do_not"),
    }
```

### §4.5 `src/lecturecast/commands/director.py` 接线（错误路径 hook）

在 `generation_resume`（@1233）与 `generate`（@~1053 digital_human_decision_required 附近）的 `except LectureCastError` 块前加 helper：

```python
def _recovery_workflow(
    error: LectureCastError, catalog: dict[str, Any] | None, root: str,
) -> dict[str, Any] | None:
    """Present a recovery directive for a failure_kind if the catalog has one.
    Returns None when the catalog is missing/unverified/no-match (caller falls
    back to _resume_error_workflow / generic fail). The catalog is verified
    HERE (fail-closed): an unverified catalog never drives a directive."""
    if catalog is None:
        return None
    from ..manifest import verify_recovery_catalog_signature
    try:
        verify_recovery_catalog_signature(catalog)
    except LectureCastError:
        return None  # unverified → never present directive 话术
    from ..recovery import recover_from_failure
    directive = recover_from_failure(error.code, catalog)
    if directive is None:
        return None
    is_main_blocker = directive["is_main_blocker"]
    if is_main_blocker:
        return {
            "phase": "main_blocker_recovery_required",
            "policy": "execute_only_returned_next_action",
            "next_action": {
                "id": "director.recovery.decide",
                "kind": "host_choice",
                "question_id": f"recovery_{directive['failure_kind']}",
                "question_label": directive["user_message"],
                "options": [
                    {
                        "id": opt["option_id"],
                        "label": opt["label"] + ("（推荐）" if opt["recommended"] else ""),
                    }
                    for opt in directive["options"]
                ],
                "argv_template": [
                    "lecturecast", "director", "recovery", "decide",
                    root, "--choice", "<option_id>", "--json",
                ],
                "mutates": True,
                "requires_user_approval": True,
                "steer_back_line": directive["steer_back_line"],
                "do_not": directive.get("do_not") or [],
            },
        }
    # is_main_blocker=false → one message: 转述→options→推荐→steer_back (§7.4 #2)
    return {
        "phase": "recovery_directive_required",
        "policy": "execute_only_returned_next_action",
        "next_action": {
            "id": "director.recovery.decide",
            "kind": "host_choice",
            "question_id": f"recovery_{directive['failure_kind']}",
            "question_label": directive["user_message"],
            "options": [
                {
                    "id": opt["option_id"],
                    "label": opt["label"] + ("（推荐）" if opt["recommended"] else ""),
                }
                for opt in directive["options"]
            ],
            "argv_template": [
                "lecturecast", "director", "recovery", "decide",
                root, "--choice", "<option_id>", "--json",
            ],
            "mutates": True,
            "requires_user_approval": True,
            "steer_back_line": directive["steer_back_line"],
            "do_not": directive.get("do_not") or [],
        },
    }
```

调用点：`generation_resume` 的 except 块——`catalog = state.generation.get("recovery_catalog") if isinstance(state.generation, dict) else None`，先试 `_recovery_workflow`，None 再落回 `_resume_error_workflow`。

> ⚠️ **接线范围诚实标注**：`state.generation` 是 resume 拿到的 generation dict；base catalog 在 v1.1 resume 响应的 `recovery_catalog` 字段（server step 5 已锁）。**provider catalog 不可达**（server 无 M2 交付通道），本步只消费 base catalog。`generate` 路径的 catalog 在 session 响应（step 4a）——是否接线取决于 generate 是否在错误路径有 session dict 可用；若不可达则本步只接 resume，generate 留标注。

---

## §5 tests-before-impl（RED-first）

> 新文件 `tests/test_recovery_catalog_client.py` + 扩展 `tests/test_protocol_contract.py`（vendor 自洽一测）。

| # | 测试 | 断言 |
|---|------|------|
| F-C1 | `update_protocol` 重 vendor v1.1 后：lock 含 recovery-directive-catalog + digest == server lock | vendor 自洽（§3-1） |
| F-C2 | `RecoveryDirectiveCatalog.model_validate` 合法 catalog → round-trip 相等 | model 可解析 |
| F-C3 | directives key ≠ directive.failure_kind → `ProtocolValidationError` | 语义自洽校验 |
| F-C4 | `verify_recovery_catalog_signature`（真签，注入配对 keyring）→ valid=True | 验签通过 |
| F-C5 | 篡改 user_message → `LectureCastError(code="manifest_signature_invalid")` | fail-closed 验签 |
| F-C6 | keyring 无该 key_id / status revoked → 同 code 拒绝 | 未知/撤销 key 拒 |
| F-C7 | `recover_from_failure` 命中 → 返回 directive dict（含 is_main_blocker/user_message/options/steer_back_line） | 查表命中 |
| F-C8 | `recover_from_failure` catalog None / 无该 failure_kind → None | catalog-driven 通用 |
| F-C9 | `_recovery_workflow` 验签失败 → None（不演话术） | fail-closed 接线 |
| F-C10 | `_recovery_workflow` is_main_blocker=false → phase=`recovery_directive_required` + host_choice（question_label==user_message, options 带推荐标记, steer_back_line） | §7.4 一条消息 |
| F-C11 | `_recovery_workflow` is_main_blocker=true → phase=`main_blocker_recovery_required` + host_choice requires_user_approval=True | §7.3 true 规则 |
| F-C12 | generation_resume 错误路径：catalog 命中 → emit recovery_directive_required workflow | 接线行为层 |
| F-C13 | generation_resume 错误路径：catalog None/验签失败/无匹配 → 落回 `_resume_error_workflow` | 无回归 fallback |
| F-C14 | v1.0 bundle lock digest 零变化（golden） | v1.0 冻结 |

⚠️ **测试数据**：用测试生成的 Ed25519 私钥签名一个 catalog fixture（或复用 server 测试模式 `Ed25519PrivateKey.generate()` + 同款 `manifest_signing_bytes`）。keyring 注入：构造含配对 public_key 的 `PublicKeyRing`，`verify_recovery_catalog_signature(catalog, keyring=...)`。

---

## §6 刻意不做（non-goals，留后续）

- ❌ **provider catalog 消费** —— server 无 M2 交付通道，provider directives（heygen_key_invalid 等）不可达。M2 wire 接线是 server step 4c 的前置。
- ❌ **director recovery decide 命令落地**（host 填 `<option_id>` 后执行 resume_action）—— 本步只 emit host_choice 卡片，实际 action 执行（`continue_without_presenter` 等）留后续子步（需接 PresenterPlan/consent 链）。
- ❌ **持久化 catalog**（cache/DB 存储）—— 每次调用时从响应读、验签、查表，不落盘。
- ❌ **改 verify_manifest / _resume_error_workflow / _command_action / 已锁函数**。
- ❌ **改 v1.0 bundle / v1.0 模型**。
- ❌ **硬编码新 failure_kind**（catalog-driven，§7.5）。

---

## §7 实现顺序

1. ✅ vendor v1.1 bundle（§4.1）—— `update_protocol.py` re-vendor，bundle_digest `960d9855…` 与 server lock 字节一致；v1.0 bundle 零改动。
2. ✅ `models.py` 加 `RecoveryDirectiveCatalog` + `__init__.py` 导出（§4.2）—— 含 directives key↔failure_kind 语义校验；`documents_for_protocol_version("1.1")` 注册 `recovery_catalog`。
3. ✅ `manifest.py` 加 `verify_recovery_catalog_signature`（§4.3）—— 镜像 verify_manifest 但 **无 created_at 时间窗检查**（catalog schema 无 created_at）。
4. ✅ 新 `recovery.py`：`recover_from_failure` + `failure_kind_for_error`（§4.4）—— server code→base failure_kind 显式映射 + 目录驱动的透传（`or error.code`）。
5. ✅ `commands/director.py`：`_recovery_workflow` + 错误路径接线（§4.5）—— resume 错误路径先试 recovery workflow，None 落回 `_resume_error_workflow`；`keyring` 可注入（测试）。
6. ✅ tests F-C1..F-C14（§5，RED-first）—— #120 已落地 **F-C1/F-C2/F-C3 + mgo re-vendor 检查 + v1.0 golden**（9 测），F-C4..F-C14 归 #121。
7. ✅ 全量测试（**1224 passed**，基线 1191 + #120 新增 9 + mgo/#120 fix 修复 + #121 新增 16）—— commit + Codex round-1（round-1 修复后 **1230 passed**）。

---

## §8 Codex round 记录（实现后回填）

### #120（vendor + parse）— round-1 Codex

**发现 8 项，全部裁决：**

| # | 级别 | 发现 | 裁决 | 处理 |
|---|------|------|------|------|
| 1 | **High** | mgo 内嵌 catalog 绕过语义校验：`ManifestGenerationOutV1_1._validate_semantics` 未委托 `RecoveryDirectiveCatalog._validate_semantics`，key↔failure_kind 只在校验独立解析路径 | **确认 real**（schema 校验覆盖 shape，语义层有缝；#122 接 resume 路径即暴露） | 已修：mgo 语义校验对 dict 型 `recovery_catalog` 委托 catalog 语义校验；补 `test_mgo_embedded_catalog_rejects_key_mismatch` |
| 2 | Med | args/external_handoff 值无 `_reject_unsafe_json` 防御（`javascript:`/绝对路径可干净通过） | **确认 real**（tech spec §7.3 v1.4 #8：per-action args 禁透传 subprocess/shell/URL；server 注释明示"client validates per-action"） | 已修：args 值与 external_handoff 跑 `_reject_unsafe_json` + 新增 scheme allowlist（`_reject_unsafe_handoff_scheme`，仅 https 白名单） |
| 3 | Med | option_id 唯一性未校验（schema 无法表达，其它模型用 `_ensure_unique`） | **确认 real**（#122 decide 映射歧义） | 已修：`_ensure_unique(option_id)` + 测试 |
| 4 | Low | 多 `recommended: true` 未约束 | **确认 real**（server 每 directive 至多一个推荐） | 已修：>1 拒绝 + 测试 |
| 5 | Low | `documents_for_protocol_version("1.1")` 注册未测 | **确认 real**（注册行删除测试仍绿） | 已修：`test_documents_registers_recovery_catalog` |
| 6 | 命名 | `test_recovery_catalog_model_accepts_dict_form` 实际测 `model_validate_json` | **确认** | 已改 `test_recovery_catalog_model_accepts_json_string` |
| 7 | 流程 | "byte-identical to server" 仓内不可证（常量随 commit 同改） | 认可，流程外锚定 | 锁测试注释记录 server 侧 digest 来源，供人工比对 |
| 8 | 无问题 | schema defs 逐字节一致 / 静默 return 分支被 schema 前置覆盖 / key↔failure_kind 逻辑无假阳性 | — | — |

**修复后全量：1208 passed**（commit `??`）。

### #121（verify + failure_kind + workflow）— round-1 Codex

**发现 3 项，裁决如下：**

| # | 级别 | 发现 | 裁决 | 处理 |
|---|------|------|------|------|
| 1 | **High** | malformed persisted catalog 绕过 fallback：v1.3 state 接受任意 dict 作 `recovery_catalog`；本地损坏成 `{}` 时 `RecoveryDirectiveCatalog.model_validate` 抛 `ProtocolValidationError`（非 `LectureCastError`），`_recovery_workflow` 只 catch `LectureCastError` → generation-resume 走进 unexpected-error 路径而非 `_resume_error_workflow`。不显示未验签 directive，但缺失/未验签/无匹配的 fallback 已回归。 | **确认 real**（schema 校验 shape，验签入口抛错类型漏网） | 已修：`verify_recovery_catalog_signature` 的 `model_validate` 包 `except ProtocolValidationError → manifest_signature_invalid`（fail-closed，不泄漏）；补 `test_recovery_workflow_malformed_catalog_returns_none` + `test_recovery_catalog_verify_rejects_malformed_catalog_dict` |
| 2 | Low | verifier 声称单 error-code 契约：缺 `cryptography` 抛 `client_upgrade_required`、schema 校验失败漏 `ProtocolValidationError`，均非 `manifest_signature_invalid`，与 docstring "on any failure" 不符。仍 fail-closed，缺依赖行为与 `verify_manifest` 一致。 | **确认**（主要是契约一致性） | 已修 docstring 精确化：缺依赖→`client_upgrade_required`（对齐 verify_manifest），其余→`manifest_signature_invalid` |
| 3 | Low | 重要失败/迁移边界缺直测：malformed catalog fallback、algorithm mismatch、invalid signature/public-key base64、invalid keyring load、缺 crypto 依赖、`previous` key 接受、v1.3 invalid billing/catalog 字段、catalog-without-billing 不升级、真实 v1.0/v1.2 reload（旧测试只构造 v1.1）。 | **确认**（覆盖缺 9 类） | 已补 6 测：algorithm mismatch 拒绝、`previous` key 接受、keyring-load 失败 fail-closed、malformed catalog fail-closed ×2、v1.2/v1.0 reload 扩展 |

**修复后全量：1230 passed**（基线 1224 + r1 修复新增 6 测，含 #122 recovery-schema byte-match 契约 1 测）。
