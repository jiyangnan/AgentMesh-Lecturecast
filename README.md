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

Lecturecast requires an active AgentMesh360 monthly pass, a universal API Key,
and at least 10 shared credits before each explicitly approved Manifest
generation request. The public client validates that commercial access before a
user Agent may start production. `monthly_pass_required` refers to this
AgentMesh360 account entitlement; there is no separate LectureCast pass.

After the cloud Director returns a signed plan, the bundled production stack runs
on your machine:

- **Remotion** (Node) renders the animated scenes for both aspect ratios.
- **edge-tts** (Python) does the voiceover — free by default, no setup.
- **ffmpeg** burns subtitles and stitches audio + video.

**Core loop**: commercial onboarding → Director choices → Brief approval →
per-milestone credit approval (server pricing_estimate) → signed ProductionManifest → complete signed-script
approval → local voice/scenes/rendering → finished mp4s and covers.

The Director receives only a bounded source summary, stable choice IDs, the Brief
and client capabilities. It uses the account's shared AgentMesh360 credits; there
is no separate LectureCast pass. Original media, voice, subtitles,
editing, Remotion, ffmpeg and all outputs remain local.

**Driving this from an AI agent?** Start with **[AGENTS.md](AGENTS.md)** and the
**[Director workflow](skills/shared/director-workflow.md)**. The local production
runbook is used only after commercial onboarding succeeds. The
[commercial workflow lessons](docs/COMMERCIAL-WORKFLOW-LESSONS.zh.md) record the
host, credential, asset, browser, timing and delivery checks learned from a
complete real-customer run.

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
local render. In a newly started host-agent task, the installed Skill runs the
host-specific `lecturecast onboard --adapter ... --host-contract 1.0.0 --json`
command and reports adapter, commercial, and renderer readiness.

Host workflow contract `1.0.0` only attests the installer-owned Skill. Fresh
projects use Director protocol `1.1` by default; `onboard` reports the two
separately under `contracts`. Existing Director Sessions keep their protocol
locked and are not silently migrated.

---

## Use

Lecturecast is **agent-driven**. Start with `onboard`; it returns the next safe
action. Run `auth login` only when `requires_user_action` asks for a key:

```bash
lecturecast onboard --adapter codex --host-contract 1.0.0 --json
# if prompted:
lecturecast auth login
lecturecast onboard --adapter codex --host-contract 1.0.0 --json
```

When `workflow.ready` is true, execute only its returned
`workflow.next_action`. The following are examples of the commands the workflow
may return; they are not a manual sequence:

```bash
lecturecast project init ./my-video --name "My video" --adapter codex --host-contract 1.0.0 --json
lecturecast director start ./my-video --source source-summary.json --adapter codex --json
lecturecast director resume ./my-video --adapter openclaw --host-contract 1.0.0 --json  # after a host handoff
lecturecast director next ./my-video --json
```

`renderer.next_actions` repair missing local dependencies while onboarding is
blocked; they do not advance project state. Run the returned repairs, rerun
`onboard`, and resume the single `workflow.next_action` chain only after
`workflow.ready` becomes true. `--host-contract 1.0.0` attests the host Skill and
is separate from both Director protocol `1.1` and ProductionManifest schema
version `1.0`.

The API key is never written to the project. The production Director URL is built
in; `LECTURECAST_DIRECTOR_URL` is a staging/development override. `director
resume` is local and deducts no credit. One confirmed ProductionManifest
bills per milestone (charge-on-success-before-release); run `director generate` only after approving the
Brief and that deduction.

The real work happens when your AI agent follows the local workflow. In your agent chat:

> 做一条关于 RAG 工作原理的 5 分钟课程视频

The agent reads [AGENTS.md](AGENTS.md) / [docs/LOCAL-WORKFLOW.md](docs/LOCAL-WORKFLOW.md), then drives the pipeline:

```
topic
  ▼ commercial onboarding (active monthly pass)
  ▼ Director v1.1 choices (including presenter + BGM)
  ▼ signed ProductionManifest + applicable PresenterPlan / OrchestrationPlan
  ▼ complete signed script review  (your approval gate)
  ▼ per-section local TTS + measured execution timeline
  ▼ local digital-human composition / BGM mix when selected
  ▼ scenes + subtitles driven by the same measured timing plan
  ▼ render      build_manifest_video.sh / .ps1 (Remotion + ffmpeg + libass)
  ▼ narration-coverage validation + 2 mp4s + 2 covers
```

### Voiceover — free by default, MiniMax optional (BYOK)

Voiceover defaults to **Edge TTS** (free, no setup). To upgrade to the warmer
**MiniMax** voice, bring your own MiniMax key — a third-party account from
[minimaxi.com](https://www.minimaxi.com), not a Lecturecast secret. Set it in
your env and the local `build_audio_mm.py` uses it automatically:

```bash
export MINIMAX_API_KEY=<your-minimax-key>   # LectureCast does not persist it
```

LectureCast does not persist the MiniMax key. A shell may still record a literal
`export` command in history, so prefer your host's secure environment injection.
If MiniMax is unavailable, the builder reports the reason before using Edge;
verify the resulting voice rather than assuming every fallback has the same
cause.
**Driving this from an AI agent? Read [AGENTS.md](AGENTS.md)** — it covers
install, the full local workflow, BYOK, and troubleshooting.

---

## Use it from your AI agent

The installer registers the current commercial Skill for detected agent hosts.
An ordinary legacy `lecturecast` Skill directory is preserved as a timestamped
backup and replaced by the installer-owned adapter. A symlink/junction owned by
another installation remains a blocking conflict. Start a new host-agent task
after every install or upgrade; an already-running task cannot attest the new
Skill.
Manual links are:

```bash
ln -s "$(pwd)/skills/claude-code" ~/.claude/skills/lecturecast
ln -s "$(pwd)/skills/codex" ~/.codex/skills/lecturecast
ln -s "$(pwd)/skills/openclaw" ~/.openclaw/skills/lecturecast
```

Then in your agent chat:

> 做一条关于 RAG 工作原理的 5 分钟课程视频

The new agent task loads the current Skill, attests its host contract, and then
executes only the CLI's machine-returned `workflow.next_action`. Project and
render mutations fail closed if the bound Skill digest is missing or stale.

Before changing installer, host-adapter, recovery or render behavior, read the
[commercial workflow lessons and regression checklist](docs/COMMERCIAL-WORKFLOW-LESSONS.zh.md).

---

## Privacy

- Only the bounded summary, stable choices, Brief and capability metadata go to the Director service. Original media, TTS files, local paths and rendered outputs are not uploaded.
- The default Edge TTS is invoked by the local client and needs no account, but
  narration text is sent to Microsoft's Edge speech service for synthesis. It
  never receives the original media or rendered output.
- If you opt into the MiniMax voice (BYOK), the signed narration script text is
  sent to your own MiniMax account over HTTPS for synthesis.
- No tracking, no telemetry. An invited limited-cohort participant may
  [explicitly create a local outcome receipt](docs/LOCAL-OUTCOME-EVIDENCE.md)
  and manually export a bounded anonymous report; the CLI never uploads it.

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
