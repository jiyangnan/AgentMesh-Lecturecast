"""Cross-repo contract tests (§6, tech-spec v1.3).

These tests verify the contracts that span the three repos on disk:
  - agentmesh-core ........ canonical product/action/cost registry
  - lecturecast-server .... canonical protocol schema export + milestone costs
  - AgentMesh-Lecturecast . vendored protocol schemas + HeyGen journal

They are the guard against the 2026-07-29 drift incident: a change in one repo
that silently breaks another. Each assertion compares two real artifacts on
disk, not an in-memory mock.

If a sibling repo is not checked out next to this one, the cross-repo tests
skip (so the suite still runs in a client-only checkout). The self-consistency
and journal tests always run.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

# --- repo locations (siblings of this repo) ---------------------------------

CLIENT_ROOT = Path(__file__).resolve().parents[1]
CLIENT_PROTOCOL = CLIENT_ROOT / "src" / "lecturecast" / "protocol"
CLIENT_V10_SCHEMAS = CLIENT_PROTOCOL / "schemas"
CLIENT_V10_LOCK = CLIENT_PROTOCOL / "protocol.lock"
CLIENT_V11_SCHEMAS = CLIENT_PROTOCOL / "schemas" / "v1.1"
CLIENT_V11_LOCK = CLIENT_V11_SCHEMAS / "protocol.lock"

# The three repos live as siblings under the same parent dir.
CORE_ROOT = CLIENT_ROOT.parent / "agentmesh-core"
SERVER_ROOT = CLIENT_ROOT.parent / "lecturecast-server"
SERVER_V10_DIR = SERVER_ROOT / "protocol" / "v1"
SERVER_V11_DIR = SERVER_ROOT / "protocol" / "v1.1"
CORE_REGISTRY = CORE_ROOT / "deploy" / "agentmesh-product-registry.json"
CORE_PRODUCTS_PY = CORE_ROOT / "app" / "products.py"
SERVER_MILESTONE_PY = SERVER_ROOT / "app" / "milestone_billing.py"

CROSS_REPO_AVAILABLE = CORE_ROOT.is_dir() and SERVER_ROOT.is_dir()

skip_cross_repo = pytest.mark.skipif(
    not CROSS_REPO_AVAILABLE,
    reason="agentmesh-core / lecturecast-server not present as sibling repos",
)


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _load_lock(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# === §6 #2: protocol export ↔ client import lock (anti-drift contract) =====
#
# The single most important cross-repo contract: every schema byte the server
# exports must be byte-identical to what the client vendored. This is exactly
# what broke on 2026-07-29 (registry/schema drift). Comparing the lock file is
# necessary but not sufficient — we also compare each schema file's bytes.

@pytest.fixture(scope="module")
def server_v10_lock() -> dict:
    return _load_lock(SERVER_V10_DIR / "protocol.lock")


@pytest.fixture(scope="module")
def server_v11_lock() -> dict:
    return _load_lock(SERVER_V11_DIR / "protocol.lock")


@skip_cross_repo
class TestProtocolLockCrossRepoIdentity:
    """Server canonical lock == client vendored lock, byte for byte."""

    def test_v1_0_lock_files_match_server(self, server_v10_lock):
        client = _load_lock(CLIENT_V10_LOCK)
        assert client["bundle_digest"] == server_v10_lock["bundle_digest"]
        assert client["files"] == server_v10_lock["files"]
        assert client["bundle_version"] == server_v10_lock["bundle_version"]

    def test_v1_1_lock_files_match_server(self, server_v11_lock):
        client = _load_lock(CLIENT_V11_LOCK)
        assert client["bundle_digest"] == server_v11_lock["bundle_digest"]
        assert client["files"] == server_v11_lock["files"]
        assert client["bundle_version"] == server_v11_lock["bundle_version"]

    @pytest.mark.parametrize("filename", [
        "creative-brief.schema.json",
        "client-capabilities.schema.json",
        "production-manifest.schema.json",
        "decision-card-set.schema.json",
        "error-envelope.schema.json",
    ])
    def test_v1_0_schema_bytes_match_server(self, filename):
        # Byte-identical schemas — the lock matching is not enough.
        assert (CLIENT_V10_SCHEMAS / filename).read_bytes() == \
               (SERVER_V10_DIR / filename).read_bytes()

    @pytest.mark.parametrize("filename", [
        "creative-brief.schema.json",
        "client-capabilities.schema.json",
        "production-manifest.schema.json",
        "decision-card-set.schema.json",
        "error-envelope.schema.json",
        "manifest-generation-out.schema.json",
        "presenter-plan.schema.json",
        "orchestration-plan.schema.json",
        "recovery-directive-catalog.schema.json",
    ])
    def test_v1_1_schema_bytes_match_server(self, filename):
        assert (CLIENT_V11_SCHEMAS / filename).read_bytes() == \
               (SERVER_V11_DIR / filename).read_bytes()


# === §6 #2 (self-consistency): vendored lock tamper detection ================
#
# Even without the server present, the client must be able to detect that its
# own vendored bundle has not been tampered with: each file digest recompute and
# the bundle_digest recompute must hold.

class TestVendoredLockSelfConsistency:
    """The client's own vendored lock must be internally consistent."""

    def test_v1_0_bundle_digest_recomputes(self):
        from lecturecast.protocol import canonical_digest
        lock = _load_lock(CLIENT_V10_LOCK)
        assert lock["bundle_digest"] == canonical_digest(lock["files"])

    def test_v1_1_bundle_digest_recomputes(self):
        from lecturecast.protocol import canonical_digest
        lock = _load_lock(CLIENT_V11_LOCK)
        assert lock["bundle_digest"] == canonical_digest(lock["files"])

    def test_v1_0_every_file_digest_holds(self):
        lock = _load_lock(CLIENT_V10_LOCK)
        for filename, expected in lock["files"].items():
            assert _sha256((CLIENT_V10_SCHEMAS / filename).read_bytes()) == expected

    def test_v1_1_every_file_digest_holds(self):
        lock = _load_lock(CLIENT_V11_LOCK)
        for filename, expected in lock["files"].items():
            assert _sha256((CLIENT_V11_SCHEMAS / filename).read_bytes()) == expected

    def test_v1_0_lock_covers_exactly_the_schema_files_present(self):
        lock = _load_lock(CLIENT_V10_LOCK)
        on_disk = {p.name for p in CLIENT_V10_SCHEMAS.glob("*.json")}
        assert set(lock["files"]) == on_disk

    def test_v1_1_lock_covers_exactly_the_schema_files_present(self):
        lock = _load_lock(CLIENT_V11_LOCK)
        on_disk = {p.name for p in CLIENT_V11_SCHEMAS.glob("*.json")}
        assert set(lock["files"]) == on_disk


# === §6 #1: Core action registry ↔ server milestone costs ====================

@pytest.fixture(scope="module")
def lecturecast_registry_actions() -> dict[str, int]:
    """action_code (without product prefix) -> cost, from Core's registry."""
    registry = json.loads(CORE_REGISTRY.read_text(encoding="utf-8"))
    lec = registry["products"]["lecturecast"]
    return {a["action_code"].removeprefix("lecturecast."): a["cost"]
            for a in lec["credit_actions"]}


@pytest.fixture(scope="module")
def server_milestone_costs() -> dict[str, int]:
    """MILESTONE_LOCKED_COST mapping parsed from server source.

    Parsing the source (not importing) keeps this test out of the server venv
    and makes the contract check a literal text comparison.
    """
    source = SERVER_MILESTONE_PY.read_text(encoding="utf-8")
    # The declaration is `MILESTONE_LOCKED_COST: dict[Milestone, int] = { ... }`;
    # skip the type annotation between the name and the `=`.
    block = re.search(r"MILESTONE_LOCKED_COST[^=]*=\s*\{([^}]*)\}", source)
    assert block, "MILESTONE_LOCKED_COST dict not found in server source"
    costs: dict[str, int] = {}
    for name, value in re.findall(r'"([a-z_]+)"\s*:\s*(\d+)', block.group(1)):
        costs[name] = int(value)
    return costs


@skip_cross_repo
class TestCoreRegistryMilestoneCostContract:
    """Core registry, Core products.py, and server locked costs all agree:
    exactly 3 lecturecast milestone actions, each costing 10 credits."""

    def test_registry_has_exactly_three_actions(self, lecturecast_registry_actions):
        assert set(lecturecast_registry_actions) == {
            "manifest", "presenter_plan", "orchestration",
        }

    def test_every_action_costs_ten(self, lecturecast_registry_actions):
        assert set(lecturecast_registry_actions.values()) == {10}

    def test_registry_matches_server_locked_costs(
        self, lecturecast_registry_actions, server_milestone_costs
    ):
        # The server's fail-closed precheck cost must equal Core's registry cost.
        assert server_milestone_costs == lecturecast_registry_actions

    def test_registry_actions_exist_in_products_py(self, lecturecast_registry_actions):
        products_py = CORE_PRODUCTS_PY.read_text(encoding="utf-8")
        for action in lecturecast_registry_actions:
            assert f"lecturecast.{action}" in products_py, (
                f"lecturecast.{action} is in the registry but not in app/products.py "
                f"— registry/products drift"
            )


# === §6 #11: verified is never uploaded (v1.3) ==============================
#
# The `verified` flag is a local client state (downloaded + ffprobe-checked).
# It must never leak into anything the client sends upstream: capability report,
# creative brief, or any artifact payload. A `verified` key appearing in a
# vendored schema would mean the server could learn local trust state it should
# not.

class TestVerifiedNeverUploaded:
    """No client protocol schema exposes a `verified` field."""

    @pytest.mark.parametrize("schema_path", sorted(CLIENT_V10_SCHEMAS.glob("*.json")))
    def test_v1_0_schema_has_no_verified_property(self, schema_path):
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        _assert_no_verified_property(schema, schema_path.name)

    @pytest.mark.parametrize("schema_path", sorted(CLIENT_V11_SCHEMAS.glob("*.json")))
    def test_v1_1_schema_has_no_verified_property(self, schema_path):
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        _assert_no_verified_property(schema, schema_path.name)


def _assert_no_verified_property(schema: object, label: str) -> None:
    """Recursively assert no `verified` key appears anywhere in the schema."""
    if isinstance(schema, dict):
        for key, value in schema.items():
            assert key != "verified", (
                f"{label}: 'verified' field leaked into protocol schema "
                f"(violates v1.3 §11: verified is local-only, never uploaded)"
            )
            _assert_no_verified_property(value, label)
    elif isinstance(schema, list):
        for item in schema:
            _assert_no_verified_property(item, label)


# === §6 #9: 定价下发（v1.3） ==============================================
#
# The client only DISPLAYS the server's pricing estimate — it must not hardcode
# milestone costs in a way that diverges from server authority, and it must not
# gate on maximum_total (the real charge gate is the server's per-milestone
# 402). Three sub-contracts:
#   1. The client's display constants (legacy MANIFEST_CREDIT_COST + v1.1
#      pricing_version/charge_model) equal the server's authoritative values.
#   2. total_max is NOT a client start gate: the balance check compares against
#      the single-milestone manifest cost, never maximum_total.
#   3. The v1.0 session schema must not expose pricing_estimate (old clients
#      that don't know the field still work — it only exists on the v1.1 path).

@skip_cross_repo
class TestPricingDeliveryContract:
    """§6 #9: client pricing constants == server authority; total_max is not a gate."""

    def test_client_manifest_cost_matches_server(
        self,
        lecturecast_registry_actions,
        server_milestone_costs,
    ):
        # Client legacy display constant must equal server authority.
        client_config = (CLIENT_ROOT / "src" / "lecturecast" / "config.py").read_text(
            encoding="utf-8"
        )
        m = re.search(r"MANIFEST_CREDIT_COST\s*=\s*(\d+)", client_config)
        assert m, "MANIFEST_CREDIT_COST not found in client config.py"
        assert int(m.group(1)) == server_milestone_costs["manifest"], (
            "client MANIFEST_CREDIT_COST != server MILESTONE_LOCKED_COST[manifest]"
        )

    def test_client_pricing_version_matches_server(self):
        # v1.1 pricing_version + charge_model must match the server's constants.
        client_pricing = (CLIENT_ROOT / "src" / "lecturecast" / "pricing.py").read_text(
            encoding="utf-8"
        )
        version_m = re.search(r"_PRICING_VERSION\s*=\s*\"([^\"]+)\"", client_pricing)
        model_m = re.search(r"_CHARGE_MODEL\s*=\s*\"([^\"]+)\"", client_pricing)
        assert version_m and model_m, "pricing constants not found in client pricing.py"
        server = SERVER_MILESTONE_PY.read_text(encoding="utf-8")
        server_version = re.search(r'PRICING_VERSION\s*=\s*"([^"]+)"', server)
        assert server_version, "PRICING_VERSION not found in server milestone_billing.py"
        assert version_m.group(1) == server_version.group(1), (
            "client _PRICING_VERSION != server PRICING_VERSION"
        )
        # charge_model is a closed literal on both sides; assert the shared value.
        assert model_m.group(1) == "per_milestone_success"
        assert "per_milestone_success" in server

    def test_total_max_is_not_a_client_start_gate(self):
        # §6 #9: "total_max 非门禁（余额 20 放行 M1 on 30-max）". The client's
        # balance check gates on the single-milestone manifest cost, never on
        # maximum_total. A regression that introduced maximum_total into the
        # start gate would make M1 unreachable when balance < full-path total.
        commercial = (CLIENT_ROOT / "src" / "lecturecast" / "commercial.py").read_text(
            encoding="utf-8"
        )
        assert "maximum_total" not in commercial, (
            "client commercial.py references maximum_total — total_max must NOT "
            "be a start gate (§6 #9)"
        )

    def test_v1_0_session_schema_omits_pricing_estimate(self):
        # §6 #9: 旧 client schema 不收 pricing_estimate 仍正常 — the v1.0
        # decision-card-set schema (frozen) must not expose the PricingEstimate
        # definition; it exists only on the v1.1 path. This keeps old clients
        # working against a v1.0 server without breaking on an unknown field.
        v10 = json.loads(
            (CLIENT_V10_SCHEMAS / "decision-card-set.schema.json").read_text()
        )
        assert "PricingEstimate" not in v10.get("$defs", {}), (
            "v1.0 decision-card-set schema leaked PricingEstimate — the v1.0 "
            "path must stay frozen"
        )
        assert "pricing_estimate" not in json.dumps(v10)


# === §6 #10: M1 能力门禁独立于 HeyGen（v1.3） ============================
#
# Contract: a photo user WITHOUT HeyGen configured can still create + deliver an
# M1 (base) video; HeyGen `configured` is consulted ONLY on the M2
# (presenter_plan) gate. Four sub-contracts, each asserted against source:
#   1. The server M1 create gate (`validate_generation_capabilities`) checks
#      rendering/runtime/tts/etc. — never `third_party_processors` / `configured`.
#   2. The server's `configured` semantics are an M2-only compatibility gate
#      (docstring), and the capability carries NO `verified` field (v1.3 §0
#      Principle 6: preflight stays local, never uploaded).
#   3. The client omits `third_party_processors` from the upload payload when
#      HeyGen is unconfigured (the `if processor is not None` guard).
#   4. The v1.1 schema keeps `third_party_processors` OPTIONAL (not required),
#      and `configured` has no default — so an unconfigured client legally
#      reports the key absent, and a photo M1 path is not gated by it.

SERVER_GENERATIONS_PY = SERVER_ROOT / "app" / "services" / "generations.py"
SERVER_CAPABILITIES_PY = SERVER_ROOT / "app" / "schemas" / "capabilities.py"
CLIENT_CAPABILITIES_SRC = CLIENT_ROOT / "src" / "lecturecast" / "capabilities.py"


@skip_cross_repo
class TestM1CapabilityGateIndependentOfHeyGen:
    """§6 #10: M1 create must never gate on HeyGen configured; configured is M2-only."""

    def test_server_m1_gate_never_mentions_heygen(self):
        # The server's M1 capability gate (validate_generation_capabilities)
        # checks digest / manifest version / local runtime / components / aspect
        # ratio / output format / tts. A regression that added
        # third_party_processors or configured to this gate would make an
        # unconfigured photo user's M1 fail on the server.
        gate = SERVER_GENERATIONS_PY.read_text(encoding="utf-8")
        # Bound the slice at the start of the M2 gate: since m2-2 the next
        # sibling function (validate_presenter_capabilities) legitimately
        # references third_party_processors/configured. Slicing to end-of-file
        # would count the M2 gate against M1 (§6 #10). If the M2 gate is later
        # renamed, find() returns -1 and we fall back to end-of-file → the M2
        # gate body is re-included → this test goes RED (fail-safe, not green).
        next_fn = gate.find("def validate_presenter_capabilities", gate.find("def validate_generation_capabilities"))
        end = next_fn if next_fn != -1 else len(gate)
        body = gate[gate.find("def validate_generation_capabilities"):end]
        assert "third_party_processors" not in body, (
            "server M1 gate (validate_generation_capabilities) references "
            "third_party_processors — M1 must be independent of HeyGen (§6 #10)"
        )
        assert "configured" not in body, (
            "server M1 gate references `configured` — M1 must not gate on "
            "HeyGen configured (§6 #10)"
        )

    def test_server_configured_is_m2_only_and_has_no_verified_field(self):
        # `configured` = capability presence, explicitly an M2 compatibility
        # gate. And there is deliberately NO `verified` field (preflight is
        # local advisory, never uploaded — §0 Principle 6).
        source = SERVER_CAPABILITIES_PY.read_text(encoding="utf-8")
        assert "M2" in source, (
            "ThirdPartyProcessorCapability docstring must declare configured as "
            "the M2 compatibility gate (§6 #10)"
        )
        body = source.split("class ThirdPartyProcessorCapability", 1)[1]
        # Cut at the next class boundary — ClientCapabilitiesV1_1 has its own
        # docstring that ALSO mentions "verified" (again only to deny it), which
        # is outside this capability's field declarations.
        body = body.split("\nclass ", 1)[0]
        # Strip the class docstring: it mentions "verified" only to DENY it
        # ("There is deliberately NO `verified` field"), which must not trip
        # the field-absence check below.
        if '"""' in body:
            _, _, body = body.partition('"""')
            _, _, body = body.partition('"""')
        assert "configured: bool" in body
        assert "verified" not in body, (
            "ThirdPartyProcessorCapability field declarations leaked a "
            "`verified` field — preflight results are local advisory, never "
            "uploaded (§0 Principle 6)"
        )

    def test_client_omits_processors_when_unconfigured(self):
        # capture_capabilities_v1_1 only adds third_party_processors inside
        # `if processor is not None:` — an unconfigured client legally omits the
        # key from the upload payload, so a photo M1 create is not blocked.
        source = CLIENT_CAPABILITIES_SRC.read_text(encoding="utf-8")
        assert "if processor is not None:" in source, (
            "client must gate the third_party_processors payload on the processor "
            "being present (§6 #10)"
        )
        # The payload mutation is inside that guard, not unconditional.
        assert 'payload["third_party_processors"] = [processor]' in source

    def test_v1_1_schema_processors_optional_and_configured_has_no_default(self):
        # The wire contract: third_party_processors is NOT in the required list,
        # and `configured` is a plain boolean with no default. An unconfigured
        # client therefore reports the key absent — valid — and the server's M1
        # gate never needs to see it.
        schema = json.loads(
            (CLIENT_V11_SCHEMAS / "client-capabilities.schema.json").read_text()
        )
        assert "third_party_processors" not in schema.get("required", []), (
            "v1.1 client-capabilities made third_party_processors required — "
            "an unconfigured client must be able to omit it (§6 #10)"
        )
        tpc = schema["$defs"]["ThirdPartyProcessorCapability"]
        assert "configured" in tpc["required"]
        assert "default" not in tpc["properties"]["configured"]


# === §6 #12 + #15: HeyGen journal invariants (v1.3) =========================

class TestHeyGenJournalContracts:
    """The journal enforces the v1.3 resource-lifecycle invariants at the
    schema level: completed ≠ verified, and the resource FK is SET NULL."""

    def test_download_status_distinguishes_downloaded_from_verified(self, tmp_path):
        """`downloaded` and `verified` are distinct download_status values; a
        bogus value is rejected by the CHECK constraint. A video must be both
        downloaded AND ffprobe-verified before it can be marked `verified` —
        `downloaded` alone is NOT a terminal-ok state."""
        import sqlite3
        from lecturecast.heygen_journal import init_database
        conn = init_database(str(tmp_path))
        try:
            # Both 'downloaded' and 'verified' must be accepted distinct states.
            _insert_op(conn, "op-a", download_status="downloaded")
            _insert_op(conn, "op-b", download_status="verified")
            statuses = {row[0] for row in conn.execute(
                "SELECT download_status FROM heygen_operations"
            )}
            assert {"downloaded", "verified"}.issubset(statuses)
            # A bogus status must be rejected — protects against an unverified
            # video being marked done.
            with pytest.raises(sqlite3.IntegrityError):
                _insert_op(conn, "op-c", download_status="definitely_finished")
        finally:
            conn.close()

    def test_resource_fk_uses_on_delete_set_null(self, tmp_path):
        """heygen_remote_resources.created_by_operation_id uses ON DELETE SET
        NULL (not cascade, not PK). Per v1.3 §12: a reusable avatar resource
        must survive the deletion of the operation that created it."""
        from lecturecast.heygen_journal import init_database
        conn = init_database(str(tmp_path))
        try:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='heygen_remote_resources'"
            ).fetchone()[0]
            assert "ON DELETE SET NULL" in sql.upper()
        finally:
            conn.close()


def _insert_op(conn, operation_id: str, *, download_status: str) -> None:
    """Insert a minimal heygen_operations row with the given download_status.

    heygen_title is UNIQUE, so derive it from the operation id.
    """
    conn.execute(
        "INSERT INTO heygen_operations ("
        "  operation_id, kind, endpoint, generation_id,"
        "  manifest_digest, request_digest, idempotency_key, heygen_title,"
        "  download_status, created_at, updated_at"
        ") VALUES ("
        "  ?, 'video', '/v3/videos', 'gen-x',"
        "  'sha256:m', 'sha256:r', ?, ?,"
        "  ?, '2026-07-29T00:00:00Z', '2026-07-29T00:00:00Z'"
        ")",
        (operation_id, f"idem-{operation_id}", f"lc:{operation_id}", download_status),
    )
    conn.commit()


# === §6 M2: presenter_plan awaiting_credits resume 跨仓契约 =================
#
# m2-4 把 M2 presenter_plan 扣费接到同步 create 路径；m2-5 回归 M2 awaiting →
# /resume 恢复。这些契约防漂移：server 改 resume route / MILESTONE_ORDER，或
# client 改 resume URL / 里程碑枚举 / 402 投影，契约立即红。

SERVER_DIRECTOR_PY = SERVER_ROOT / "app" / "routes" / "director.py"
CLIENT_DIRECTOR_PY = CLIENT_ROOT / "src" / "lecturecast" / "director.py"
CLIENT_DIRECTOR_CMD = CLIENT_ROOT / "src" / "lecturecast" / "commands" / "director.py"
SERVER_MILESTONE_BILLING_PY = SERVER_MILESTONE_PY


@skip_cross_repo
class TestM2ResumeCrossRepoContract:
    """§6 M2: client resume path == server route; milestone enum aligned."""

    def test_client_resume_path_matches_server_route(self):
        # Client DirectorClient.resume_generation POSTs /director/generations/{id}/resume.
        client = CLIENT_DIRECTOR_PY.read_text(encoding="utf-8")
        m = re.search(r'url=f".*?/director/generations/\{generation_id\}/resume"', client)
        assert m, "client resume URL not found in director.py"
        # Bound to the resume_generation body: the URL pattern is on the line just
        # above method="POST" in DirectorClient.resume_generation (director.py:436).
        body = client.split("def resume_generation", 1)[1].split("def ", 1)[0]
        assert re.search(r'method="POST"', body), (
            "client resume_generation must POST — otherwise it cannot trigger "
            "the server's await/charge resume path"
        )
        # Server route registers the same path + method (POST). The decorator is
        # `@router.post(\n    "/director/generations/{generation_id}/resume"` — read
        # the token that precedes the quoted path on its own decorator block.
        server = SERVER_DIRECTOR_PY.read_text(encoding="utf-8")
        path_idx = server.find('"/director/generations/{generation_id}/resume"')
        assert path_idx != -1, "server resume route not found in routes/director.py"
        head = server[:path_idx]
        decorator = re.search(r"@router\.\w+\(?", head[::-1][:80][::-1])
        assert decorator and "post" in decorator.group(0), (
            "server resume route must be POST — client resume_generation POSTs it"
        )

    def test_client_resume_projection_matches_billing_aggregation(self):
        # The client offers resume only when the server says awaiting_credits +
        # resume_available. Regression guard: if the server renamed the field or
        # changed the resume gate, the client projection must follow.
        client_cmd = CLIENT_DIRECTOR_CMD.read_text(encoding="utf-8")
        assert '"awaiting_credits"' in client_cmd, (
            "client _status_workflow lost the awaiting_credits resume projection"
        )
        assert 'billing_state == "awaiting_credits" and resume_available' in client_cmd, (
            "client resume gate drifted from server billing_state+resume_available"
        )
        server = SERVER_MILESTONE_BILLING_PY.read_text(encoding="utf-8")
        assert 'state = "awaiting_credits"' in server, (
            "server aggregate_billing_state lost awaiting_credits aggregation"
        )
        assert "resume_available" in server, (
            "server aggregate_billing_state lost resume_available"
        )

    def test_milestone_enum_matches_cross_repo(self):
        # Server MILESTONE_ORDER == client _CORE_MILESTONES (manifest →
        # presenter_plan → orchestration). A milestone added/renamed on either
        # side without the other breaks the resume iteration order.
        server = SERVER_MILESTONE_BILLING_PY.read_text(encoding="utf-8")
        sm = re.search(r'MILESTONE_ORDER[^=]*=\s*\(([^)]*)\)', server)
        assert sm, "server MILESTONE_ORDER not found"
        server_ms = [x.strip().strip('"') for x in sm.group(1).split(",") if x.strip()]
        client_canary = (CLIENT_ROOT / "src" / "lecturecast" / "canary.py").read_text(
            encoding="utf-8"
        )
        cm = re.search(r'_CORE_MILESTONES\s*=\s*\(([^)]*)\)', client_canary)
        assert cm, "client _CORE_MILESTONES not found"
        client_ms = [x.strip().strip('"') for x in cm.group(1).split(",") if x.strip()]
        assert server_ms == client_ms, (
            f"milestone enum drifted: server={server_ms} client={client_ms}"
        )
        assert "presenter_plan" in server_ms

    def test_server_resume_route_is_v1_1_envelope(self):
        # M2 resume is a v1.1 feature: the route must raise the v1.1 error
        # envelope (wider code set incl. presenter-plan codes) and return a view
        # the v1.1 client parser understands.
        server = SERVER_DIRECTOR_PY.read_text(encoding="utf-8")
        assert "raise_api_error_v1_1" in server, (
            "server resume route must use raise_api_error_v1_1 (M2 codes)"
        )
        assert "milestone_billing_enabled" in server, (
            "server resume route lost the feature-flag gate"
        )
        # The resume view carries milestone_charges (ManifestGenerationOutV1_1)
        # so a presenter_plan awaiting/charged row is visible to the client.
        assert "milestone_charges" in server


# === §6 m2-6d: M2 create path 跨仓契约 =====================================
#
# m2-6 让 client 的 `director generation-presenter-plan` 消费 M2 create 路径。
# 三个契约防漂移：
#   1. client DirectorClient.create_presenter_plan 的 URL/方法 == server route
#      (@router.post /director/generations/{generation_id}/presenter-plan)。
#   2. client 的 M2 disclosure_version 常量 == server M2Approval schema 的字面量。
#      server 用 Literal[...] 拒绝其它值；client 若改了版本，请求立即被拒。
#   3. server provider catalog（M2 create 附带）不含 m1_insufficient_credits —
#      M2 上下文的不够额度是 provider 余额问题，不该给 M1 话术（m2-6 §2.5）。

SERVER_RECOVERY_CATALOG_PY = SERVER_ROOT / "app" / "recovery_catalog.py"
CLIENT_DIRECTOR_SRC = CLIENT_ROOT / "src" / "lecturecast" / "director.py"


@skip_cross_repo
class TestM2CreateCrossRepoContract:
    """§6 m2-6d: client presenter-plan create path == server route; M2 approval
    disclosure pinned; provider catalog never carries the M1 话术."""

    def test_client_presenter_plan_path_matches_server_route(self):
        # Client DirectorClient.create_presenter_plan POSTs
        # /director/generations/{id}/presenter-plan.
        client = CLIENT_DIRECTOR_SRC.read_text(encoding="utf-8")
        body = client.split("def create_presenter_plan", 1)[1].split("def ", 1)[0]
        assert re.search(
            r'url=f".*?/director/generations/\{generation_id\}/presenter-plan"', body
        ), "client create_presenter_plan URL not found in director.py"
        assert re.search(r'method="POST"', body), (
            "client create_presenter_plan must POST the M2 create-and-charge route"
        )
        # Server registers the same path + method. The decorator is
        # `@router.post(\n    "/director/generations/{generation_id}/presenter-plan"`.
        server = SERVER_DIRECTOR_PY.read_text(encoding="utf-8")
        path_idx = server.find('"/director/generations/{generation_id}/presenter-plan"')
        assert path_idx != -1, (
            "server presenter-plan route not found in routes/director.py"
        )
        head = server[:path_idx]
        decorator = re.search(r"@router\.\w+\(?", head[::-1][:80][::-1])
        assert decorator and "post" in decorator.group(0), (
            "server presenter-plan route must be POST — the client POSTs it"
        )

    def test_m2_disclosure_version_matches_server(self):
        # The client pins the disclosure the user saw; the server schema only
        # accepts that exact literal (Literal["heygen-transfer-2026-07-27"]). A
        # client that sends a stale/newer version gets a schema validation error.
        client = CLIENT_DIRECTOR_SRC.read_text(encoding="utf-8")
        cm = re.search(r'DISCLOSURE_VERSION\s*=\s*"([^"]+)"', client)
        assert cm, "client DISCLOSURE_VERSION not found in director.py"
        server = (SERVER_ROOT / "app" / "schemas" / "presenter.py").read_text(
            encoding="utf-8"
        )
        sm = re.search(
            r'disclosure_version:\s*Literal\["([^"]+)"\]', server
        )
        assert sm, "server M2Approval disclosure_version Literal not found"
        assert cm.group(1) == sm.group(1), (
            f"client DISCLOSURE_VERSION {cm.group(1)!r} != server Literal "
            f"{sm.group(1)!r} — the server would reject the client's approval"
        )

    def test_server_provider_catalog_has_no_m1_directive(self):
        # The provider (HeyGen) catalog ships with the M2 PresenterPlan. It must
        # never carry the M1 insufficient_credits 话术: an M2-context resume-402
        # is a provider-balance problem, not the M1 manifest charge. The client
        # suppresses the M1 mapping in M2 context (m2-6 §2.5) — this contract
        # pins the server side so the M1 directive cannot leak into the provider
        # increment.
        server = SERVER_RECOVERY_CATALOG_PY.read_text(encoding="utf-8")
        provider_block = server.split("def _provider_heygen_directives", 1)[1]
        provider_block = provider_block.split("def ", 1)[0]
        assert "m1_insufficient_credits" not in provider_block, (
            "server provider (HeyGen) catalog must NOT carry the m1_insufficient_credits "
            "directive — M2-context 402 is a provider balance issue, not the M1 "
            "manifest charge (m2-6 §2.5)"
        )
        # Sanity: the provider catalog is a distinct layer from the base catalog,
        # which legitimately owns the m1 directive.
        assert "def build_provider_catalog" in server
        assert "def build_base_catalog" in server


# === §6 m3: M3 orchestration-plan create path 跨仓契约 ======================
#
# m3-3/m3-4 让 server 的 M3 orchestration-plan gate + create-and-charge route 落地。
# 契约防漂移：
#   1. server M3 gate (`validate_orchestration_capabilities`) 是独立纯函数：只查
#      orchestration_plan schema 版本 / own_voice→f5 / photo→m2_charged，绝不引用
#      M1 的 renderer/tts 旁支或 M2 的 heygen 旁支（§0 Principle 6）。
#   2. server orchestration route 是 POST + v1.1 envelope（feature-flag gate +
#      raise_api_error_v1_1），client 的 v1.1 resume 视图能识别 M3 charge 行。
#   3. orchestration milestone 走既有的 `lecturecast:orchestration:{gid}` 幂等键
#      / `{gid}:orchestration` external_id 格式（§1.7 统一 shape）。

SERVER_GENERATIONS_SRC = SERVER_ROOT / "app" / "services" / "generations.py"


@skip_cross_repo
class TestM3OrchestrationCrossRepoContract:
    """§6 m3: M3 gate independent; orchestration route is POST + v1.1; billing shape aligned."""

    def test_server_m3_gate_is_independent_pure_function(self):
        # The M3 gate checks ONLY §2.6 items: orchestration_plan schema version,
        # own_voice→f5, photo→m2_charged. A regression that added M1/M2 gates
        # (renderer, output format, heygen/third_party_processors, consent,
        # approval) would re-check upstream-locked concerns and break the M3
        # path for a brief that M1/M2 already cleared. Bound the slice at EOF —
        # the M3 gate is the last function in generations.py.
        source = SERVER_GENERATIONS_SRC.read_text(encoding="utf-8")
        gate = source.split("def validate_orchestration_capabilities", 1)[1]
        body = gate.split("\ndef ", 1)[0]  # everything up to the next top-level def
        assert "def validate_orchestration_capabilities" in source
        # Strip the docstring: it legitimately *mentions* "renderer/voice" only
        # to explain what the gate does NOT re-check. Assert on the code body so
        # an explanatory mention cannot trip a field-absence check.
        if '"""' in body:
            _, _, body = body.partition('"""')
            _, _, body = body.partition('"""')
        # M1-only markers (must not appear in code).
        assert "renderer" not in body, (
            "M3 gate must not re-check M1 renderer requirements (§0 Principle 6)"
        )
        assert "output_format" not in body, (
            "M3 gate must not re-check M1 output-format requirements (§0 Principle 6)"
        )
        # M2-only markers (must not appear in code).
        assert "third_party_processors" not in body, (
            "M3 gate must not re-check M2 HeyGen / third_party_processors (§2.6)"
        )
        assert "consent" not in body, (
            "M3 gate must not re-check M2 approval/consent (§2.6)"
        )
        assert "heygen" not in body, (
            "M3 gate must not reference the HeyGen provider (§2.6)"
        )
        # The gate's OWN assertions (must be present).
        assert "type(m2_charged) is not bool" in body, (
            "M3 gate lost the non-bool m2_charged fail-closed guard"
        )
        assert '"1.1" not in supported_artifact_versions.orchestration_plan' in body, (
            "M3 gate lost the orchestration_plan schema-version check (①)"
        )
        assert 'voice_mode == "own_voice" and "f5" not in capabilities.tts_engines' in body, (
            "M3 gate lost the own_voice→f5 check (②)"
        )
        assert "m3_not_ready" in body, (
            "M3 gate must raise code=m3_not_ready (not a bare 409)"
        )

    def test_server_orchestration_route_is_post_v1_1(self):
        # The M3 create-and-charge route must be POST + v1.1 error envelope
        # (feature-flag gate + raise_api_error_v1_1), so an M3 409/402 is
        # parseable by the client's v1.1 error handling (m3_not_ready etc.).
        server = SERVER_DIRECTOR_PY.read_text(encoding="utf-8")
        path_idx = server.find('"/director/generations/{generation_id}/orchestration-plan"')
        assert path_idx != -1, "server orchestration-plan route not found in routes/director.py"
        head = server[:path_idx]
        decorator = re.search(r"@router\.\w+\(?", head[::-1][:80][::-1])
        assert decorator and "post" in decorator.group(0), (
            "server orchestration-plan route must be POST — it creates + charges"
        )
        # The route body (to the next @router decorator) must use the v1.1
        # envelope and the feature-flag gate.
        body = server[path_idx:]
        next_route = body.find("\n@router.")
        if next_route != -1:
            body = body[:next_route]
        assert "raise_api_error_v1_1" in body, (
            "server orchestration route must use raise_api_error_v1_1 (M3 codes)"
        )
        assert "milestone_billing_enabled" in body, (
            "server orchestration route lost the feature-flag gate"
        )

    def test_orchestration_billing_shape_matches_server_constants(self):
        # M3 uses the unified §1.7 idempotency key / external_id shapes — the
        # same non-manifest pattern M2 uses (lecturecast:{milestone}:{gid} /
        # {gid}:{milestone}). A regression that gave orchestration a bespoke
        # shape would break dedup against the server's charge rows.
        server_billing = SERVER_MILESTONE_BILLING_PY.read_text(encoding="utf-8")
        assert 'return f"lecturecast:{milestone}:{generation_id}"' in server_billing, (
            "server idempotency_key_for must produce lecturecast:{milestone}:{gid} "
            "for orchestration (§1.7)"
        )
        assert 'return f"{generation_id}:{milestone}"' in server_billing, (
            "server external_id_for must produce {gid}:{milestone} for orchestration (§1.7)"
        )
        # The orchestration milestone must be reachable in MILESTONE_ORDER for resume.
        server_ms = re.search(r'MILESTONE_ORDER[^=]*=\s*\(([^)]*)\)', server_billing)
        assert server_ms and "orchestration" in [
            x.strip().strip('"') for x in server_ms.group(1).split(",")
        ], "server MILESTONE_ORDER missing orchestration (resume would skip it)"

    def test_client_v1_1_envelope_vocabulary_has_m3_code(self):
        # The M3 gate raises code=m3_not_ready over the v1.1 envelope. The
        # client vendored bundle must accept that code or a M3 409 would blow up
        # client-side envelope parsing. This is the byte-level check that the
        # re-vendor in m3-5 actually took.
        envelope = json.loads(
            (CLIENT_V11_SCHEMAS / "error-envelope.schema.json").read_text()
        )
        codes = _enum_values(envelope)
        assert "m3_not_ready" in codes, (
            "client v1.1 error-envelope does not include m3_not_ready — re-vendor "
            "from lecturecast-server protocol/v1.1 is required"
        )

    def test_client_orchestration_plan_path_matches_server_route(self):
        # Client DirectorClient.create_orchestration_plan POSTs
        # /director/generations/{id}/orchestration-plan — the exact path the
        # server registers. A path/method drift would 404 the M3 create.
        client = CLIENT_DIRECTOR_SRC.read_text(encoding="utf-8")
        body = client.split("def create_orchestration_plan", 1)[1].split("def ", 1)[0]
        assert re.search(
            r'url=f".*?/director/generations/\{generation_id\}/orchestration-plan"', body
        ), "client create_orchestration_plan URL not found in director.py"
        assert re.search(r'method="POST"', body), (
            "client create_orchestration_plan must POST the M3 create-and-charge route"
        )
        server = SERVER_DIRECTOR_PY.read_text(encoding="utf-8")
        path_idx = server.find('"/director/generations/{generation_id}/orchestration-plan"')
        assert path_idx != -1, (
            "server orchestration-plan route not found in routes/director.py"
        )
        head = server[:path_idx]
        decorator = re.search(r"@router\.\w+\(?", head[::-1][:80][::-1])
        assert decorator and "post" in decorator.group(0), (
            "server orchestration-plan route must be POST — the client POSTs it"
        )

    def test_client_orchestration_plan_has_no_approval(self):
        # 裁决 B: M3 carries NO approval credential — neither the client method
        # signature nor its HTTP payload may contain an approval field. A server
        # CreateOrchestrationPlanIn that added `approval` would reject (and a
        # client that sent one would leak a fake dependency).
        client = CLIENT_DIRECTOR_SRC.read_text(encoding="utf-8")
        body = client.split("def create_orchestration_plan", 1)[1].split("def ", 1)[0]
        assert "approved" not in body, (
            "client create_orchestration_plan must NOT carry an approval param (裁决 B)"
        )
        assert '"approval"' not in body, (
            "client create_orchestration_plan payload must NOT include approval (裁决 B)"
        )
        server = (SERVER_ROOT / "app" / "schemas" / "presenter.py").read_text(
            encoding="utf-8"
        )
        sbody = server.split("class CreateOrchestrationPlanIn", 1)[1].split("class ", 1)[0]
        # Strip the docstring — it legitimately *mentions* "approval" only to
        # explain why M3 has none (裁决 B). Assert on the field declarations.
        if '"""' in sbody:
            _, _, sbody = sbody.partition('"""')
            _, _, sbody = sbody.partition('"""')
        assert "approval" not in sbody, (
            "server CreateOrchestrationPlanIn must NOT carry an approval field (裁决 B)"
        )
        assert "approved" not in sbody, (
            "server CreateOrchestrationPlanIn must NOT carry an approved field (裁决 B)"
        )
        # The payload is exactly {capabilities}.
        assert re.search(r'payload=\{"capabilities": capabilities\}', body), (
            "client create_orchestration_plan payload must be exactly "
            "{capabilities} (裁决 B)"
        )

    def test_server_orchestration_envelope_returns_base_catalog(self):
        # M3 has NO provider dependency, so its create response must return the
        # BASE recovery catalog (the provider increment is M2-only). A regression
        # that attached a provider catalog to M3 would leak a directive into a
        # context where the provider is not involved.
        server = (SERVER_ROOT / "app" / "schemas" / "presenter.py").read_text(
            encoding="utf-8"
        )
        sbody = server.split("class OrchestrationPlanOut", 1)[1].split("class ", 1)[0]
        assert "recovery_catalog" in sbody, (
            "server OrchestrationPlanOut must expose recovery_catalog"
        )
        # No provider-catalog field on the M3 envelope (presenter vs orchestration).
        assert "provider_catalog" not in sbody, (
            "server OrchestrationPlanOut must not carry a provider catalog (M2-only)"
        )

    def test_client_m3_context_recovery_suppression(self):
        # M3's create response carries the BASE recovery catalog (m3-4:
        # orchestration_plan.py get_base_catalog — M3 has no provider). The base
        # catalog DOES carry the m1_insufficient_credits directive (it is the M1
        # blocker catalog). So an M3-context resume-402 would naively present the
        # M1 话术 — the client must suppress the m1 mapping in the M3 context
        # exactly as it does in the M2 context (m3-6 §2.5). This contract pins
        # BOTH sides so the mismatch cannot silently reappear:
        #   server: M3 create returns the base catalog (m1 directive included),
        #   client: _project_in_m2_context also treats orchestration-plan.json as
        #           an entered plan phase, and _recovery_workflow suppresses the
        #           m1 mapping there, falling through to credit_top_up_required.
        server_plan = (SERVER_ROOT / "app" / "services" / "orchestration_plan.py").read_text(
            encoding="utf-8"
        )
        # ① M3 create returns the BASE catalog — not a provider catalog.
        assert "get_base_catalog()" in server_plan, (
            "server M3 create must return the base recovery catalog (M3 has no "
            "provider) — otherwise there is no m1 directive to suppress"
        )
        assert "get_provider_catalog()" not in server_plan, (
            "server M3 create must NOT return the provider catalog (M2-only)"
        )
        # ② The base catalog legitimately owns the m1 directive (the suppression
        #    is what keeps it out of M2/M3 contexts, not its absence).
        server_catalog = SERVER_RECOVERY_CATALOG_PY.read_text(encoding="utf-8")
        base_block = server_catalog.split("def _base_directives", 1)[1]
        base_block = base_block.split("def ", 1)[0]
        assert "m1_insufficient_credits" in base_block, (
            "server base catalog must carry the m1_insufficient_credits directive — "
            "the client suppression (not catalog omission) is the contract"
        )
        # ③ Client: orchestration-plan.json counts as an entered plan phase for
        #    the M1-directive suppression (the context check lives in the CLI
        #    layer, commands/director.py).
        client = CLIENT_DIRECTOR_CMD.read_text(encoding="utf-8")
        context_body = client.split("def _project_in_m2_context", 1)[1].split("def ", 1)[0]
        assert "orchestration_plan_path.exists()" in context_body, (
            "client _project_in_m2_context must treat orchestration-plan.json as an "
            "entered plan phase (M3 context) — otherwise an M3 resume-402 leaks "
            "the M1 话术"
        )
        assert "presenter_plan_path.exists()" in context_body, (
            "client _project_in_m2_context lost the presenter-plan.json check"
        )
        # ④ Client: _recovery_workflow suppresses the m1 mapping in plan context.
        recovery_body = client.split("def _recovery_workflow", 1)[1].split("def ", 1)[0]
        assert 'if m2_context and error.code == "insufficient_credits":' in recovery_body, (
            "client _recovery_workflow must suppress the insufficient_credits→"
            "m1_insufficient_credits mapping in plan context"
        )
        # ⑤ Client: the suppression falls through to the generic top-up 话术.
        assert "credit_top_up_required" in client, (
            "client _resume_error_workflow must offer credit_top_up_required for the "
            "M3-context 402 (the fall-through of the suppressed directive)"
        )


def _enum_values(schema: object) -> set[str]:
    """Collect all values named in any `enum` array in the schema (the error
    codes are declared as a JSON enum in error-envelope.schema.json)."""
    values: set[str] = set()
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "enum" and isinstance(value, list):
                values.update(item for item in value if isinstance(item, str))
            else:
                values |= _enum_values(value)
    elif isinstance(schema, list):
        for item in schema:
            values |= _enum_values(item)
    return values
