"""Stdlib video downloader + ffprobe media probe (§5.5e5a).

Real implementations of the VideoDownloader and MediaProbe protocols. The
downloader writes ONLY the deterministic .tmp file (publication is the e4a2
finalize step's job, never the downloader's). The probe shells out to ffprobe.
Both are injectable so tests can stub them.

Security rules (per Codex e5 plan):
- HTTPS-only; no userinfo; no non-default ports; no cross-host redirects.
- Host allowlist is a LOCAL trust policy, never derived from a remote URL.
- DNS resolution must succeed and resolve to public IPs only.
- Redirects are disabled; a redirect is a hard error.
- temp file opened with O_NOFOLLOW where supported, 0600 permissions.
- Streaming SHA-256 + size enforcement; Content-Length is a hint, not authority.
- API key never sent with the download request.
- Downloader never calls os.replace; it only produces the staged temp.
- Temp path uses lexical containment + lstat each parent component.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import shutil
import socket
import stat
import subprocess
from pathlib import Path
from typing import Protocol
from urllib import request as urllib_request
from urllib.parse import urlparse

from lecturecast.operation_repository import MediaProbeResult, PreparedDownload

_DEFAULT_DOWNLOAD_HOSTS = frozenset({"files.heygen.ai"})
_DOWNLOAD_CHUNK = 65536


def resolve_download_hosts(extra: str | None = None) -> frozenset[str]:
    """Build the host allowlist. Defaults to the official HeyGen CDN host.
    An optional comma-separated env value adds exact hosts (no wildcards,
    lowercased, IDNA-encoded)."""
    hosts: set[str] = set(_DEFAULT_DOWNLOAD_HOSTS)
    if extra:
        for h in extra.split(","):
            h = h.strip().lower()
            if h and "*" not in h and not h.startswith("."):
                hosts.add(h)
    return frozenset(hosts)


def _reject_non_public_ip(ip_str: str) -> None:
    """Reject loopback/private/link-local/multicast/reserved/unspecified addresses."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return
    if (addr.is_loopback or addr.is_private or addr.is_link_local
            or addr.is_multicast or addr.is_reserved or addr.is_unspecified):
        raise ValueError(f"download resolves to a forbidden IP: {ip_str}")


def _resolve_and_check_host(host: str) -> list[str]:
    """Resolve host via DNS, reject on failure or non-public IP. Returns
    verified IP addresses."""
    # If host is already an IP literal, check directly.
    try:
        ipaddress.ip_address(host)
        _reject_non_public_ip(host)
        return [host]
    except ValueError:
        pass
    # DNS resolution — failure is a hard error, not silently passed.
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"DNS resolution failed for {host}: {exc}") from exc
    if not infos:
        raise ValueError(f"DNS returned no addresses for {host}")
    verified: list[str] = []
    for family, _, _, _, sockaddr in infos:
        ip = sockaddr[0]
        _reject_non_public_ip(ip)
        verified.append(ip)
    if not verified:
        raise ValueError(f"no public IP addresses for {host}")
    return verified


class _NoRedirectHandler(urllib_request.BaseHandler):
    """urllib opener handler that refuses HTTP redirects — a redirect is treated
    as a hard error, not silently followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError(f"download redirect refused: {code} -> {newurl}")


def _validate_download_url(url: str, allowed_hosts: frozenset[str]) -> str:
    """Full URL validation: HTTPS, no userinfo, host in allowlist, DNS resolves
    to public IPs only. Returns the validated host."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"download URL must be HTTPS: {url}")
    if parsed.username or parsed.password:
        raise ValueError("download URL must not contain userinfo")
    if parsed.port is not None:
        raise ValueError("download URL must not specify a non-default port")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("download URL missing host")
    if host not in allowed_hosts:
        raise ValueError(f"download host not in allowlist: {host}")
    _resolve_and_check_host(host)
    return host


def _verify_lexical_containment(path: Path, root: Path) -> None:
    """Lexical path containment: walk the non-resolved path from root, lstat
    each parent component to detect symlink injection."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        raise ValueError(f"path escapes runtime (lexical): {path}")
    current = root
    for part in rel.parts[:-1]:
        current = current / part
        try:
            st = current.lstat()
            if stat.S_ISLNK(st.st_mode):
                raise ValueError(f"symlink in directory chain: {current}")
        except FileNotFoundError:
            return  # intermediate doesn't exist yet — fine, mkdir will create


class StdlibVideoDownloader:
    """Downloads a video URL to a deterministic .tmp file with streaming
    SHA-256, size enforcement, and media validation. Does NOT os.replace —
    publication is the e4a2 finalize step's job.

    Redirects are disabled (a redirect is a hard error). DNS must resolve to
    public IPs. The temp file uses O_NOFOLLOW + 0600 where supported."""

    def __init__(self, allowed_hosts: frozenset[str] | None = None) -> None:
        self._allowed_hosts = allowed_hosts or resolve_download_hosts(
            os.environ.get("LECTURECAST_HEYGEN_DOWNLOAD_HOSTS")
        )

    def download_and_verify(self, url: str, runtime_dir: str,
                            local_output_ref: str, max_bytes: int,
                            probe: MediaProbe) -> PreparedDownload:
        _validate_download_url(url, self._allowed_hosts)
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive int")
        runtime_root = Path(runtime_dir)
        temp_path = runtime_root / (local_output_ref + ".tmp")
        # Lexical containment + symlink check (before mkdir creates intermediates).
        _verify_lexical_containment(temp_path, runtime_root)
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        # Post-mkdir re-check: intermediate dirs just created must not be symlinks.
        _verify_lexical_containment(temp_path, runtime_root)

        h = hashlib.sha256()
        total = 0
        fd = None
        resp = None
        try:
            req = urllib_request.Request(url, method="GET")
            # Build an opener that refuses redirects.
            opener = urllib_request.build_opener(_NoRedirectHandler)
            resp = opener.open(req, timeout=30)
            # Open temp with 0600; O_NOFOLLOW where supported.
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            try:
                flags |= os.O_NOFOLLOW  # type: ignore[attr-defined]
            except AttributeError:
                pass
            fd = os.open(str(temp_path), flags, 0o600)
            while True:
                chunk = resp.read(_DOWNLOAD_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"download exceeded max_bytes ({max_bytes})")
                h.update(chunk)
                # Handle partial writes.
                written = 0
                while written < len(chunk):
                    written += os.write(fd, chunk[written:])
            os.fsync(fd)
            os.close(fd)
            fd = None
            if total == 0:
                raise ValueError("downloaded file is empty")
            digest = "sha256:" + h.hexdigest()
            media = probe.probe(str(temp_path))
            return PreparedDownload(
                temp_path_str=str(temp_path),
                local_output_ref=local_output_ref,
                digest=digest,
                size_bytes=total,
                media=media,
            )
        except Exception:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temp_path.exists() and not temp_path.is_symlink():
                temp_path.unlink()
            raise
        finally:
            if resp is not None:
                resp.close()


# --- ffprobe media probe ------------------------------------------------


class FfprobeMediaProbe:
    """Probes a media file via subprocess ffprobe. Returns MediaProbeResult
    with validated fields. Raises on any anomaly."""

    def __init__(self, ffprobe_path: str | None = None) -> None:
        path = ffprobe_path or os.environ.get("LECTURECAST_FFPROBE_PATH") \
            or shutil.which("ffprobe")
        if not path:
            raise FileNotFoundError("ffprobe not found (set LECTURECAST_FFPROBE_PATH or install ffmpeg)")
        if not Path(path).is_file():
            raise FileNotFoundError(f"ffprobe not found at: {path}")
        self._ffprobe = path

    def probe(self, path: str) -> MediaProbeResult:
        p = Path(path)
        if not p.is_file() or p.is_symlink():
            raise ValueError(f"probe target must be a regular file: {path}")
        result = subprocess.run(
            [self._ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise ValueError(f"ffprobe failed (exit {result.returncode})")
        stdout = result.stdout
        if len(stdout) > 1_048_576:
            raise ValueError("ffprobe stdout exceeded 1 MiB")
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("ffprobe output not valid JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("ffprobe output is not a JSON object")
        streams = data.get("streams")
        if not isinstance(streams, list):
            raise ValueError("ffprobe streams field missing or not a list")
        video_streams = [s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"]
        if not video_streams:
            raise ValueError("no video stream found")
        vs = video_streams[0]
        codec = str(vs.get("codec_name", "")).strip()
        if not codec:
            raise ValueError("video stream missing codec_name")
        try:
            width = int(vs.get("width", 0))
            height = int(vs.get("height", 0))
        except (TypeError, ValueError):
            raise ValueError("video stream width/height not integers")
        if type(width) is bool or type(height) is bool:
            raise ValueError("video stream width/height must not be bool")
        if width <= 0 or height <= 0:
            raise ValueError("video stream width/height must be positive")
        raw_dur = vs.get("duration") or data.get("format", {}).get("duration")
        try:
            duration = float(raw_dur)
        except (TypeError, ValueError):
            raise ValueError("duration not a valid float")
        if duration != duration or duration == float("inf") or duration <= 0:
            raise ValueError("duration must be finite positive")
        return MediaProbeResult(
            duration_seconds=duration,
            video_codec=codec,
            width=width,
            height=height,
        )
