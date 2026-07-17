from pathlib import Path

import pytest

from lecturecast import manifest


FIXTURE_KEYRING = Path(__file__).parent / "fixtures" / "signing-keyring-v1.json"


@pytest.fixture(autouse=True)
def use_fixture_signing_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manifest, "KEYRING_PATH", FIXTURE_KEYRING)
