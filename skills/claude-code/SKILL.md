---
name: lecturecast
description: Turn a topic into a finished 5-minute course video on both Bilibili (16:9) and Xiaohongshu (9:16), rendered fully locally. You act as the director and run the whole pipeline on this machine from the bundled templates/ (script -> TTS -> Remotion -> ffmpeg). Use when the user asks to "做一条课程视频 / 5 分钟讲清 X / 出一期教程 / make a course video / lecturecast about X".
---

# Lecturecast

Lecturecast is a **fully local, open-source** video workflow. There is no cloud
service, no account, and no API key — you act as the director and render the whole
video on this machine from the bundled `templates/`.

**Full runbook:** **[AGENTS.md](../../AGENTS.md)**
**Full pipeline:** **[docs/LOCAL-WORKFLOW.md](../../docs/LOCAL-WORKFLOW.md)**

The CLI itself is just a thin local helper (`lecturecast workflow`, `lecturecast
version`). The real work is the local pipeline below.

## When to use

User says any of:
- "做一条关于 X 的 5 分钟课程视频"
- "5 分钟讲清 X / 出一期 X 教程"
- "make a course video about X"
- "lecturecast about X"

Trigger only when the topic is **didactic** — a concept, technology, or
how-to. For viral / lifestyle / hook-driven short videos, use the
`/moneyprinter` skill (auto-clipped Pexels footage).

## Prerequisites (all local)

Check at the start; offer to install whatever's missing.

- **Node 20+** + `npm` — Remotion render (`brew install node`)
- **Python 3.11+** (venv) — `edge-tts` + the SRT/ASS converters
- **ffmpeg with libass** — subtitle burn + audio concat (`brew install ffmpeg`)
- *(optional, BYOK)* a **MiniMax** key from the user's own minimaxi.com account →
  `export MINIMAX_API_KEY=…`. No key → the free Edge voice is used automatically.

## How to run — the local pipeline

Follow **[docs/LOCAL-WORKFLOW.md](../../docs/LOCAL-WORKFLOW.md)** end to end:

1. **Scope** — platforms / depth / series brand / voice (quick gate).
2. **Script** — write a 7–8 section `scripts/bilibili.json` and surface the full
   draft to the user; wait for explicit "通过 / approved". For science / medical /
   code topics, demand user-verified text — do not hallucinate facts.
3. **Voiceover** — `python3 build_audio_mm.py` (MiniMax if `MINIMAX_API_KEY` is
   set, else free Edge).
4. **Scenes** — one Remotion project, `src/scenes/<Id>.tsx` (vertical) +
   `src/scenesH/<Id>H.tsx` (landscape).
5. **Render** — `./build_video.sh <slug>` merges audio, renders both aspect
   ratios, burns subs (ffmpeg + libass), and renders both covers.
6. **Xiaohongshu compliance pass** — grep the whole video for banned/导流 words.
7. **Deliver** — 2 mp4s + 2 covers + `publish-meta.md`, all in the working dir.

## Depth selection

| User intent | Depth |
|---|---|
| "讲清 X" / explain / introduce | `concept` (default, best for 5 min) |
| "深入讲 X" / how it works under the hood | `deep` |
| "动手 X" / write a X / hands-on | `hands_on` |

## Platform selection

- Default: both Bilibili + Xiaohongshu (script and voice are reused; only visual
  rendering doubles, so doing both is the value play).
- Bilibili-only or Xiaohongshu-only if the user asks.

## Voice selection

- **Edge** — free default, no key. Voices: `zh-CN-YunjianNeural` (sober male,
  default), `zh-CN-XiaomengNeural` (gentle female).
- **MiniMax** — BYOK upgrade, warmer MiniMax T2A. Used automatically when the user
  has their own `MINIMAX_API_KEY` set in their env. Default voice `male-qn-jingying`.

The MiniMax key is BYOK: it lives only in the **user's** env, is sent over HTTPS
to their own MiniMax account for synthesis, and is never persisted. Lecturecast
has no TTS keys of its own.

## Output location

Work in a dir **outside this repo** (e.g. `~/lecturecast-projects/<slug>/`) so
renders don't pollute the repo. Finished files land in `output/`:

- `<slug>-bilibili.mp4` / `<slug>-xiaohongshu.mp4`
- `<slug>-cover-bilibili.png` / `<slug>-cover-xiaohongshu.png`

## Do not

- Do not hardcode or commit any key — `MINIMAX_API_KEY` from env only.
- Do not put 导流 / 诱导关注 / links in the 小红书 video or description (限流 risk;
  end card = soft hook only).
- Do not hand-edit `theme.ts` `SECTIONS` — `update_theme.py` owns it.
- Do not `bun install` the Remotion project — use `npm install`.
- Do not edit files under `templates/` in place — copy them into your working dir.

## Reference

- Agent runbook: [AGENTS.md](../../AGENTS.md)
- Full local pipeline: [docs/LOCAL-WORKFLOW.md](../../docs/LOCAL-WORKFLOW.md)
- CLI repo: https://github.com/jiyangnan/AgentMesh-Lecturecast
- AgentMesh ecosystem: https://agentmesh360.com
