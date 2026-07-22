from pathlib import Path

import pytest

from lecturecast import manifest


FIXTURE_KEYRING = Path(__file__).parent / "fixtures" / "signing-keyring-v1.json"


@pytest.fixture(autouse=True)
def use_fixture_signing_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manifest, "KEYRING_PATH", FIXTURE_KEYRING)


@pytest.fixture(autouse=True)
def isolate_home_with_current_host_skills(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Tests never depend on or mutate the developer's real host-agent Skills."""
    home = tmp_path_factory.mktemp("lecturecast-test-home")
    root = Path(__file__).resolve().parents[1]
    targets = {
        home / ".codex" / "skills" / "lecturecast": root / "skills" / "codex",
        home / ".claude" / "skills" / "lecturecast": root / "skills" / "claude-code",
        home / ".openclaw" / "skills" / "lecturecast": root / "skills" / "openclaw",
    }
    for target, source in targets.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(source, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
