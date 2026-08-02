# §5.5e5d-d 设计稿 — 交互式降级卡片（D13）

> 子步：§5.5e5d-d（D13）。前序：e5d-c maintenance wiring ✅ LOCKED（13 轮 Codex）。
> 规格：`lecturecast-server/docs/DIGITAL-HUMAN-TECH-SPEC.md` v1.4 §0/§1.5/§2.6/§3.6/§7.1。
> 工作流：调研 DONE → **本文 = 盲预测+设计稿** → 实现+测试 → Codex review → lock。
> 不变量（铁）：fail-closed / 宁可少报绝不虚报 —— configured/claimed capability = 本机真实能 serve 的；CLI exit 0 = 无需注意，exit 2 = partial/skip/attention，exit 1 = 直接 lib 编程错（绝不经 CLI）。

---

## §0 D13 契约（来自 tech spec + DIGITAL-HUMAN-PROGRESS 设计表）

> 设计表 D13 行（`docs/DIGITAL-HUMAN-PROGRESS.md` §5.5e5d 表 + e5cd-design.md §D13 @449）原文：
>
> **D13 交互式降级卡片（用户裁定 §7.1）**：director preflight 探测「presenter_plan 要数字人但 configured 缺失/失败」时，返回交互式 next_action（选项 A 配 HEYGEN_API_KEY 并反馈 / 选项 B 降级 M1 基础视频），**懒触发**（capture 不问、use-time 绝不静默放弃）；M1 路径（不要数字人）**不触发**；上报层面仍 omit（third_party_processors 不上报）。

三条硬约束（契约级，实现不能放松）：

1. **懒触发**：capture 阶段不弹卡（能力采集只如实记录 HeyGen 是否配置），只在 director `generate` 真正要花钱前才比对 intent vs capability。
2. **M1 路径不触发**：`avatar == "none"`（用户没要数字人）→ 卡片永不出现，M1 正常走。
3. **上报层面仍 omit**：即便用户选 B（降级），`create_generation` 的 capabilities payload 仍 OMIT `third_party_processors`（本机没配就不能声称 configured=true，§0 Principle 6）。

---

## §1 调研 grounding（file:line 锚点，逐条核实）

### §1.1 intent 信号 = `brief.presenter.avatar`

- `src/lecturecast/protocol/schemas/v1.1/creative-brief.schema.json` PresenterBrief：
  `avatar: {"default": "none", "enum": ["none", "photo"], "type": "string"}`。
- **`avatar == "photo"` = 用户要数字人**（HeyGen 真人出镜）；`avatar == "none"` = M1 基础视频。
- Brief 是 M0 产物，`director brief confirm` 前确认、`project_store.save_brief()` 持久化、digest 进 DirectorState。
- intent 信号在 **Brief**（M0），不是 presenter_plan（M2/server-side）—— 这是 linchpin：intent 在 client 可读、generate 时已确定。

### §1.2 capability 信号 = capabilities.third_party_processors 是否含 heygen+configured

- `src/lecturecast/capabilities.py:645-660` `capture_capabilities_v1_1`：`processor = heygen_processor(...)`（3 AND 门：env `HEYGEN_API_KEY` + adapter_probe + journal_probe，任一失败返 None）；**仅当 `processor is not None` 才 `payload["third_party_processors"] = [processor]`**（@657-658）。
  - → **key 存在 = configured+live**；key 缺 = 整个 key 被 omit（绝非空 list）。
- **precedent 谓词**（必复用，勿另造）：`_stored_heygen_still_live` @`director.py:755-760`：
  ```python
  heygen_configured = any(
      isinstance(p, dict) and p.get("provider") == "heygen" and p.get("configured")
      for p in (payload.get("third_party_processors") or [])
  )
  ```
  D13 的「not configured」判定 = 此表达式的否定。同源同形态，避免新造启发式（[[feedback_no_trial_error]]）。

### §1.3 触发位置 = director.generate capabilities 解析后、paid call 前

- `director.py` `generate()` @772-881：
  - capabilities 解析 @806-846（stored snapshot → `_stored_heygen_still_live` B1 守卫 @820 → 或 fresh `capture_capabilities_v1_1` @828 → `save_capabilities` @844）。
  - **paid call** `create_generation(...)` @848-854，传 `capabilities=capabilities.model_dump()`。
- **D13 插入点：line 846 之后、line 848 之前**（capabilities 已终态、paid call 未发）。
- B1 守卫保证：到 846 时 capabilities 准确反映 HeyGen 当前可用性（stored stale 则已 drop+recapture，key 没了则已 omit）。→ D13 拿到的就是「现在能不能 serve HeyGen」的真值。

### §1.4 option B 可行性 = server 不在 create_generation 硬门禁 avatar/capability

- tech spec §0 Principle 6（@15-16）：capability presence 是 **M2 gate**（M1 不查）；advisory 永不上传、永不硬门禁。
- tech spec @259（v1.3）：**「M1 不依赖 HeyGen：photo 用户未配 HeyGen key → M1 仍可创建并交付基础视频；HeyGen configured 仅在 M2 gate 查」**。
- tech spec @257：`total_max` 非门禁（v1.3）。
- → option B（avatar=photo + payload omit third_party_processors → create_generation）**server 接受**：M1 创建并交付，M2/M3 因无 capability 后续不触发（不写空 milestone 行，@179）。用户拿到 M1 视频，知情放弃 M2。
- 与 server-side consent 路径区分（@80）：`photo + consent=declined → effective_avatar=none` 是 **Brief 里 ThirdPartyProcessingBrief.consent_status** 的 server-side 裁决；D13 是 **client-local capability gap**（env key 没配），两者都能导致「无 M2」但路径不同。D13 只管 capability gap。

### §1.5 brief 在 generate 可读

- `project.py:136` `self.brief_path = directory / "creative-brief.json"`；@225 `brief = parse_creative_brief(_read_object(self.brief_path))`。
- generate @806 已有 `project_store = ProjectStore(directory)` 在 scope → 复用同一读取路径（加一个 `project_store.load_brief()` helper 或直接 `parse_creative_brief(_read_object(project_store.brief_path))`）。

### §1.6 next_action 是 plain dict（无 Pydantic 模型锁死 kind）

- `_session_workflow` @194-336 把 action 组成 dict；现有 kind：`"command"`（@117）、`"host_choice"`（@204）、`"stop"`（@237）。policy 恒为 `"execute_only_returned_next_action"`。
- `host_choice` @200-220 由 `session.get("decision_card_set")` 驱动（**server-driven**），用 `argv_template` 带 `<question_id>`/`<option_id>` 占位符 + `requires_user_approval: True` + phase `"decision_required"`。
- `answer` 命令 @548-585 把 question/option 提交 **server**（`_make_client(...).answer(...)`）—— **D13 不能直接复用 answer**（server 不知 client capability gap）。

### §1.7 DirectorState payload（不持 flag）

- `director.py` DirectorState：payload 持 `brief_version`(int,@300)、`generation_id`(str,@320)、`session_status` 等。**D13 选 CLI flag 方案（per-invocation consent），不改 state schema** —— 避免动 e5d-c 已锁的状态机 + 迁移链。

---

## §2 盲预测契约（invariants + 精确触发）

### §2.1 触发谓词（精确，三条 AND + 一条 guard）

```
v1.1_only = (state.protocol_version == "1.1")
wants_dh  = (brief.presenter.avatar == "photo")
configured = any(p.provider=="heygen" and p.configured
                 for p in capabilities.third_party_processors or [])   # §1.2 precedent
consented = accept_digital_human_downgrade   # CLI bool flag，default False

D13_fire = v1.1_only and wants_dh and (not configured) and (not consented)
```

- **v1.0 guard**：v1.0 无 HeyGen 概念 → 永不触发（`v1.1_only` 短路）。
- **M1 path guard**：`avatar == "none"` → `wants_dh=False` → 永不触发（契约 §0.2）。
- **consent guard**：用户已选 B（flag=True）→ 不再弹卡，直接走 paid call（避免死循环）。

### §2.2 行为契约（fire 时）

D13_fire=True 时，generate **不调 create_generation**，改为 emit 一个 workflow，其 `next_action` 是交互式卡片（A/B 二选一），phase=`"digital_human_decision_required"`。payload 不变（capabilities 已 omit HeyGen，本就不含 third_party_processors）。

### §2.3 行为契约（consent 时 / option B）

`accept_digital_human_downgrade=True`（host 收到 B 选择后带 flag 重调 generate）→ 跳过卡片，正常走 `create_generation`。**payload 仍 omit** `third_party_processors`（capabilities 本就如此，D13 不碰 payload）→ 不虚假 claimed configured=true（契约 §0.3 + §0 Principle 6）。

### §2.4 不引入新 truthy force 源（blind-prediction 约束 a）

`--accept-digital-human-downgrade` 是 **consent-to-OMIT**（同意降级、不出数字人），**不是** force-include。它绝不创建「configured=false 但 payload 声称 configured=true」的路径。payload 真值由 `capture_capabilities_v1_1` 单源决定，D13 不覆写。

### §2.5 只动 generate + 新增 routing 命令，不碰已锁子系统（约束 d）

不动 e5b0c3c 删除子系统、e5d-a doctor、e5d-b canary、e5d-c maintenance、consent/lease/fence 任一已锁不变量。`heygen_processor`/`build_heygen_doctor_section` 只读复用。

---

## §3 实现 map（实现阶段照此，先 RED 后 GREEN）

### §3.1 新增 helper（纯函数，先单测）

```python
# director.py（或 capabilities.py，择邻近）
def _brief_avatar(project_store: ProjectStore) -> str | None:
    """读 brief.presenter.avatar；brief 缺/解析失败 → None（fail-closed：None≠'photo'，不触发）。"""
    # 复用 project.py:225 的 parse_creative_brief(_read_object(brief_path))

def _heygen_configured(capabilities: ClientCapabilities) -> bool:
    """复用 _stored_heygen_still_live @755-760 的同源谓词（any provider==heygen and configured）。"""
    payload = capabilities.model_dump()
    return any(
        isinstance(p, dict) and p.get("provider") == "heygen" and p.get("configured")
        for p in (payload.get("third_party_processors") or [])
    )

def _digital_human_decision_action(root: str, *, credit_cost: int) -> dict[str, Any]:
    """D13 卡片 next_action（A/B 二选一，路由到 director digital-human decide）。"""
```

### §3.2 generate 插入（line 846↔848 之间）

```python
# capabilities 终态后、create_generation 前：
if state.protocol_version == "1.1" and not accept_digital_human_downgrade:
    avatar = _brief_avatar(project_store)
    if avatar == "photo" and not _heygen_configured(capabilities):
        emit(_result(
            state=state,
            workflow={
                "phase": "digital_human_decision_required",
                "policy": "execute_only_returned_next_action",
                "next_action": _digital_human_decision_action(
                    str(directory.expanduser().resolve()),
                    credit_cost=_pricing_credit_cost(session, protocol_version="1.1"),
                ),
            },
        ), json_output=json_output,
           message="检测到 avatar=photo 但本机未配置 HeyGen，需用户裁定。")
        return   # 不调 create_generation
# 否则（avatar=none / 已 configured / 已 consent / v1.0）→ 原流程 create_generation @848
```

generate signature 加 `accept_digital_human_downgrade: bool = typer.Option(False, "--accept-digital-human-downgrade")`。

### §3.3 新增 routing 命令 `director digital-human decide`

```python
@app.command("digital-human")  # 或子命令组
def digital_human_decide(
    directory: Path = typer.Argument(Path(".")),
    choice: str = typer.Option(..., "--choice"),   # enum: configure | downgrade
    json_output: bool = typer.Option(False, "--json"),
):
    """D13 用户裁定路由（client-local，不 hit server）。"""
    # choice 用 type() is str 守卫 + 白名单 {configure, downgrade}，非白名单 → LectureCastError exit 2
    # configure → emit next_action = director.doctor（read-only，已锁，告知 key_missing 修复指引）+ phase "digital_human_configure_required"
    # downgrade → emit next_action = director.generate --accept-digital-human-downgrade（paid, approval, credit_cost）
```

- **option A（configure）**：路由到已锁的 `director doctor`（D1-D5，read-only，会告知 `key_missing`/`adapter_unimportable` 等修复指引）。不 create_generation。
- **option B（downgrade）**：路由到 `director generate --accept-digital-human-downgrade`（带 approval + credit_cost，复用 `_pricing_credit_cost`）。

### §3.4 卡片 shape（host_choice kind + 内联 question/options）

```python
{
  "id": "director.digital_human.decide",
  "kind": "host_choice",
  "question_id": "digital_human_downgrade",      # client-local static（非 server card_set）
  "question_label": "brief.avatar=photo 但本机未配置 HeyGen。请裁定：",
  "options": [
    {"id": "configure", "label": "配置 HEYGEN_API_KEY 并重新采集（可出数字人）"},
    {"id": "downgrade", "label": "降级 M1 基础视频（本次不出数字人）"},
  ],
  "argv_template": ["lecturecast","director","digital-human","decide", root,
                    "--choice","<option_id>","--json"],
  "mutates": True,
  "requires_user_approval": True,
}
```

复用 host_choice kind + 占位符机制（host 已实现「填 `<option_id>` → 跑 argv」）。**与 server-driven card 的唯一差别**：question/options 来自 action 内联（非 `session.decision_card_set`）。

---

## §4 tests-before-impl（RED-first，先写后跑）

新建 `tests/test_director_digital_human_decision.py`：

| # | 测试 | 断言 |
|---|------|------|
| D-T1 | avatar="photo" + capabilities 无 third_party_processors → generate emit 卡片，**不**调 create_generation | workflow.phase=="digital_human_decision_required"；next_action.kind=="host_choice"；options 恰 2（configure/downgrade）；create_generation mock 0 调用 |
| D-T2 | avatar="none"（M1）+ 同样无 HeyGen → **不触发**卡片，正常 create_generation | phase=="generation_..."；无 digital_human_decision；create_generation 调用 1 |
| D-T3 | avatar="photo" + HeyGen configured（third_party_processors 含 heygen+configured=True）→ **不触发**，正常 create_generation | 同 D-T2 |
| D-T4 | avatar="photo" + 无 HeyGen + `--accept-digital-human-downgrade` → 跳过卡片、create_generation，**payload 仍 omit** third_party_processors | create_generation 收到的 capabilities dict 不含 key `third_party_processors` |
| D-T5 | v1.0 session + avatar="photo"（构造）→ **不触发**（v1.1_only guard） | 走原 v1.0 流程 |
| D-T6 | `digital-human decide --choice configure` → next_action = director.doctor（read-only, mutates=False） | 不 hit server；next_action.id 含 doctor |
| D-T7 | `digital-human decide --choice downgrade` → next_action = director.generate --accept-digital-human-downgrade（approval=True, credit_cost=定价） | next_action.argv 含 flag；credit_cost 正确 |
| D-T8 | `--choice bogus` → LectureCastError exit 2（白名单守卫，type() is str） | 非崩溃 exit 1 |
| D-T9 | D-T1 的卡片 payload **不含** third_party_processors（契约 §0.3：即便 fire，上报层仍 omit） | capabilities.model_dump() 无 key |
| D-T10 | `_brief_avatar` brief 缺/malformed → None（fail-closed：None≠photo 不触发，D-T2 同族） | 返 None，不 raise |

约束：每个测试 mock `_make_client().create_generation` 计数；capabilities 用 fixture（with/without third_party_processors）。

### §4.1 RED-first 实跑记录（2026-08-03，20 测全 GREEN，全量 1189 passed）

20 测 = 11 unit（helpers）+ 9 integration（generate 触发组合 + decide 路由）。RED→GREEN 期间暴露 **5 处**，分类记录（诚实区分「实现 bug」vs「测试装置问题」）：

1. **实现 bug — `expand_user` 拼写（director.py:620 + 1056）**：两处新增代码写 `directory.expand_user()`，正确是 `expanduser()`（无下划线， pathlib 原语）。文件其余 6 处均正确。两个 D13 新增点是仅有的错处 → 已修。**若非 RED-first，runtime 才会以 `core_unavailable`/AttributeError 炸**。
2. **测试装置 — schema const**：v1.1 capabilities schema 把 `third_party_processors[].provider` 钉死为 `"heygen"`（const），不能改 `"f5"` 验「其他 provider」分支。改为 stub 对象的 `model_dump()` 返非-heygen provider，直测谓词的 `provider == "heygen"` 合取项（防御未来 schema 放宽）。
3. **测试装置 — generation 响应必须 schema-valid**：v1.1 经 `_generation` 的 `ManifestGenerationOutV1_1.model_validate`。初版 mock body 缺字段 + `session_id:"s1"` 违 minLength 3 → `ProtocolValidationError`。补全 17 required 字段 + `session_id:"sess_1"`（state session 仍 "s1"，`_generation` 不交叉校验 session 一致性）。
4. **测试装置 — v1.0 capture 不可带 billing_state**：mock `_queued_generation` 含 `billing_state` → `state_store.update` 触发 v1.1→v1.2 schema 升级（加 billing_state/resume_available/billing_updated_at 三键），但 v1.0-origin state 缺 `protocol_version` → v1.2 17-key 校验失败（ValueError）。`_Capture` 改为 protocol-aware：v1.0 返最小 `{generation_id,status,updated_at}`（`_generation` 对 v1.0 跳过 schema 校验），v1.1 返完整 `_queued_generation`。
5. **测试装置 — `fail()` emit 目标 + 扁平 envelope**：`fail()` 走 `typer.echo(..., err=True)` → stderr → CliRunner 读 `result.output`（非 `result.stdout`，对齐 test_generation_v1_1.py:393 resume 范式）；`LectureCastError.to_dict()` 扁平（`body["code"]`，非 `body["error"]["code"]`）。

**结论**：4/5 是测试装置（mock body 形状 / CliRunner 流），仅 1/5（expand_user）是真实实现 bug —— 但正是 RED-first 才在提交前抓到它。helpers 谓词（intent fail-closed None / capability 同源表达式 / card shape）+ 触发组合（photo+unconfigured→fire / none→skip / configured→skip / flag→skip+omit / v1.0→skip）+ 路由（configure→doctor read-only / downgrade→generate+flag / bogus→invalid_choice）全部覆盖。

---

## §5 spec anchors（实现时逐条对齐）

- §0 Principle 6（@15-16）：capability=M2 gate，advisory 永不上传/不硬门禁 → D13 是 client-local，payload omit。
- §1.5 case A（@203）：photo preflight 失败 → M2 awaiting_user_action，M1 digest 不变 → D13 option A 路由修复、option B 知情降级，链不破。
- §2.6（@259）：M1 不依赖 HeyGen → option B 可行。
- §3.6（@423-432）：preflight 本地 advisory，永不上传 → D13 只读 intent vs capability，不上报。
- §7.1（用户裁定）：D13 卡片即 §7.1 交互裁定的 client 落地。

---

## §6 留给 Codex 的开放问题（盲预测诚实标注）

1. ~~**host 契约（cross-repo agentmesh-core）**~~ **【调研后已解，2026-08-03】**：查证 `agentmesh-core` 无 `next_action`/`host_choice`/`argv_template` 执行器（唯一命中是设计文档提及 `next_action` 概念）；`docs/LOCAL-WORKFLOW.md` 不记录该契约。→ **host renderer 是去中心化的 caller-side 约定**（lecturecast 只发自描述 action JSON，渲染 + 填占位符由 caller —— 人/orchestrator/agent —— 负责）。D13 emit 自描述 inline-options host_choice 即可，**无需 cross-repo 改动**。caller 若已支持现有 host_choice 的「填 `<option_id>` → 跑 argv_template」机制，则同样能处理 D13（唯一差别：options 源是 action 内联而非 `session.decision_card_set`，字段同形）。留给 Codex 确认：action 内联 options 是否与现有 server-driven card 的 caller 渲染路径兼容（非阻塞 —— client 侧 emit 正确即可测）。
2. **consent 持久化粒度**：当前选 per-invocation CLI flag（crash 后重调会再弹卡 = fail-safe 重确认）。是否需要 DirectorState 持 flag 以免重弹？（倾向：否 —— 重确认更安全，且避免动已锁 state schema。）
3. **option A 路由目标**：选 `director doctor`（已锁、read-only、已有 HeyGen 修复指引）。是否够，还是需要专门的「re-capture」命令？（倾向：doctor 够。）

---

## §7 刻意不做（non-goals）

- ❌ 不改 brief（不把 avatar photo→none —— 会破坏 brief digest 链 / manifest / brief_version，blast radius 过大）。
- ❌ 不在 capture 阶段弹卡（契约：懒触发）。
- ❌ 不让 server 在 create_generation 硬门禁（§0 P6：advisory 不硬门禁）。
- ❌ 不引入 force-include HeyGen 的任何路径（约束 a）。
- ❌ 不改 DirectorState schema / consent / lease / fence / 删除子系统任一已锁不变量（约束 d）。
- ❌ 不在 v1.0 触发（v1.0 无 HeyGen）。
