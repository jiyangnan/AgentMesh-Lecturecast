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
