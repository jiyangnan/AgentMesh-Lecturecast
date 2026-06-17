# Lecturecast

English · [中文](README.zh.md)


> 🟣 Part of **[AgentMesh](https://github.com/jiyangnan/agentmesh-core)** — see the [ecosystem index](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ECOSYSTEM.md) ([中文](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ECOSYSTEM.zh.md)) for all related repos, the [roadmap](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ROADMAP.md), and [architecture](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ARCHITECTURE.md).
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-active-brightgreen.svg)](#)
[![Brand](https://img.shields.io/badge/brand-AgentMesh-6E4AFF.svg)](https://agentmesh360.com)

> An **open-source, fully local** video-production workflow for AI agents. One topic → a finished 5-minute course video for both **Bilibili** (16:9) and **Xiaohongshu** (9:16) — everything renders on **your** machine. Built to be driven by your AI agent (Claude Code, OpenClaw, Cursor, Codex) from chat.

![Lecturecast demo — side-by-side Bilibili and Xiaohongshu output](assets/demo.gif)

<sub>↑ Same script, two visual systems. Left: Bilibili 1920×1080. Right: Xiaohongshu 1080×1920. Played at 12× speed — actual length ~5:21.</sub>

**Everything is local.** There is no cloud service, no account, and no API key. Your agent acts as the director and runs the whole pipeline on your machine using the bundled `templates/`:

- **Remotion** (Node) renders the animated scenes for both aspect ratios.
- **edge-tts** (Python) does the voiceover — free by default, no setup.
- **ffmpeg** burns subtitles and stitches audio + video.

**Core loop**: topic → 7-section draft script → your approval → voiceover + scenes + rendering → finished mp4s + covers, all on your machine.

**Driving this from an AI agent?** Start with **[AGENTS.md](AGENTS.md)** and **[docs/LOCAL-WORKFLOW.md](docs/LOCAL-WORKFLOW.md)** — the complete, end-to-end how-to for producing a video locally.

---

## Install

### One-liner (recommended)

**macOS / Linux** (Terminal):

```bash
curl -fsSL https://raw.githubusercontent.com/jiyangnan/AgentMesh-Lecturecast/main/scripts/install.sh | bash
```

### Manual

```bash
git clone https://github.com/jiyangnan/AgentMesh-Lecturecast.git
cd AgentMesh-Lecturecast
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

You'll also need **Node 20+**, **Python 3.11+**, and **ffmpeg with libass** for the local render — see [docs/LOCAL-WORKFLOW.md](docs/LOCAL-WORKFLOW.md) for the one-line installs.

---

## Use

Lecturecast is **agent-driven**. The `lecturecast` CLI itself is just a thin local helper:

```bash
lecturecast workflow   # shows where the local workflow lives
lecturecast version    # installed version
```

The real work happens when your AI agent follows the local workflow. In your agent chat:

> 做一条关于 RAG 工作原理的 5 分钟课程视频

The agent reads [AGENTS.md](AGENTS.md) / [docs/LOCAL-WORKFLOW.md](docs/LOCAL-WORKFLOW.md), then drives the pipeline:

```
topic
  ▼ scope (platforms / depth / series brand / voice)
  ▼ 7-section draft script         (your approval gate)
  ▼ voiceover   python3 build_audio_mm.py   (Edge free, MiniMax optional)
  ▼ scenes      Remotion (vertical + landscape)
  ▼ render      ./build_video.sh <slug>      (ffmpeg + libass)
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

Lecturecast ships with an agent skill at `skills/claude-code/SKILL.md`. Drop a symlink:

```bash
ln -s "$(pwd)/skills/claude-code" ~/.claude/skills/lecturecast
ln -s "$(pwd)/skills/claude-code" ~/.codex/skills/lecturecast
```

Then in your agent chat:

> 做一条关于 RAG 工作原理的 5 分钟课程视频

The agent reads the runbook and drives the local pipeline to completion.

---

## Privacy

- **Everything runs and stays on your machine.** Audio, rendered mp4s, and covers are produced locally — nothing is uploaded.
- If you opt into the MiniMax voice (BYOK), your topic + script text are sent to your own MiniMax account over HTTPS for synthesis. The default Edge voice runs without any third-party account.
- No tracking, no telemetry.

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
