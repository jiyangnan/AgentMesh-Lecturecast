from __future__ import annotations

import json

import pytest

from lecturecast.config import HEYGEN_KEYRING_USERNAME, KEYRING_SERVICE
from lecturecast.errors import LectureCastError
from lecturecast.heygen_credentials import (
    delete_stored_heygen_api_key,
    get_heygen_api_key,
    heygen_credential_status,
    save_heygen_api_key,
)


class FakeBackend:
    def __init__(self, value: str | None = None, *, fail: bool = False) -> None:
        self.value = value
        self.fail = fail

    def get_password(self, service: str, username: str) -> str | None:
        assert (service, username) == (KEYRING_SERVICE, HEYGEN_KEYRING_USERNAME)
        if self.fail:
            raise RuntimeError("backend unavailable with hidden internals")
        return self.value

    def set_password(self, service: str, username: str, password: str) -> None:
        assert (service, username) == (KEYRING_SERVICE, HEYGEN_KEYRING_USERNAME)
        if self.fail:
            raise RuntimeError("backend unavailable with hidden internals")
        self.value = password

    def delete_password(self, service: str, username: str) -> None:
        assert (service, username) == (KEYRING_SERVICE, HEYGEN_KEYRING_USERNAME)
        self.value = None


def test_environment_override_remains_compatible_without_exposing_secret() -> None:
    secret = "heygen_environment_secret"
    backend = FakeBackend("heygen_stored_secret")

    assert get_heygen_api_key(environment={"HEYGEN_API_KEY": secret}, backend=backend) == secret
    status = heygen_credential_status(environment={"HEYGEN_API_KEY": secret}, backend=backend)

    assert status.source == "environment"
    assert status.environment_override is True
    assert secret not in json.dumps(status.to_dict())


def test_system_store_round_trip_never_serializes_key() -> None:
    secret = "heygen_system_store_secret"
    backend = FakeBackend()

    status = save_heygen_api_key(secret, backend=backend)
    assert status.configured is True
    assert status.source == "system_credential_store"
    assert secret not in repr(status)
    assert get_heygen_api_key(environment={}, backend=backend) == secret

    delete_stored_heygen_api_key(backend=backend)
    assert heygen_credential_status(environment={}, backend=backend).configured is False


def test_unavailable_keyring_is_reported_as_unconfigured_for_safe_recovery() -> None:
    backend = FakeBackend(fail=True)

    assert get_heygen_api_key(environment={}, backend=backend) is None
    status = heygen_credential_status(environment={}, backend=backend)
    assert status.configured is False
    assert "hidden internals" not in json.dumps(status.to_dict())


def test_keyring_write_error_is_sanitized_and_never_echoes_key() -> None:
    secret = "heygen_never_echo_this_secret"

    with pytest.raises(LectureCastError) as captured:
        save_heygen_api_key(secret, backend=FakeBackend(fail=True))

    serialized = json.dumps(captured.value.to_dict(), ensure_ascii=False)
    assert captured.value.code == "missing_credential"
    assert secret not in serialized
    assert "hidden internals" not in serialized
    assert "聊天" in captured.value.next_action


def test_invalid_key_is_rejected_before_system_storage() -> None:
    backend = FakeBackend()

    with pytest.raises(LectureCastError) as captured:
        save_heygen_api_key("short", backend=backend)

    assert captured.value.code == "invalid_api_key"
    assert backend.value is None
