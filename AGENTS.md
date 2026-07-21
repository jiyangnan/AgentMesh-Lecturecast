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
on behalf of your human. LectureCast is a commercial AgentMesh360 product: every
new or resumed task must bind a universal AgentMesh360 API Key, verify an active
paid account with enough shared credits, and use the cloud Director to produce a
signed declarative Manifest. Raw media, voice, editing and rendering remain on
this machine. This file tells you how to enter that workflow safely.

Lecturecast turns one topic into a finished 5-minute course video in **both**
Bilibili 16:9 and Xiaohongshu 9:16 (script → voiceover → animated scenes → burned
subtitles → dual covers → compliant end card).

The cloud Director owns the structured creative plan. You present its stable
choice cards, obtain the human's approvals, and then execute the signed Manifest
with the bundled Remotion/TTS/render tools. There is no account-free fallback.

Voiceover is **Edge TTS (free) by default**, upgradable to **MiniMax** if your
human gives you their own `MINIMAX_API_KEY` (see [Voiceover & BYOK](#voiceover--byok)).

Supported native hosts are **macOS and Windows**. Linux distributions and WSL
are not supported; read [docs/SUPPORTED-PLATFORMS.md](docs/SUPPORTED-PLATFORMS.md).

## Produce a video — the workflow

1. **Verify commercial access first:** run `lecturecast onboard --json`.
   - If `requires_user_action` is true, show its exact `user_prompt`, follow
     `next_suggested`, and stop until the human completes the action.
   - Do not create, resume, or render a project until `workflow.ready` is true.
2. **Install missing renderer tools** using `renderer.next_actions`:
   - Node 20+ + npm — macOS: `brew install node`; Windows: install Node LTS
   - Python 3.11+ — for `edge-tts` + the SRT/ASS converters
   - ffmpeg **with libass** — on current Homebrew use `brew install ffmpeg-full`,
     then `export PATH="$(brew --prefix ffmpeg-full)/bin:$PATH`; on Windows use
     a Windows build with libass and let `lecturecast doctor` verify it
   - *(optional)* a MiniMax key from your human → `export MINIMAX_API_KEY=…`
3. **Follow the Director workflow** in
   **[skills/shared/director-workflow.md](skills/shared/director-workflow.md)**:
   source summary → choice cards → Brief approval → explicit 10-credit approval →
   signed ProductionManifest.
4. **Execute the approved Manifest locally** with
   **[docs/LOCAL-WORKFLOW.md](docs/LOCAL-WORKFLOW.md)** and deliver two mp4s plus
   two covers.

Original media and all rendering remain on this machine. The commercial client
includes credential storage and signed-Manifest verification by default. Use the
host-specific Skill for native choices:

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
| AgentMesh360 paid account + universal API Key | Yes | [AgentMesh360 account center](https://agentmesh360.com/app/) | Director access and shared credits |
| Node + ffmpeg installed | Yes | macOS: Homebrew; Windows: native Node and an ffmpeg build with libass | Local render |
| MiniMax API key | Optional | Their own signup at minimaxi.com | Upgrade voiceover to MiniMax |

## When something's missing

| Symptom | What to do |
|---|---|
| `api_key_required` | Open the AgentMesh360 account center, then run `lecturecast auth login`. |
| `paid_subscription_required` / `insufficient_credits` | Follow `next_suggested` to pricing; never continue through a local-only fallback. |
| MiniMax warned + fell back to Edge | No `MINIMAX_API_KEY` set. Want MiniMax? Ask your human for a key. Else ignore — Edge still ships. |
| `bun` / `@rspack/binding` error | Use `npm install`, not bun. See LOCAL-WORKFLOW failure modes. |
| ffmpeg `No option name near 'subtitle.ass'` | System ffmpeg lacks libass — use `ffmpeg-full` on macOS or a Windows build with libass, then rerun `lecturecast doctor`. |
| Burned Chinese subtitles are squares | Keep the platform default, or set `LECTURECAST_SUBTITLE_FONT` to a locally installed CJK font family before generating ASS. macOS defaults to `Arial Unicode MS`; Windows defaults to `Microsoft YaHei`. |
| `ModuleNotFoundError: edge_tts` | Activate the Python venv (PEP 668 locks system python). |

## Don'ts

- **Never** hardcode or commit any key. Env vars only (`MINIMAX_API_KEY`).
- **Never** offer, describe, or execute an account-free LectureCast route.
- **Don't** put 导流 / 诱导关注 / links in the 小红书 video or description — it
  gets the note rate-limited (限流). End card = soft hook only.
- **Don't** fabricate scripts for authoritative topics (medicine/law/finance/
  version-specific software) — demand user-verified text.
- **Don't** edit files under `templates/` in place — copy them into your working
  dir, then edit.

## More

- Local pipeline (full): [docs/LOCAL-WORKFLOW.md](docs/LOCAL-WORKFLOW.md)
- Supported hosts: [docs/SUPPORTED-PLATFORMS.md](docs/SUPPORTED-PLATFORMS.md)
- Human-facing docs: [README.md](README.md) · [中文](README.zh.md)
- Agent Skills: [Codex](skills/codex/SKILL.md) · [Claude Code](skills/claude-code/SKILL.md) · [OpenClaw](skills/openclaw/SKILL.md)
