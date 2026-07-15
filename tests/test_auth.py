from __future__ import annotations

import json

import pytest

from lecturecast.auth import (
    auth_status,
    delete_stored_api_key,
    get_api_key,
    require_api_key,
    save_api_key,
)
from lecturecast.config import KEYRING_SERVICE, KEYRING_USERNAME
from lecturecast.errors import LectureCastError


class FakeBackend:
    def __init__(self, value: str | None = None, *, fail: bool = False) -> None:
        self.value = value
        self.fail = fail

    def get_password(self, service: str, username: str) -> str | None:
        assert (service, username) == (KEYRING_SERVICE, KEYRING_USERNAME)
        if self.fail:
            raise RuntimeError("backend unavailable")
        return self.value

    def set_password(self, service: str, username: str, password: str) -> None:
        assert (service, username) == (KEYRING_SERVICE, KEYRING_USERNAME)
        if self.fail:
            raise RuntimeError("backend unavailable")
        self.value = password

    def delete_password(self, service: str, username: str) -> None:
        assert (service, username) == (KEYRING_SERVICE, KEYRING_USERNAME)
        self.value = None


def test_environment_credential_has_priority_without_being_exposed() -> None:
    secret = "lc_live_environment_secret"
    backend = FakeBackend("lc_live_keyring_secret")

    assert get_api_key(environment={"LECTURECAST_API_KEY": secret}, backend=backend) == secret
    status = auth_status(environment={"LECTURECAST_API_KEY": secret}, backend=backend)

    assert status.source == "environment"
    assert secret not in json.dumps(status.to_dict())


def test_keyring_login_status_and_logout_never_return_secret() -> None:
    secret = "lc_live_keyring_secret"
    backend = FakeBackend()

    saved = save_api_key(secret, backend=backend)
    assert saved.configured
    assert secret not in repr(saved)
    assert require_api_key(environment={}, backend=backend) == secret

    delete_stored_api_key(backend=backend)
    assert auth_status(environment={}, backend=backend).configured is False


def test_backend_error_is_sanitized() -> None:
    secret = "lc_live_never_echo_this"
    with pytest.raises(LectureCastError) as captured:
        save_api_key(secret, backend=FakeBackend(fail=True))

    serialized = json.dumps(captured.value.to_dict(), ensure_ascii=False)
    assert secret not in serialized
    assert "backend unavailable" not in serialized


def test_missing_credential_has_machine_readable_next_action() -> None:
    with pytest.raises(LectureCastError) as captured:
        require_api_key(environment={}, backend=FakeBackend())

    assert captured.value.code == "missing_credential"
    assert "LECTURECAST_API_KEY" in captured.value.next_action

