"""HeyGen asset upload adapter — safe multipart upload (§5.5e5b0b).

Uploads portrait photos and synthetic narration audio to HeyGen's
POST /v3/assets endpoint via multipart/form-data.

Security rules (per Codex e5b plan):
- Single fixed multipart field "file" (no arbitrary fields)
- Boundary deterministic from idempotency_key hash
- File opened O_RDONLY | O_NOFOLLOW, fstat'd, regular-file checked
- MIME from magic bytes + extension (both must match asset_role)
- 32 MiB hard limit (double-layer: adapter fstat + transport check)
- Hash completed BEFORE upload (no media leak on digest mismatch)
- seek(0) + same FD for upload (no hash/upload swap)
- Filename fixed by asset_role (portrait.png / narration.wav)
- 32 MiB not buffered in memory (streaming multipart)
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lecturecast.heygen_http import (HeyGenHttpTransport, HttpResponse,
    HttpErrorResponse, HttpTransportError)
from lecturecast.heygen_adapter import HeyGenAdapterError

AssetRole = Literal["portrait_photo", "synthetic_narration_audio"]

_ASSET_MAX_BYTES = 32 * 1024 * 1024  # 32 MiB
_PROVIDER_FILENAMES = {
    ("portrait_photo", "image/png"): "portrait.png",
    ("portrait_photo", "image/jpeg"): "portrait.jpg",
    ("synthetic_narration_audio", "audio/mpeg"): "narration.mp3",
    ("synthetic_narration_audio", "audio/wav"): "narration.wav",
}
_ALLOWED_EXTENSIONS = {
    "portrait_photo": {".png", ".jpg", ".jpeg"},
    "synthetic_narration_audio": {".mp3", ".wav"},
}
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class AssetUploadCommand:
    """Prepared, validated command for an asset upload. Constructed only via
    prepare_asset_upload() — callers cannot forge it."""
    operation_id: str
    asset_role: AssetRole
    local_output_ref: str        # runtime-root-relative path
    expected_asset_digest: str    # sha256:<64hex>
    idempotency_key: str          # derived from op+role+digest
    provider_filename: str        # fixed by asset_role
    content_type: str             # derived from magic+ext
    file_size: int


@dataclass(frozen=True)
class AssetUploadResult:
    asset_id: str
    remote_url: str   # transient, never persisted
    mime_type: str
    size_bytes: int


class AssetUploadError(HeyGenAdapterError):
    """Asset upload failed (not_sent)."""
    def __init__(self, *, code: str, message: str = "", retryable: bool = False):
        super().__init__(code=code, retryable=retryable,
                         submission_certainty="not_sent", message=message)


class AssetUploadAmbiguousError(HeyGenAdapterError):
    """Asset upload may have reached HeyGen (maybe_sent)."""
    def __init__(self, *, code: str, message: str = "", retryable: bool = True):
        super().__init__(code=code, retryable=retryable,
                         submission_certainty="maybe_sent", message=message)


def _validate_asset_path(runtime_root: Path, local_output_ref: str) -> None:
    """Shared path containment guard for prepare + upload. Rejects absolute,
    traversal, dot segments, and symlinks in runtime root or any intermediate
    directory."""
    if local_output_ref.startswith("/"):
        raise ValueError("absolute path rejected")
    if ".." in local_output_ref:
        raise ValueError("path traversal rejected")
    file_path = runtime_root / local_output_ref
    try:
        rel = file_path.relative_to(runtime_root)
    except ValueError:
        raise ValueError("file path escapes runtime")
    if "." in rel.parts or ".." in rel.parts:
        raise ValueError(f"path contains . or ..")
    current = runtime_root
    try:
        root_st = current.lstat()
        if stat.S_ISLNK(root_st.st_mode):
            raise ValueError("runtime root is a symlink")
    except FileNotFoundError:
        pass
    for part in rel.parts[:-1]:
        current = current / part
        try:
            st = current.lstat()
            if stat.S_ISLNK(st.st_mode):
                raise ValueError(f"symlink in path: {current}")
        except FileNotFoundError:
            pass


def prepare_asset_upload(
    *,
    operation_id: str,
    asset_role: AssetRole,
    runtime_root: Path,
    local_output_ref: str,
) -> AssetUploadCommand:
    """Derive a validated, deterministic AssetUploadCommand. Reads the file to
    compute SHA-256 (which must match on replay). Derives idempotency_key from
    operation_id + asset_role + digest so replays are idempotent."""
    if asset_role not in ("portrait_photo", "synthetic_narration_audio"):
        raise ValueError(f"unknown asset_role: {asset_role!r}")
    if not operation_id or not operation_id.strip():
        raise ValueError("operation_id is required")
    _validate_asset_path(runtime_root, local_output_ref)
    file_path = runtime_root / local_output_ref
    # Open safely
    flags = os.O_RDONLY
    try:
        flags |= os.O_NOFOLLOW  # type: ignore[attr-defined]
    except AttributeError:
        pass
    fd = None
    try:
        fd = os.open(str(file_path), flags)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("asset file is not a regular file")
        if st.st_size > _ASSET_MAX_BYTES:
            raise ValueError(f"asset file exceeds {_ASSET_MAX_BYTES} bytes")
        if st.st_size <= 0:
            raise ValueError("asset file is empty")
        # Determine MIME from magic + extension
        ext = Path(local_output_ref).suffix.lower()
        if ext not in _ALLOWED_EXTENSIONS.get(asset_role, set()):
            raise ValueError(f"extension {ext!r} not allowed for role {asset_role!r}")
        magic = os.read(fd, 16)
        os.lseek(fd, 0, os.SEEK_SET)  # rewind
        content_type = _detect_mime(magic, ext, asset_role)
        # Stream SHA-256
        h = hashlib.sha256()
        remaining = st.st_size
        while remaining > 0:
            chunk_size = min(65536, remaining)
            chunk = os.read(fd, chunk_size)
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
        digest = "sha256:" + h.hexdigest()
    finally:
        if fd is not None:
            os.close(fd)
    # Derive idempotency key
    idem_input = f"{operation_id}:{asset_role}:{digest}"
    idempotency_key = "lc-hg-asset-" + hashlib.sha256(idem_input.encode()).hexdigest()
    return AssetUploadCommand(
        operation_id=operation_id,
        asset_role=asset_role,
        local_output_ref=local_output_ref,
        expected_asset_digest=digest,
        idempotency_key=idempotency_key,
        provider_filename=_PROVIDER_FILENAMES.get((asset_role, content_type), f"asset.{ext.lstrip('.')}"),
        content_type=content_type,
        file_size=st.st_size,
    )


def _detect_mime(magic: bytes, ext: str, asset_role: AssetRole) -> str:
    """Detect MIME from magic bytes + extension. Both must be consistent with
    the asset role."""
    # PNG: \x89PNG\r\n\x1a\n
    if magic[:8] == b"\x89PNG\r\n\x1a\n":
        if ext != ".png":
            raise ValueError("PNG magic but non-.png extension")
        return "image/png"
    # JPEG: \xff\xd8\xff
    if magic[:3] == b"\xff\xd8\xff":
        if ext not in (".jpg", ".jpeg"):
            raise ValueError("JPEG magic but non-.jpg extension")
        return "image/jpeg"
    # MP3: ID3 tag or \xff\xfb/\xff\xf3/\xff\xf2
    if magic[:3] == b"ID3" or magic[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        if ext != ".mp3":
            raise ValueError("MP3 magic but non-.mp3 extension")
        return "audio/mpeg"
    # WAV: RIFF....WAVE
    if magic[:4] == b"RIFF" and magic[8:12] == b"WAVE":
        if ext != ".wav":
            raise ValueError("WAV magic but non-.wav extension")
        return "audio/wav"
    raise ValueError(f"unrecognized file format for asset_role {asset_role!r}")


class HeyGenAssetAdapter:
    """Uploads assets to HeyGen POST /v3/assets. Uses the shared HTTP transport
    for the actual multipart call."""

    def __init__(self, transport: HeyGenHttpTransport) -> None:
        self._transport = transport

    def upload_asset(self, command: AssetUploadCommand, *, runtime_root: Path) -> AssetUploadResult:
        """Upload one asset. Re-verifies the SHA-256 digest on the re-opened
        FD (prevents same-size mutation), then seeks to 0 and streams via
        multipart. Never re-hashes after the network call."""
        try:
            _validate_asset_path(runtime_root, command.local_output_ref)
        except ValueError as exc:
            raise AssetUploadError(code="validation_error", message=str(exc))
        file_path = runtime_root / command.local_output_ref
        flags = os.O_RDONLY
        try:
            flags |= os.O_NOFOLLOW  # type: ignore[attr-defined]
        except AttributeError:
            pass
        fd = None
        try:
            fd = os.open(str(file_path), flags)
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size != command.file_size:
                raise AssetUploadError(code="validation_error", message="file changed since prepare")
            # Re-verify digest on the same FD before upload.
            h = hashlib.sha256()
            remaining = st.st_size
            while remaining > 0:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                h.update(chunk)
                remaining -= len(chunk)
            actual_digest = "sha256:" + h.hexdigest()
            if actual_digest != command.expected_asset_digest:
                raise AssetUploadError(code="validation_error",
                    message="asset digest mismatch on re-open (file mutated)")
            # Re-derive idempotency key from operation_id + role + digest.
            idem_input = f"{command.operation_id}:{command.asset_role}:{actual_digest}"
            expected_idem = "lc-hg-asset-" + hashlib.sha256(idem_input.encode()).hexdigest()
            if expected_idem != command.idempotency_key:
                raise AssetUploadError(code="validation_error",
                    message="idempotency key does not match derivation")
            # Re-detect MIME from magic + extension, verify matches command.
            os.lseek(fd, 0, os.SEEK_SET)
            magic = os.read(fd, 16)
            os.lseek(fd, 0, os.SEEK_SET)
            ext = Path(command.local_output_ref).suffix.lower()
            re_detected_ct = _detect_mime(magic, ext, command.asset_role)
            if re_detected_ct != command.content_type:
                raise AssetUploadError(code="validation_error",
                    message="content_type mismatch on re-open")
            # Verify role + extension consistency.
            if command.asset_role not in ("portrait_photo", "synthetic_narration_audio"):
                raise AssetUploadError(code="validation_error", message="invalid asset_role")
            if ext not in _ALLOWED_EXTENSIONS.get(command.asset_role, set()):
                raise AssetUploadError(code="validation_error",
                    message=f"extension {ext!r} not allowed for role {command.asset_role!r}")
            expected_fn = _PROVIDER_FILENAMES.get(
                (command.asset_role, command.content_type), f"asset.{ext.lstrip('.')}")
            if expected_fn != command.provider_filename:
                raise AssetUploadError(code="validation_error",
                    message="provider filename mismatch")
            # Rewind for upload.
            os.lseek(fd, 0, os.SEEK_SET)
            fileobj = os.fdopen(fd, "rb")
            fd = None
            try:
                resp = self._transport.request_multipart_file(
                    path="/v3/assets",
                    fileobj=fileobj,
                    filename=command.provider_filename,
                    content_type=command.content_type,
                    file_size=command.file_size,
                    idempotency_key=command.idempotency_key,
                )
            except HttpErrorResponse as exc:
                raise self._map_error(exc) from None
            except HttpTransportError as exc:
                if exc.code == "auth_failed":
                    raise AssetUploadError(code="auth_failed",
                        message=f"transport error: {exc}") from None
                if exc.code in ("network_timeout", "connection_error"):
                    raise AssetUploadAmbiguousError(code=exc.code, retryable=True,
                        message=f"transport error: {exc}") from None
                if exc.code == "malformed_response":
                    raise AssetUploadAmbiguousError(code="malformed_response", retryable=False,
                        message=f"transport error: {exc}") from None
                raise AssetUploadAmbiguousError(code="unknown",
                    message=f"transport error during upload: {exc}") from None
            finally:
                fileobj.close()
        finally:
            if fd is not None:
                os.close(fd)

        data = resp.body.get("data")
        if not isinstance(data, dict):
            raise AssetUploadAmbiguousError(code="malformed_response",
                message="upload response data is not a dict")
        asset_id = data.get("asset_id")
        if not isinstance(asset_id, str) or not asset_id.strip():
            raise AssetUploadAmbiguousError(code="malformed_response",
                message="upload succeeded but no asset_id returned")
        mime_type = data.get("mime_type", "")
        if not isinstance(mime_type, str) or not mime_type.strip():
            raise AssetUploadAmbiguousError(code="malformed_response",
                message="upload response missing mime_type")
        if mime_type.strip() != command.content_type:
            raise AssetUploadAmbiguousError(code="malformed_response",
                message=f"MIME mismatch: response {mime_type} != upload {command.content_type}")
        size_bytes = data.get("size_bytes", 0)
        if type(size_bytes) is not int or size_bytes <= 0 or size_bytes != command.file_size:
            raise AssetUploadAmbiguousError(code="malformed_response",
                message=f"upload response size_bytes mismatch: {size_bytes} != {command.file_size}")
        return AssetUploadResult(
            asset_id=asset_id.strip(),
            remote_url=str(data.get("url", "")),
            mime_type=mime_type.strip(),
            size_bytes=size_bytes,
        )

    @staticmethod
    def _map_error(exc: HttpErrorResponse) -> Exception:
        """Map HTTP error responses to adapter errors with stable internal codes.
        Provider codes are preserved in the message, never used as .code."""
        provider_code = exc.provider_code or "unknown"
        if exc.status == 429:
            return AssetUploadError(code="rate_limited", retryable=True,
                message=f"HTTP 429 ({provider_code})")
        if exc.status == 409:
            if provider_code == "request_in_progress":
                return AssetUploadAmbiguousError(code="unknown",
                    message="HTTP 409 request_in_progress")
            return AssetUploadAmbiguousError(code="unknown", retryable=False,
                message=f"HTTP 409 ({provider_code}) — non-retryable")
        if exc.status in (401, 403):
            return AssetUploadError(code="auth_failed",
                message=f"HTTP {exc.status} ({provider_code})")
        if exc.status in (400, 404, 422):
            return AssetUploadError(code="validation_error",
                message=f"HTTP {exc.status} ({provider_code})")
        if 400 <= exc.status < 500:
            return AssetUploadError(code="unknown",
                message=f"HTTP {exc.status} ({provider_code})")
        # 5xx
        return AssetUploadAmbiguousError(code="provider_server_error",
            message=f"HTTP {exc.status} ({provider_code})")
