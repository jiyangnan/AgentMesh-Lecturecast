# AGENTS.md — driving Lecturecast as an AI agent

## System boundary — read before any change

Before changing product scope, hosting, DNS, Caddy, deployment, credits, or
media handling, read
[`docs/LECTURECAST-SYSTEM-BOUNDARY.md`](docs/LECTURECAST-SYSTEM-BOUNDARY.md).

- The official site is served through the existing AgentMesh360
  `jobagent-caddy`; GitHub Pages is not a production origin.
- `site/` is the canonical website source, while `agentmesh-core` owns
  production publishing, gateway credentials, verification, and rollback.
- Never add a production `CNAME`, production SSH Secret, second Caddy, media
  upload, or cloud rendering path here.

You are an AI agent (Claude Code / OpenClaw / Cursor / Codex …) running Lecturecast
on behalf of your human. **LectureCast Community is fully local and open source**:
no account and no LectureCast API key. The optional **Director** route can provide
cloud creative decisions and a signed declarative Manifest, while raw media,
voice, editing and rendering still remain on this machine. This file tells you
how to produce the video, what to install, and what to ask your human for.

Lecturecast turns one topic into a finished 5-minute course video in **both**
Bilibili 16:9 and Xiaohongshu 9:16 (script → voiceover → animated scenes → burned
subtitles → dual covers → compliant end card).

**You are the director.** You write the script and design the scenes; the bundled
`templates/` give you the Remotion project, the TTS/render scripts, and working
Hook/End scenes to copy. There is no "do-it-all" command — didactic visuals need
per-topic design, and that's your job.

Voiceover is **Edge TTS (free) by default**, upgradable to **MiniMax** if your
human gives you their own `MINIMAX_API_KEY` (see [Voiceover & BYOK](#voiceover--byok)).

## Produce a video — the workflow

1. **Install tools** (offer to install whatever's missing):
   - Node 20+ + npm — `brew install node`
   - Python 3.11+ — for `edge-tts` + the SRT/ASS converters
   - ffmpeg **with libass** — `brew install ffmpeg`
   - *(optional)* a MiniMax key from your human → `export MINIMAX_API_KEY=…`
2. **Follow the full pipeline** in **[docs/LOCAL-WORKFLOW.md](docs/LOCAL-WORKFLOW.md)**:
   scope → script (approval gate) → `build_audio_mm.py` → Remotion scenes →
   `build_video.sh` → Xiaohongshu compliance grep → deliver 2 mp4s + 2 covers.

Everything runs on this machine. Two finished videos + two covers land in your
working dir.

## Optional Director route

Use Director only when the user wants guided choices and explicitly accepts the
paid Manifest step. Follow [skills/shared/director-workflow.md](skills/shared/director-workflow.md).
Use the host-specific Skill for native choices:

- [Codex](skills/codex/SKILL.md)
- [Claude Code](skills/claude-code/SKILL.md)
- [OpenClaw](skills/openclaw/SKILL.md)

The local project, not conversation history, is the durable state source. Never
upload original media, never put `LECTURECAST_API_KEY` in arguments or files, and
never create a second generation ID after a timeout.

## Voiceover & BYOK — ask your human for a MiniMax key to upgrade the voice

- **Default: Edge TTS.** Free, no key, always works. Good enough to ship.
- **Upgrade: MiniMax T2A** (warmer). This is **BYOK** — bring your own key:
  1. Ask your human for a **MiniMax API key** — a third-party service
     (<https://www.minimaxi.com>) *they* register for. **It is not our product
     key** and is not provided by Lecturecast.
  2. `export MINIMAX_API_KEY=<their_minimax_key>`
  3. `build_audio_mm.py` auto-detects it; no key → it uses the free Edge voice.
- **The key is never persisted** — env only, never written to disk/config/repo.

## Checklist — what to get from your human

| Thing | Required? | How they get it | Used for |
|---|---|---|---|
| Node + ffmpeg installed | Yes | `brew install node ffmpeg` | Local render |
| MiniMax API key | Optional | Their own signup at minimaxi.com | Upgrade voiceover to MiniMax |

## When something's missing

| Symptom | What to do |
|---|---|
| MiniMax warned + fell back to Edge | No `MINIMAX_API_KEY` set. Want MiniMax? Ask your human for a key. Else ignore — Edge still ships. |
| `bun` / `@rspack/binding` error | Use `npm install`, not bun. See LOCAL-WORKFLOW failure modes. |
| ffmpeg `No option name near 'subtitle.ass'` | System ffmpeg lacks libass — use `brew install ffmpeg`. |
| `ModuleNotFoundError: edge_tts` | Activate the Python venv (PEP 668 locks system python). |

## Don'ts

- **Never** hardcode or commit any key. Env vars only (`MINIMAX_API_KEY`).
- **Don't** put 导流 / 诱导关注 / links in the 小红书 video or description — it
  gets the note rate-limited (限流). End card = soft hook only.
- **Don't** fabricate scripts for authoritative topics (medicine/law/finance/
  version-specific software) — demand user-verified text.
- **Don't** edit files under `templates/` in place — copy them into your working
  dir, then edit.

## More

- Local pipeline (full): [docs/LOCAL-WORKFLOW.md](docs/LOCAL-WORKFLOW.md)
- Human-facing docs: [README.md](README.md) · [中文](README.zh.md)
- Agent Skills: [Codex](skills/codex/SKILL.md) · [Claude Code](skills/claude-code/SKILL.md) · [OpenClaw](skills/openclaw/SKILL.md)
