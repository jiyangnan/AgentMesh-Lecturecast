# AGENTS.md — driving Lecturecast as an AI agent

You are an AI agent (Claude Code / OpenClaw / Cursor / Codex …) running this CLI
on behalf of your human. This file tells you **what to install, what you'll need
from your human, and the exact workflow** — so you don't get stuck mid-task.

## TL;DR

Lecturecast turns one topic into a finished 5-minute course video in **both**
Bilibili 16:9 and Xiaohongshu 9:16. **All rendering happens in the cloud** — your
machine renders nothing. Your job:

1. Install the CLI (one line).
2. Get an **AgentMesh360 account key** from your human → `lecturecast init`.
3. *(Optional)* Want a warmer voice? Get a **MiniMax key** from your human →
   `export MINIMAX_API_KEY=…`.
4. `lecturecast new "你的主题"` and drive the approval gate.

## 1. What this is — and what you must install locally

A **thin client** that talks to `api.lecturecast.agentmesh360.com`. You do **NOT**
need Node, ffmpeg, Remotion, Docker, or any heavy setup locally — those all live
on the server. You only need:

- This CLI — install in one line:
  ```bash
  curl -fsSL https://raw.githubusercontent.com/jiyangnan/AgentMesh-Lecturecast/main/scripts/install.sh | bash
  ```
- Python 3.11+ (the CLI itself runs on it).

## 2. Connect — the one required key

**AgentMesh360 account API key — required.**

- Ask your human for it. If they don't have one, they create a free account at
  <https://agentmesh360.com/app/> (free during open beta) and copy the key from
  the account dashboard.
- Then:
  ```bash
  lecturecast init --key <their_account_key>
  lecturecast status      # should report ok
  ```
- **If you have no key, stop and ask your human.** Don't guess or retry blindly.

## 3. The workflow — what one command does (so you can narrate it)

`lecturecast new "主题"` kicks off this cloud pipeline. You don't run the steps —
you just understand them and drive the approval gate:

1. **Script** — the cloud LLM drafts a 7-section script.
2. **Approval gate** — the CLI prints the draft and asks `[Y]/[E]/[N]`. **Show
   the draft to your human and pass back their choice** (or pre-approve with
   `--yes` if they told you to).
3. **Voiceover** — Edge TTS by default (free); MiniMax if a key was provided
   (see §4), auto-falling back to Edge on any MiniMax error.
4. **Render** — ONE Remotion project produces **both** aspect ratios at once
   (B站 1920×1080 + 小红书 1080×1920) from the same narration.
5. **Subtitles + covers** — burned in (local libass on the server) + two covers.
6. **Compliance** — a Xiaohongshu 限流-word gate scans the whole video.
7. **Download** — finished files land in `~/lecturecast/<topic>/`.

## 4. Voiceover & BYOK — ask your human for a MiniMax key to upgrade the voice

- **Default: Edge TTS.** Free, zero config, always works. Good enough to ship.
- **Upgrade: MiniMax T2A.** Warmer, less robotic. This is **BYOK** (bring your
  own key):
  1. Ask your human for a **MiniMax API key** — it's a third-party service
     (<https://www.minimaxi.com>) that *they* register for. **This is not our
     product key**, and it is not provided by Lecturecast.
  2. Set it in your shell:
     ```bash
     export MINIMAX_API_KEY=<their_minimax_key>
     ```
  3. Run `lecturecast new …` normally — the CLI auto-detects the key and switches
     voiceover to MiniMax. No flag needed.
- **How the key is handled (important):** it lives only in your shell env, is sent
  over HTTPS for that single job, is **never written to disk or our config**, and
  the server **discards it when the job finishes**. Don't persist it anywhere.
- **Explicit control:** `--engine edge|minimax` and `--voice <id>` override the
  auto-detection.

## 5. Checklist — what to get from your human

| Thing | Required? | How they get it | Used for |
|---|---|---|---|
| AgentMesh360 account key | **Yes** | Free signup at agentmesh360.com | Auth + usage metering |
| MiniMax API key | Optional | Their own signup at minimaxi.com | Upgrade voiceover to MiniMax |

## 6. When something's missing — self-serve table

| Symptom | What to do |
|---|---|
| `No token configured` | Ask your human for the AgentMesh key → `lecturecast init --key …` |
| HTTP 401 | Key invalid/expired — ask your human for a fresh one, re-`init` |
| HTTP 402 `insufficient_credits` | (rare during open beta) Tell your human to top up at agentmesh360.com/account |
| `--engine minimax` warned + fell back to Edge | No `MINIMAX_API_KEY` set. Want MiniMax? Ask your human for a key. Otherwise ignore — Edge still ships the video. |
| Render takes >15 min | Cloud queue is busy; the CLI keeps polling. No action needed. |

## 7. Don'ts

- **Never** hardcode any key into scripts, the repo, or a commit. Env vars only.
- **Don't** try to render locally — there is no local render path; rendering is
  closed-source and cloud-side.
- **Don't** fabricate scripts to bypass the API.
- **Don't** run more than ~2 concurrent `lecturecast new` calls (small beta worker pool).

## More

- Human-facing docs: [README.md](README.md) · [中文](README.zh.md)
- Agent skill (auto-loaded by Claude Code / Codex): [skills/claude-code/SKILL.md](skills/claude-code/SKILL.md)
