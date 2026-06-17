"""Tiny httpx wrapper around the cloud API."""
import httpx

from . import config


class LectureCastAPI:
    def __init__(self, base: str | None = None, token: str | None = None):
        self.base = (base or config.get_api_base()).rstrip("/")
        self.token = token or config.get_token()
        if not self.token:
            raise RuntimeError(
                "No token configured. Run: lecturecast init --key lc_live_xxx"
            )

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def health(self) -> dict:
        with httpx.Client(timeout=8) as cli:
            r = cli.get(f"{self.base}/v1/health")
            return {"status": r.status_code, **(r.json() if r.is_success else {})}

    def new_course(self, *, topic: str, depth: str, platforms: list[str],
                   voice_engine: str = "edge", voice: str = "zh-CN-YunjianNeural",
                   minimax_api_key: str | None = None,
                   user_script: str | None = None) -> dict:
        body = {"topic": topic, "depth": depth, "platforms": platforms,
                "voice_engine": voice_engine, "voice": voice,
                "user_script": user_script}
        if minimax_api_key:   # BYOK — only sent when the user supplied their own key
            body["minimax_api_key"] = minimax_api_key
        with httpx.Client(timeout=30) as cli:
            r = cli.post(f"{self.base}/v1/courses", json=body, headers=self._headers())
        r.raise_for_status()
        return r.json()

    def get_course(self, job_id: str) -> dict:
        with httpx.Client(timeout=15) as cli:
            r = cli.get(f"{self.base}/v1/courses/{job_id}", headers=self._headers())
        r.raise_for_status()
        return r.json()

    def approve(self, job_id: str, *, approved: bool, edits: str | None = None) -> dict:
        with httpx.Client(timeout=15) as cli:
            r = cli.post(
                f"{self.base}/v1/courses/{job_id}/approve",
                json={"approved": approved, "edits": edits},
                headers=self._headers(),
            )
        r.raise_for_status()
        return r.json()

    def list_courses(self, limit: int = 50) -> list[dict]:
        with httpx.Client(timeout=15) as cli:
            r = cli.get(f"{self.base}/v1/courses?limit={limit}", headers=self._headers())
        r.raise_for_status()
        return r.json()
