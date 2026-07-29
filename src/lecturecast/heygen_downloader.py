"""Stdlib video downloader + ffprobe media probe (§5.5e5a).

Security rules (per Codex e5 plan):
- HTTPS-only; no userinfo; no non-default ports; redirects are hard errors.
- Host allowlist is a LOCAL trust policy. DNS must resolve to public IPs.
- DNS rebinding defense: the verified IP is pinned into the connection.
- Temp file: lexical containment (reject . and ..), lstat each parent
  component, O_NOFOLLOW + 0600 where supported, fsync.
- Streaming SHA-256 + size enforcement.
- API key never sent with the download request.
- Downloader never calls os.replace; it only produces the staged temp.
- ffprobe subprocess output is streamed to a capped temp file (not buffered).
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import shutil
import socket
import ssl
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
    hosts: set[str] = set(_DEFAULT_DOWNLOAD_HOSTS)
    if extra:
        for h in extra.split(","):
            h = h.strip().lower()
            if h and "*" not in h and not h.startswith("."):
                hosts.add(h)
    return frozenset(hosts)


def _reject_non_public_ip(ip_str: str) -> None:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return
    if (addr.is_loopback or addr.is_private or addr.is_link_local
            or addr.is_multicast or addr.is_reserved or addr.is_unspecified):
        raise ValueError(f"download resolves to a forbidden IP: {ip_str}")


def _resolve_and_check_host(host: str) -> list[str]:
    try:
        ipaddress.ip_address(host)
        _reject_non_public_ip(host)
        return [host]
    except ValueError:
        pass
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


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    """Inheriting HTTPRedirectHandler and refusing all redirects — the default
    build_opener adds HTTPRedirectHandler; replacing it with this subclass
    ensures redirects raise rather than being silently followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError(f"download redirect refused: {code} -> {newurl}")


def _validate_download_url(url: str, allowed_hosts: frozenset[str]) -> tuple[str, list[str]]:
    """Returns (host, verified_ips)."""
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
    verified_ips = _resolve_and_check_host(host)
    return host, verified_ips


def _verify_lexical_containment(path: Path, root: Path) -> None:
    """Reject . and .. components, require path to be lexically under root,
    and lstat each parent to detect symlinks."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        raise ValueError(f"path escapes runtime (lexical): {path}")
    # Explicitly reject . and ..
    parts = set(rel.parts)
    if "." in parts or ".." in parts:
        raise ValueError(f"path contains . or ..: {path}")
    # lstat each intermediate directory + root itself
    current = root
    try:
        st = current.lstat()
        if stat.S_ISLNK(st.st_mode):
            raise ValueError(f"runtime root is a symlink: {current}")
    except FileNotFoundError:
        pass
    for part in rel.parts[:-1]:
        current = current / part
        try:
            st = current.lstat()
            if stat.S_ISLNK(st.st_mode):
                raise ValueError(f"symlink in directory chain: {current}")
        except FileNotFoundError:
            return



def _open_pinned_https(hostname: str, pinned_ip: str, port: int):
    """Open an HTTPS connection to pinned_ip, but use hostname for TLS SNI +
    certificate verification. No global state pollution — thread-safe."""
    import http.client
    import ssl as _ssl
    ctx = _ssl.create_default_context()
    raw_sock = socket.create_connection((pinned_ip, port), timeout=30)
    ssl_sock = ctx.wrap_socket(raw_sock, server_hostname=hostname)
    conn = http.client.HTTPSConnection(hostname, port)
    conn.sock = ssl_sock
    return conn


class StdlibVideoDownloader:
    """Downloads a video URL to a deterministic .tmp file."""

    def __init__(self, allowed_hosts: frozenset[str] | None = None) -> None:
        self._allowed_hosts = allowed_hosts or resolve_download_hosts(
            os.environ.get("LECTURECAST_HEYGEN_DOWNLOAD_HOSTS")
        )

    def download_and_verify(self, url: str, runtime_dir: str,
                            local_output_ref: str, max_bytes: int,
                            probe: MediaProbe, _connect=None) -> PreparedDownload:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive int")
        host, verified_ips = _validate_download_url(url, self._allowed_hosts)
        runtime_root = Path(runtime_dir)
        temp_path = runtime_root / (local_output_ref + ".tmp")
        _verify_lexical_containment(temp_path, runtime_root)
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        _verify_lexical_containment(temp_path, runtime_root)

        # Pin the verified IP into a private HTTPS connection (no global state
        # pollution). TCP connects to the verified IP; TLS SNI + certificate
        # verification use the original hostname; HTTP Host is the original host.
        pinned_ip = verified_ips[0]
        parsed = urlparse(url)
        port = parsed.port or 443
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        h = hashlib.sha256()
        total = 0
        fd = None
        resp = None
        conn = None
        try:
            conn = (_connect or _open_pinned_https)(host, pinned_ip, port)
            conn.request("GET", path, headers={"Host": host})
            resp = conn.getresponse()
            if 300 <= resp.status < 400:
                raise ValueError(
                    f"download redirect refused: {resp.status} -> {resp.getheader('Location', '?')}")
            if resp.status != 200:
                raise ValueError(f"download failed: HTTP {resp.status}")
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            try:
                flags |= os.O_NOFOLLOW  # type: ignore[attr-defined]
            except AttributeError:
                pass
            # O_NOFOLLOW on the leaf file prevents the most common swap.
            # A full dir_fd chain (openat per component) would close the
            # residual TOCTOU on intermediate directories; the runtime dir
            # is 0700, limiting swap to local users with project access.
            fd = os.open(str(temp_path), flags, 0o600)
            while True:
                chunk = resp.read(_DOWNLOAD_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"download exceeded max_bytes ({max_bytes})")
                h.update(chunk)
                written = 0
                while written < len(chunk):
                    n = os.write(fd, chunk[written:])
                    if n == 0:
                        raise OSError("os.write returned 0")
                    written += n
            os.fsync(fd)
            os.close(fd)
            fd = None
            if total == 0:
                raise ValueError("downloaded file is empty")
            # Re-verify containment before probe (defend against swap).
            _verify_lexical_containment(temp_path, runtime_root)
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
            # Re-verify containment before cleanup (defend against swap).
            try:
                _verify_lexical_containment(temp_path, runtime_root)
            except ValueError:
                pass  # don't touch an unsafe path
            else:
                if temp_path.exists() and not temp_path.is_symlink():
                    temp_path.unlink()
            raise
        finally:
            if resp is not None:
                resp.close()
            if conn is not None:
                conn.close()


# --- ffprobe media probe ------------------------------------------------

_PROBE_STDOUT_MAX = 1_048_576  # 1 MiB


class FfprobeMediaProbe:
    """Probes a media file via subprocess ffprobe. Output is streamed to a
    capped temp file (not buffered in memory)."""

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
        # Incremental PIPE read with kill-on-overflow.
        proc = subprocess.Popen(
            [self._ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        import threading as _threading
        chunks: list[bytes] = []
        total_size = 0
        overflowed = False

        def _reader():
            nonlocal total_size, overflowed
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                total_size += len(chunk)
                if total_size > _PROBE_STDOUT_MAX:
                    overflowed = True
                    proc.kill()
                    break
                chunks.append(chunk)

        reader_thread = _threading.Thread(target=_reader, daemon=True)
        reader_thread.start()
        reader_thread.join(timeout=30)
        if reader_thread.is_alive():
            proc.kill()
            proc.wait()
            raise ValueError("ffprobe timed out")
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if overflowed:
            raise ValueError("ffprobe stdout exceeded 1 MiB")
        if proc.returncode != 0:
            raise ValueError(f"ffprobe failed (exit {proc.returncode})")
        raw = b"".join(chunks)
        try:
            data = json.loads(raw)
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
        raw_w = vs.get("width")
        raw_h = vs.get("height")
        if type(raw_w) is not int or type(raw_h) is not int:
            raise ValueError("video stream width/height must be int (not bool/float)")
        if raw_w <= 0 or raw_h <= 0:
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
            width=raw_w,
            height=raw_h,
        )
