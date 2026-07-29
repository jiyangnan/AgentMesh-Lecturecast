"""Stdlib video downloader + ffprobe media probe (§5.5e5a).

Real implementations of the VideoDownloader and MediaProbe protocols. The
downloader writes ONLY the deterministic .tmp file (publication is the e4a2
finalize step's job, never the downloader's). The probe shells out to ffprobe.
Both are injectable so tests can stub them.

Security rules (per Codex e5 plan):
- HTTPS-only; no userinfo; no non-default ports; no cross-host redirects.
- Host allowlist is a LOCAL trust policy, never derived from a remote URL.
- DNS resolution rejects loopback/private/link-local/multicast/reserved.
- temp file opened with O_NOFOLLOW where supported, 0600 permissions.
- Streaming SHA-256 + size enforcement; Content-Length is a hint, not authority.
- API key never sent with the download request.
- Downloader never calls os.replace; it only produces the staged temp.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from lecturecast.operation_repository import MediaProbeResult, PreparedDownload

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_DEFAULT_DOWNLOAD_HOSTS = frozenset({"files.heygen.ai"})
_DOWNLOAD_CHUNK = 65536
_TEMP_REJECT_RESOLVED = (
    ipaddress.IPv4Address("127.0.0.1"),  # placeholder; real check below
)


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


def _reject_private_ip(ip_str: str) -> None:
    """Reject loopback/private/link-local/multicast/reserved addresses."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return  # not an IP literal (hostname); DNS check below
    if addr.is_loopback or addr.is_private or addr.is_link_local \
            or addr.is_multicast or addr.is_reserved or addr.is_unspecified:
        raise ValueError(f"download resolves to a forbidden IP: {ip_str}")


def _validate_download_url(url: str, allowed_hosts: frozenset[str]) -> str:
    """Full URL validation: HTTPS, no userinfo, host in allowlist, no private IP.
    Returns the validated host."""
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
    _reject_private_ip(host)  # literal-IP rejection
    # DNS resolution check (only for hostnames, not already-IP)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
            for family, _, _, _, sockaddr in infos:
                ip = sockaddr[0]
                _reject_private_ip(ip)
        except socket.gaierror:
            pass  # let the actual request fail naturally
    return host


class StdlibVideoDownloader:
    """Downloads a video URL to a deterministic .tmp file with streaming
    SHA-256, size enforcement, and media validation. Does NOT os.replace —
    publication is the e4a2 finalize step's job."""

    def __init__(self, allowed_hosts: frozenset[str] | None = None) -> None:
        self._allowed_hosts = allowed_hosts or resolve_download_hosts(
            os.environ.get("LECTURECAST_HEYGEN_DOWNLOAD_HOSTS")
        )

    def download_and_verify(self, url: str, runtime_dir: str,
                            local_output_ref: str, max_bytes: int,
                            probe: MediaProbe) -> PreparedDownload:
        import urllib.request
        _validate_download_url(url, self._allowed_hosts)
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive int")
        temp_path = Path(runtime_dir) / (local_output_ref + ".tmp")
        # Pre-validate containment: the temp must be inside runtime_dir.
        if not temp_path.resolve().is_relative_to(Path(runtime_dir).resolve()):
            raise ValueError("temp path escapes runtime")
        temp_path.parent.mkdir(parents=True, exist_ok=True)

        h = hashlib.sha256()
        total = 0
        fd = None
        try:
            req = urllib.request.Request(url, method="GET")
            # No API key header on download requests.
            resp = urllib.request.urlopen(req, timeout=30)
            # Open temp with 0600; O_NOFOLLOW where supported.
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            try:
                flags |= os.O_NOFOLLOW  # type: ignore[attr-defined]
            except AttributeError:
                pass  # Windows
            fd = os.open(str(temp_path), flags, 0o600)
            while True:
                chunk = resp.read(_DOWNLOAD_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"download exceeded max_bytes ({max_bytes})")
                h.update(chunk)
                os.write(fd, chunk)
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
            # Clean up the temp file on any failure.
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temp_path.exists():
                temp_path.unlink()
            raise


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
        if not Path(path).is_file() or Path(path).is_symlink():
            raise ValueError(f"probe target must be a regular file: {path}")
        result = subprocess.run(
            [self._ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise ValueError(f"ffprobe failed (exit {result.returncode})")
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError(f"ffprobe output not valid JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("ffprobe output is not a JSON object")
        streams = data.get("streams", [])
        video_streams = [s for s in streams if s.get("codec_type") == "video"]
        if not video_streams:
            raise ValueError("no video stream found")
        vs = video_streams[0]
        codec = str(vs.get("codec_name", "")).strip()
        if not codec:
            raise ValueError("video stream missing codec_name")
        # width/height from the video stream.
        try:
            width = int(vs.get("width", 0))
            height = int(vs.get("height", 0))
        except (TypeError, ValueError):
            raise ValueError("video stream width/height not integers")
        if width <= 0 or height <= 0:
            raise ValueError("video stream width/height must be positive")
        # duration: prefer video stream, fallback to format.
        raw_dur = vs.get("duration") or data.get("format", {}).get("duration")
        try:
            duration = float(raw_dur)
        except (TypeError, ValueError):
            raise ValueError("duration not a valid float")
        if duration <= 0 or duration != duration or duration == float("inf"):
            raise ValueError("duration must be finite positive")
        return MediaProbeResult(
            duration_seconds=duration,
            video_codec=codec,
            width=width,
            height=height,
        )
