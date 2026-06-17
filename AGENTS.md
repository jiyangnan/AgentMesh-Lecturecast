# AGENTS.md — driving Lecturecast as an AI agent

You are an AI agent (Claude Code / OpenClaw / Cursor / Codex …) running Lecturecast
on behalf of your human. This file tells you **the two ways to produce a video,
what to install, and what to ask your human for** — so you don't get stuck.

Lecturecast turns one topic into a finished 5-minute course video in **both**
Bilibili 16:9 and Xiaohongshu 9:16 (script → voiceover → animated scenes → burned
subtitles → dual covers → compliant end card).

## Two ways to run it — pick one

| Path | Setup | Renders where | Use when |
|---|---|---|---|
| **Local** (recommended today) | Node + ffmpeg (+ optional MiniMax key) | **This machine** — you are the director | You want guaranteed output and full control |
| **Cloud** | Just the CLI + an account key | Hosted service | Zero-setup, hands-off *(server-side render is still being built — see note)* |

> **Honest status:** the hosted cloud render is **not finished yet**. `lecturecast
> new` will draft a script and queue the job, but rendering on the server is not
> live. **For a finished video today, use the Local path.** The cloud path is
> wired end-to-end except the render step.

Both paths use **Edge TTS (free) by default**, upgradable to **MiniMax** if your
human gives you their own `MINIMAX_API_KEY` (see [Voiceover & BYOK](#voiceover--byok)).

---

## Local path — produce the whole video here (recommended)

**You are the director.** You write the script and design the scenes; the bundled
`templates/` give you the Remotion project, the TTS/render scripts, and working
Hook/End scenes to copy. There is no "do-it-all" command — didactic visuals need
per-topic design.

1. **Install tools** (offer to install what's missing):
   - Node 20+ + npm — `brew install node`
   - Python 3.11+ — for `edge-tts` + the SRT/ASS converters
   - ffmpeg **with libass** — `brew install ffmpeg`
   - *(optional)* a MiniMax key from your human → `export MINIMAX_API_KEY=…`
2. **Follow the full pipeline** in **[docs/LOCAL-WORKFLOW.md](docs/LOCAL-WORKFLOW.md)**:
   scope → script (approval gate) → `build_audio_mm.py` → Remotion scenes →
   `build_video.sh` → Xiaohongshu compliance grep → deliver 2 mp4s + 2 covers.

Everything runs locally; nothing is billed per video.

---

## Cloud path — hand it to the hosted service

1. **Install the CLI:**
   ```bash
   curl -fsSL https://raw.githubusercontent.com/jiyangnan/AgentMesh-Lecturecast/main/scripts/install.sh | bash
   ```
2. **Connect — required account key.** Ask your human for an **AgentMesh360
   account key** (free signup at <https://agentmesh360.com/app/>):
   ```bash
   lecturecast init --key <their_account_key>
   lecturecast status      # should report ok
   ```
   No key → stop and ask your human. Don't guess.
3. **Run + drive the approval gate:**
   ```bash
   lecturecast new "RAG 工作原理" --depth concept --platforms bilibili,xiaohongshu
   ```
   The CLI prints the draft script and asks `[Y]/[E]/[N]` — **show the draft to
   your human and pass back their choice** (or `--yes` if pre-approved).

The cloud pipeline (once render is live): script → voiceover (Edge default /
MiniMax if BYOK) → ONE Remotion project rendered to both aspect ratios → burned
subtitles + covers → Xiaohongshu compliance gate → files in `~/lecturecast/<topic>/`.

---

## Voiceover & BYOK — ask your human for a MiniMax key to upgrade the voice

- **Default: Edge TTS.** Free, no key, always works. Good enough to ship.
- **Upgrade: MiniMax T2A** (warmer). This is **BYOK** — bring your own key:
  1. Ask your human for a **MiniMax API key** — a third-party service
     (<https://www.minimaxi.com>) *they* register for. **It is not our product
     key** and is not provided by Lecturecast.
  2. `export MINIMAX_API_KEY=<their_minimax_key>`
  3. Local path: `build_audio_mm.py` auto-detects it. Cloud path: the CLI sends it
     over HTTPS for that one job only.
- **The key is never persisted** — env only, never written to disk/config/repo,
  and the cloud server discards it when the job finishes.

## Checklist — what to get from your human

| Thing | Required? | How they get it | Used for |
|---|---|---|---|
| Node + ffmpeg installed | Local path | `brew install node ffmpeg` | Local render |
| AgentMesh360 account key | Cloud path | Free signup at agentmesh360.com | Auth + usage metering |
| MiniMax API key | Optional (both paths) | Their own signup at minimaxi.com | Upgrade voiceover to MiniMax |

## When something's missing

| Symptom | What to do |
|---|---|
| `No token configured` (cloud) | Ask your human for the AgentMesh key → `lecturecast init --key …` |
| HTTP 401 (cloud) | Key invalid/expired — get a fresh one, re-`init` |
| Cloud `new` queues but never renders | Expected for now — server render isn't live. Use the **Local path**. |
| MiniMax warned + fell back to Edge | No `MINIMAX_API_KEY`. Want MiniMax? Ask your human. Else ignore — Edge still ships. |
| `bun` / `@rspack/binding` error (local) | Use `npm install`, not bun. See LOCAL-WORKFLOW failure modes. |

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
- Skill (auto-loaded by Claude Code / Codex): [skills/claude-code/SKILL.md](skills/claude-code/SKILL.md)
