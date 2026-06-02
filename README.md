# Lecturecast

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-early%20access-orange.svg)](https://lecturecast.agentmesh360.com)
[![Brand](https://img.shields.io/badge/brand-AgentMesh-6E4AFF.svg)](https://agentmesh360.com)

> One prompt → finished 5-minute course video for both **Bilibili** (16:9) and **Xiaohongshu** (9:16). Built to be controlled by your AI agent (Claude Code, OpenClaw, Cursor, Codex) from chat.

![Lecturecast demo — side-by-side Bilibili and Xiaohongshu output](assets/demo.gif)

<sub>↑ Same script, two visual systems. Left: Bilibili 1920×1080. Right: Xiaohongshu 1080×1920. Played at 12× speed — actual length ~5:21.</sub>

**Core loop**: Topic → 7-section draft script (cloud) → your approval → TTS + scenes + rendering (cloud) → mp4s downloaded to your machine.

**Architecture in one breath**: The CLI is thin — it sends your topic to `api.lecturecast.agentmesh360.com`, polls for the script draft, shows it in your terminal for approval, then waits for the cloud worker to render and downloads the final mp4s + covers when ready. No local Docker / Playwright / Remotion / Python setup.

This is a product under the **[AgentMesh](https://agentmesh360.com)** umbrella — a series of vertical AI agents for specific industries. Your AgentMesh subscription's credit pool is **shared across all products**: Pro $9.9/mo gives you 1,500 credits = 30 Lecturecast videos OR 1,500 Job Agent applications OR a mix.

> **⚠️ Early access**. M1 phase — premium tier is complimentary. Request a license key from the maintainer or wait for the public form at `lecturecast.agentmesh360.com`.

---

## Architecture — two repos + one platform

| Repo | Visibility | What it holds |
|------|-----------|---------------|
| **Lecturecast CLI** (this repo) | Public · Apache 2.0 | Thin client. Talks to the cloud API. No rendering happens locally. |
| **Server** | **Private** | Closed-source IP: script & scene prompts, HTML/Remotion templates, Edge TTS pipeline, Playwright recorder, libass subtitle burner. |
| **agentmesh-core** | **Private** | Shared identity, subscriptions, credits across all AgentMesh products. |

---

## Install

### One-liner (recommended)

**macOS / Linux** (Terminal):

```bash
curl -fsSL https://raw.githubusercontent.com/jiyangnan/AgentMesh-Lecturecast/main/scripts/install.sh | bash
```

**Windows** (PowerShell):

```powershell
irm https://raw.githubusercontent.com/jiyangnan/AgentMesh-Lecturecast/main/scripts/install.ps1 | iex
```

After install, open a new terminal and run:

```bash
lecturecast init --key lc_live_xxxxxxxx   # paste the key the maintainer sent
```

### Manual

```bash
git clone https://github.com/jiyangnan/AgentMesh-Lecturecast.git
cd AgentMesh-Lecturecast
python -m venv .venv
source .venv/bin/activate
pip install -e .
lecturecast init --key lc_live_xxxxxxxx
```

---

## Use

```bash
$ lecturecast new "RAG 工作原理"
→ submitting … job_id=lct_5xz9k1
→ drafting script (~90s) ⠋
→ ┌─ Draft (7 sections, ~5 min) ────┐
  │ § 1 (24s) Hook                    │
  │ § 2 (38s) What is RAG             │
  │ § 3 (58s) Embeddings explained    │
  │ § 4 (62s) Retrieval step          │
  │ § 5 (60s) Generation step         │
  │ § 6 (40s) Real example            │
  │ § 7 (18s) Recap + next            │
  └───────────────────────────────────┘
[Y] approve  [E] edit  [N] reject  > Y
→ rendering bilibili ……………… 42%
→ rendering xiaohongshu …… 73%
→ burning subtitles + covers … 91%
→ downloading … ✓
✓ 4 files in ~/lecturecast/RAG-工作原理/
  → bilibili.mp4 (13 MB · 5:21)
  → xiaohongshu.mp4 (20 MB · 5:21)
  → cover-bilibili.png
  → cover-xiaohongshu.png
```

Other commands:

| Command | What it does |
|---|---|
| `lecturecast new "TOPIC"` | Start a new course |
| `lecturecast new "TOPIC" --depth hands_on --platforms xiaohongshu` | Customize |
| `lecturecast new --script ./my-script.json` | Skip draft, use your own |
| `lecturecast list` | History |
| `lecturecast get <job_id>` | Re-download outputs |
| `lecturecast usage` | Credits remaining this month |
| `lecturecast status` | Cloud + token health check |

---

## Pricing — shared with all AgentMesh products

| Tier | Monthly credits | Lecturecast 5-min video cost | Job Agent application cost |
|---|---|---|---|
| Free | 100 | 50 credits each (= 2 / month) | 1 credit each |
| Pro $9.9/mo | 1,500 | (= 30 videos / month) | |
| Team $39/mo | 8,000 | (= 160 videos / month) | |

You buy one subscription. Credits are spent across whichever product you use.

**M1 phase**: premium is free with a license key while we collect feedback.

---

## Use it from your AI agent

Lecturecast ships with an agent skill at `skills/claude-code/SKILL.md`. After
`lecturecast init`, drop a symlink:

```bash
ln -s "$(pwd)/skills/claude-code" ~/.claude/skills/lecturecast
ln -s "$(pwd)/skills/claude-code" ~/.codex/skills/lecturecast
```

Then in your agent chat:

> 做一条关于 RAG 工作原理的 5 分钟课程视频

The agent shells out to `lecturecast new` and waits for completion.

---

## Privacy

- The audio file, slide HTML, and rendered mp4s are temporarily stored on the cloud worker (24h TTL) for download. After 24h, they're auto-deleted.
- Your topic + draft script text are processed by DeepSeek V4 Flash (LLM provider) for script generation. No payment data leaves agentmesh-core's Stripe integration.
- No tracking, no telemetry beyond per-action credit metering.

---

## Roadmap

- [ ] M1: hosted service live + free license keys for early users (you're here)
- [ ] M2: Stripe self-serve checkout
- [ ] M2: webhook on completion (飞书 / Slack / email)
- [ ] M3: batch series mode (3-4 60s clips from one script)
- [ ] M3: custom theme upload (your brand colors / fonts)
- [ ] M3: direct B站/小红书publish API integration (with your auth)

## License

Apache 2.0 — see [LICENSE](LICENSE).
