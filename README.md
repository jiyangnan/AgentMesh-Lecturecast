# Lecturecast

English · [中文](README.zh.md)


> 🟣 Part of **[AgentMesh](https://github.com/jiyangnan/agentmesh-core)** — see the [ecosystem index](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ECOSYSTEM.md) ([中文](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ECOSYSTEM.zh.md)) for all related repos, the [roadmap](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ROADMAP.md), and [architecture](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ARCHITECTURE.md).
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-active-brightgreen.svg)](#)
[![Brand](https://img.shields.io/badge/brand-AgentMesh-6E4AFF.svg)](https://agentmesh360.com)
[![Website](https://img.shields.io/badge/website-lecturecast.agentmesh360.com-CC785C.svg)](https://lecturecast.agentmesh360.com)

> A commercial AgentMesh360 course-video product for AI agents. The cloud Director creates a signed production plan; original media, voice, editing, rendering and exports remain on **your** machine. One topic → finished 16:9 and 9:16 course videos.

Website: **[lecturecast.agentmesh360.com](https://lecturecast.agentmesh360.com)** · AgentMesh360 main site: **[agentmesh360.com](https://agentmesh360.com)**

![Lecturecast demo — side-by-side Bilibili and Xiaohongshu output](assets/demo.gif)

<sub>↑ Same script, two visual systems. Left: Bilibili 1920×1080. Right: Xiaohongshu 1080×1920. Played at 12× speed — actual length ~5:21.</sub>

Lecturecast requires a paid AgentMesh360 account, a universal API Key, and at
least 10 shared credits for each confirmed ProductionManifest. The public client
validates that commercial access before a user Agent may start production.

After the cloud Director returns a signed plan, the bundled production stack runs
on your machine:

- **Remotion** (Node) renders the animated scenes for both aspect ratios.
- **edge-tts** (Python) does the voiceover — free by default, no setup.
- **ffmpeg** burns subtitles and stitches audio + video.

**Core loop**: commercial onboarding → Director choices → Brief approval → explicit
10-credit approval → signed ProductionManifest → local voice/scenes/rendering →
finished mp4s and covers.

The Director receives only a bounded source summary, stable choice IDs, the Brief
and client capabilities. It uses the account's shared AgentMesh360 credits; there
is no separate LectureCast subscription. Original media, voice, subtitles,
editing, Remotion, ffmpeg and all outputs remain local.

**Driving this from an AI agent?** Start with **[AGENTS.md](AGENTS.md)** and the
**[Director workflow](skills/shared/director-workflow.md)**. The local production
runbook is used only after commercial onboarding succeeds.

---

## Install

Supported native hosts: **macOS and Windows**. Linux distributions and WSL are
not supported; see [Supported platforms](docs/SUPPORTED-PLATFORMS.md).

### One-liner (recommended)

**macOS** (Terminal):

```bash
curl -fsSL https://raw.githubusercontent.com/jiyangnan/AgentMesh-Lecturecast/main/scripts/install.sh | bash
```

**Windows** (PowerShell):

```powershell
irm https://raw.githubusercontent.com/jiyangnan/AgentMesh-Lecturecast/main/scripts/install.ps1 | iex
```

### Manual

macOS:

```bash
git clone https://github.com/jiyangnan/AgentMesh-Lecturecast.git
cd AgentMesh-Lecturecast
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Windows PowerShell:

```powershell
git clone https://github.com/jiyangnan/AgentMesh-Lecturecast.git
Set-Location AgentMesh-Lecturecast
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

The install includes secure credential storage and signed-Manifest verification.
You'll also need **Node 20+**, **Python 3.11+**, and **ffmpeg with libass** for the
local render; `lecturecast onboard --json` reports both commercial and renderer
readiness.

---

## Use

Lecturecast is **agent-driven**. Bind and verify commercial access first:

```bash
lecturecast auth login      # validates and stores a universal AgentMesh360 API Key
lecturecast onboard --json  # account, credits, renderer, blockers, next action
lecturecast version    # installed version
```

When `workflow.ready` is true, Director commands use the same local project across Codex, Claude Code and OpenClaw:

```bash
lecturecast project init ./my-video --name "My video" --json
lecturecast director start ./my-video --source source-summary.json --adapter codex --json
lecturecast director resume ./my-video --adapter openclaw --json  # after a host handoff
lecturecast director next ./my-video --json
```

The API key is never written to the project. The production Director URL is built
in; `LECTURECAST_DIRECTOR_URL` is a staging/development override. `director
resume` is local and deducts no credit. One confirmed ProductionManifest
generation deducts 10 credits; run `director generate` only after approving the
Brief and that deduction.

The real work happens when your AI agent follows the local workflow. In your agent chat:

> 做一条关于 RAG 工作原理的 5 分钟课程视频

The agent reads [AGENTS.md](AGENTS.md) / [docs/LOCAL-WORKFLOW.md](docs/LOCAL-WORKFLOW.md), then drives the pipeline:

```
topic
  ▼ commercial onboarding (paid account + ≥10 credits)
  ▼ Director choices + signed ProductionManifest
  ▼ scope (platforms / depth / series brand / voice)
  ▼ 7-section draft script         (your approval gate)
  ▼ voiceover   python3 build_audio_mm.py   (Edge free, MiniMax optional)
  ▼ scenes      Remotion (vertical + landscape)
  ▼ render      build_video.sh / build_video.ps1 <slug> (ffmpeg + libass)
  ▼ 2 mp4s + 2 covers in your working dir
```

### Voiceover — free by default, MiniMax optional (BYOK)

Voiceover defaults to **Edge TTS** (free, no setup). To upgrade to the warmer
**MiniMax** voice, bring your own MiniMax key — a third-party account from
[minimaxi.com](https://www.minimaxi.com), not a Lecturecast secret. Set it in
your env and the local `build_audio_mm.py` uses it automatically:

```bash
export MINIMAX_API_KEY=<your-minimax-key>   # your own key — never stored, env only
```

The key stays in your env and falls back to the free Edge voice on any error.
**Driving this from an AI agent? Read [AGENTS.md](AGENTS.md)** — it covers
install, the full local workflow, BYOK, and troubleshooting.

---

## Use it from your AI agent

The installer registers the current commercial Skill for detected agent hosts.
It never overwrites a custom `lecturecast` Skill: a conflict blocks onboarding
and prints a migration action instead of silently leaving a stale workflow.
Manual links are:

```bash
ln -s "$(pwd)/skills/claude-code" ~/.claude/skills/lecturecast
ln -s "$(pwd)/skills/codex" ~/.codex/skills/lecturecast
ln -s "$(pwd)/skills/openclaw" ~/.openclaw/skills/lecturecast
```

Then in your agent chat:

> 做一条关于 RAG 工作原理的 5 分钟课程视频

The agent runs `lecturecast onboard --json`, completes account binding when
needed, and only then drives the Director and local production pipeline.

---

## Privacy

- Only the bounded summary, stable choices, Brief and capability metadata go to the Director service. Original media, TTS files, local paths and rendered outputs are not uploaded.
- If you opt into the MiniMax voice (BYOK), your topic + script text are sent to your own MiniMax account over HTTPS for synthesis. The default Edge voice runs without any third-party account.
- No tracking, no telemetry. An invited limited-cohort participant may
  [explicitly create a local outcome receipt](docs/LOCAL-OUTCOME-EVIDENCE.md)
  and manually export a bounded anonymous report; the CLI never uploads it.

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
