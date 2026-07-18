# Lecturecast

English · [中文](README.zh.md)


> 🟣 Part of **[AgentMesh](https://github.com/jiyangnan/agentmesh-core)** — see the [ecosystem index](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ECOSYSTEM.md) ([中文](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ECOSYSTEM.zh.md)) for all related repos, the [roadmap](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ROADMAP.md), and [architecture](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ARCHITECTURE.md).
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-active-brightgreen.svg)](#)
[![Brand](https://img.shields.io/badge/brand-AgentMesh-6E4AFF.svg)](https://agentmesh360.com)
[![Website](https://img.shields.io/badge/website-lecturecast.agentmesh360.com-CC785C.svg)](https://lecturecast.agentmesh360.com)

> An **open-source, fully local** video-production workflow for AI agents. One topic → a finished 5-minute course video for both **Bilibili** (16:9) and **Xiaohongshu** (9:16) — everything renders on **your** machine. Built to be driven by your AI agent (Claude Code, OpenClaw, Cursor, Codex) from chat.

Website: **[lecturecast.agentmesh360.com](https://lecturecast.agentmesh360.com)** · AgentMesh360 main site: **[agentmesh360.com](https://agentmesh360.com)**

![Lecturecast demo — side-by-side Bilibili and Xiaohongshu output](assets/demo.gif)

<sub>↑ Same script, two visual systems. Left: Bilibili 1920×1080. Right: Xiaohongshu 1080×1920. Played at 12× speed — actual length ~5:21.</sub>

**Community stays fully local.** It needs no account or LectureCast API key. Your agent can run the whole pipeline on your machine using the bundled `templates/`:

- **Remotion** (Node) renders the animated scenes for both aspect ratios.
- **edge-tts** (Python) does the voiceover — free by default, no setup.
- **ffmpeg** burns subtitles and stitches audio + video.

**Core loop**: topic → 7-section draft script → your approval → voiceover + scenes + rendering → finished mp4s + covers, all on your machine.

**Director is optional.** It adds structured creative choices and a paid, signed declarative ProductionManifest. It receives only a bounded source summary, your stable choice IDs, the Brief and client capabilities. Original media, voice, subtitles, editing, Remotion, ffmpeg and all outputs remain local.

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

Optional Director commands use the same local project across Codex, Claude Code and OpenClaw:

```bash
lecturecast project init ./my-video --name "My video" --json
lecturecast director start ./my-video --source source-summary.json --adapter codex --json
lecturecast director resume ./my-video --adapter openclaw --json  # after a host handoff
lecturecast director next ./my-video --json
```

Set the credential with the hidden `lecturecast auth login` prompt (or `LECTURECAST_API_KEY`) and set `LECTURECAST_DIRECTOR_URL`. The API key is never written to the project. `director resume` is local and deducts no credit; it ensures the paid request uses the current host's capabilities. Run `director generate` only after approving the Brief and the fixed credit deduction.

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

The installer registers a host-specific Skill only when that agent's Skill directory already exists. It never overwrites a custom `lecturecast` Skill. Manual links are:

```bash
ln -s "$(pwd)/skills/claude-code" ~/.claude/skills/lecturecast
ln -s "$(pwd)/skills/codex" ~/.codex/skills/lecturecast
ln -s "$(pwd)/skills/openclaw" ~/.openclaw/skills/lecturecast
```

Then in your agent chat:

> 做一条关于 RAG 工作原理的 5 分钟课程视频

The agent reads the runbook and drives the local pipeline to completion.

---

## Privacy

- **Community sends nothing to a LectureCast service.** Audio, rendered mp4s, covers and original media remain local.
- If you opt into Director, only the bounded summary, stable choices, Brief and capability metadata go to the Director service. Original media, TTS files, local paths and rendered outputs are not uploaded.
- If you opt into the MiniMax voice (BYOK), your topic + script text are sent to your own MiniMax account over HTTPS for synthesis. The default Edge voice runs without any third-party account.
- No tracking, no telemetry. An invited limited-cohort participant may
  [explicitly create a local outcome receipt](docs/LOCAL-OUTCOME-EVIDENCE.md)
  and manually export a bounded anonymous report; the CLI never uploads it.

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
