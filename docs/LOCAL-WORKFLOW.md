# Local workflow — render the whole video on your own machine

This is the local production stage of the paid Lecturecast workflow. Before
using it, `lecturecast onboard --json` must report `workflow.ready: true`, the
cloud Director must return a verified signed ProductionManifest, and the human
must have explicitly approved the 10-credit generation step. An AI agent then
executes that Manifest on this machine using the bundled `templates/`. Original
media, voice, editing, rendering and exports remain local.

Supported native hosts are **macOS and Windows**. Linux distributions and WSL
are not supported. See [SUPPORTED-PLATFORMS.md](SUPPORTED-PLATFORMS.md).

## What you need on the machine

Check at the start; offer to install whatever's missing.

| Tool | Why | Install |
|---|---|---|
| Node 20+ + `npm` | Remotion render | macOS: `brew install node`; Windows: install Node LTS |
| Python 3.11+ (venv) | `edge-tts`, SRT/ASS converters | macOS: `python3 -m venv .venv`; Windows: `python -m venv .venv` |
| Local `ffmpeg` **with libass** | subtitle burn + audio concat | macOS: `brew install ffmpeg-full`, then put its bin first in this shell's PATH. Windows: install a native ffmpeg build with libass. Verify either route with `lecturecast doctor`. |
| **MiniMax API key** *(optional, BYOK)* | warmer default voice | **ask your human for their own MiniMax key** (third-party, minimaxi.com) → `export MINIMAX_API_KEY=…`. No key → automatic free Edge voice. |

> **On the MiniMax key:** it is the user's own third-party account, not a
> Lecturecast secret. Read it from the environment only — never hardcode it,
> never commit it. Without it, `build_audio_mm.py` falls back to Edge TTS and the
> video still ships (just a different voice).

## The pipeline

```
topic
  │
  ▼
[1] Scope — platforms / depth / series brand / voice        (quick gate)
  ▼
[2] Outline → 7-8 section script (scripts/bilibili.json)    (user approval gate)
  ▼
[3] Voiceover — python3 build_audio_mm.py → audio/<id>.mp3 + .json (MiniMax→Edge)
  ▼
[4] Scenes — ONE Remotion project: src/scenes/<Id>.tsx (vertical)
                                  + src/scenesH/<Id>H.tsx (landscape)
  ▼
[5] build_video.sh / build_video.ps1 <slug> — merge audio · rewrite timing · SRT+ASS ·
        render VideoVertical + VideoLandscape · burn subs · 2 covers
  ▼
[6] Xiaohongshu compliance pass — banned-word grep over the WHOLE video
  ▼
[7] Deliver 2 mp4s + 2 covers
  ▼
[8] Title + description (publish-meta.md) — no links, no 导流
```

### Step 1 · Scope (ask only what changes behavior)

- **Platform(s)**: B站 only / 小红书 only / both (default both).
- **Depth**: 概念扫盲 / 原理深探 / 实战导向.
- **Series brand**: sets `BRAND` + `COLORS.accent` in `theme.ts` (see
  [palette presets](#series-palette-presets)).
- **Voice**: default MiniMax `male-qn-jingying`; Edge fallback `zh-CN-YunjianNeural`.

Cover / hook / end card are always on — don't ask.

### Step 2 · Write the script (approval gate)

Make a working dir **outside this repo** (so renders don't pollute it):

```bash
SLUG=rag
mkdir -p ~/lecturecast-projects/$SLUG && cd ~/lecturecast-projects/$SLUG
mkdir -p scripts audio assets output
```

Windows PowerShell equivalent:

```powershell
$Slug = "rag"
$Project = Join-Path $HOME "lecturecast-projects\$Slug"
New-Item -ItemType Directory -Force -Path $Project | Out-Null
Set-Location $Project
New-Item -ItemType Directory -Force -Path scripts, audio, assets, output | Out-Null
```

Write `scripts/bilibili.json`:

```json
{
  "title": "<≤10 字主标题>",
  "subtitle": "<one-line teaser>",
  "series": "AI 实战教程",
  "accent": "#FF5C00",
  "sections": [
    { "id": "01_hook", "title": "开场钩子", "text": "…(~125字,25s,0-3s 必须硬钩子)…", "visual": "…" },
    { "id": "02_…",    "title": "…",       "text": "…(~165-280字/段)…",            "visual": "…" },
    { "id": "08_end",  "title": "EndCard", "text": "…软钩子，无导流(见 Step 6)…",    "visual": "…" }
  ]
}
```

~1400 字 @ 280 字/min ≈ 5 min. Keep sentence punctuation (。！？，) — the TTS uses
sentence boundaries for subtitle cues.

**Strict rule:** do NOT hallucinate facts in didactic content. For technical /
medical / scientific / financial topics, draft the script and have the user
verify key claims (versions, dates, names, code) before audio. Surface the full
script and wait for explicit "通过 / approved". (If the user supplied a script,
skip this gate.)

### Step 3 · Voiceover (MiniMax default → Edge fallback)

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install --quiet edge-tts
cp /path/to/AgentMesh-Lecturecast/templates/shared/{build_audio_mm.py,build_srt.py,subtitle_font.py,srt_to_ass.py,srt_to_ass_vertical.py,update_theme.py,build_video.sh} .
chmod +x build_video.sh
export MINIMAX_API_KEY=…    # optional BYOK; omit to use the free Edge voice
python3 build_audio_mm.py
```

Windows PowerShell equivalent:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --quiet edge-tts
Copy-Item C:\path\to\AgentMesh-Lecturecast\templates\shared\build_audio_mm.py .
Copy-Item C:\path\to\AgentMesh-Lecturecast\templates\shared\build_srt.py .
Copy-Item C:\path\to\AgentMesh-Lecturecast\templates\shared\subtitle_font.py .
Copy-Item C:\path\to\AgentMesh-Lecturecast\templates\shared\srt_to_ass*.py .
Copy-Item C:\path\to\AgentMesh-Lecturecast\templates\shared\update_theme.py .
Copy-Item C:\path\to\AgentMesh-Lecturecast\templates\shared\build_video.ps1 .
$env:MINIMAX_API_KEY = "..."  # optional BYOK; omit for Edge
python build_audio_mm.py
```

Output: `audio/<id>.mp3` + `audio/<id>.json` (per-sentence cues) per section.
Degradation is built in: MiniMax transient/RPM error → retry 3× at 20s;
quota/auth error → permanent fallback to Edge. Per-sentence synth + ffmpeg
concat keeps cues accurate and avoids long-segment truncation.

### Step 4 · Remotion scenes (ONE project, both aspect ratios)

```bash
cp -R /path/to/AgentMesh-Lecturecast/templates/remotion/. remotion/
cd remotion
npm install --no-fund --no-audit   # NOT bun — see failure modes
npx remotion browser ensure        # explicit first-run browser download/warm-up
cd ..
lecturecast doctor --project-root "$PWD"
```

Windows PowerShell equivalent:

```powershell
Copy-Item C:\path\to\AgentMesh-Lecturecast\templates\remotion remotion -Recurse
Set-Location remotion
npm install --no-fund --no-audit
npx.cmd remotion browser ensure
Set-Location ..
lecturecast doctor --project-root (Get-Location)
```

- Set the look in `src/theme.ts`: `BRAND.series` / `BRAND.ep` / `COLORS.accent`.
  **Don't hand-edit `SECTIONS`** — `update_theme.py` fills it from the audio.
- For each section write **two** scene files:
  - `src/scenes/<Id>.tsx` — vertical (use `kit.tsx`; `Stage` reserves bottom 340px for subs)
  - `src/scenesH/<Id>H.tsx` — landscape (use `kitH.tsx`; `StageH` reserves bottom 150px)
  - register both in `src/Video.tsx` / `src/VideoH.tsx` `SCENES` maps (key = id without `NN_`).
- `Hook` / `End` ship as **working examples** — copy their structure. Use the
  `anim.ts` primitives (`reveal` / `pop` / `slideX` / `grow` / `pulse`).

**Visual rules:** cream `COLORS.bg`; one accent per series; no shadows/gradients
on B站; vertical = stacked cards + bigger type; the Hook MUST have a hard 0-3s
opener (dark bg + huge contrast text). Don't invent new color names — the palette
is deliberately constrained.

QA a still before the long render (cheap; a 6-min render to find a wrap bug is the
costliest mistake here):

```bash
cd remotion && npx remotion still VideoVertical qa/hook.png --frame=120
```

### Step 5 · Render + burn + covers (one command)

Edit the `HIGHLIGHTS` dict in `srt_to_ass_vertical.py` with this episode's key
terms first, then:

```bash
./build_video.sh $SLUG
```

Windows PowerShell:

```powershell
.\build_video.ps1 $Slug
```

The ASS generators choose a CJK-capable platform default: `Arial Unicode MS`
on macOS and `Microsoft YaHei` on Windows. Unsupported operating systems stop
instead of silently selecting an unverified font. To use another locally
installed family, set it before generating ASS:

```bash
export LECTURECAST_SUBTITLE_FONT="Your CJK Font Family"
```

Windows PowerShell uses `$env:LECTURECAST_SUBTITLE_FONT = "Your CJK Font Family"`.

LectureCast does not bundle or upload fonts. The override is read from the
current environment only.

Merges audio → `update_theme.py` → SRT + both ASS → renders `VideoVertical` +
`VideoLandscape` → burns subs (local ffmpeg+libass) → renders both covers.
Outputs `output/$SLUG-{xiaohongshu,bilibili}.mp4` + `$SLUG-cover-*.png`.

### Step 6 · Xiaohongshu compliance pass (MANDATORY, before final render)

小红书 silently rate-limits (限流) videos that break two rules:

- ❌ **No 诱导关注 + 站外导流**: never "关注/私信/评论 领取·获取·暗号". End cards
  use a **soft hook only** — no links, no repo names. Put resources in the
  profile bio, or reply privately in comments.
- ❌ **No 擦边/灰色 words**: 「扒光/扒/起底」/「爬虫/爬取」 → use 「拆解/复盘/分析/采集/还原」.
- 🔍 Grep the WHOLE video — narration, on-screen text, subtitles:
  ```bash
  grep -rno "扒\|私信\|领取\|暗号\|起底\|爬虫\|爬取\|关注我" scripts remotion/src assets/*.srt
  # expect: no matches
  ```

  Windows PowerShell equivalent:

  ```powershell
  Get-ChildItem scripts, remotion\src, assets -Recurse -File |
    Select-String -Pattern "扒|私信|领取|暗号|起底|爬虫|爬取|关注我"
  # expect: no matches
  ```

### Step 7-8 · Deliver + publish-meta

Open the 4 finals; spot-check an EndCard frame + a body frame for baked-in banned
text. Write `output/publish-meta.md`: 主推标题(B站) + 小红书短标题 + B站简介(标签) +
小红书正文(#话题). **No links, no 导流 in either** — soft hook only.

## Series palette presets

One series = one accent. Edit `remotion/src/theme.ts`:

| Series line | `accent` | Vibe |
|---|---|---|
| AI 实战教程 (手把手实战) | `#FF5C00` 赤焰橙 | hands-on, energetic |
| MCP 系列 | `#2E5BFF` 蓝 + orange | protocol / infra |
| RAG 系列 | `#7B2D8E` 紫 | retrieval / data |
| Agent 系列 | `#10B981` 翡翠绿 | autonomy / agents |

Keep `bg` cream (`#FFF7F0`), `ink` near-black. Accent appears in the brand strip,
key numbers, highlights, and the EndCard.

## Voice options

MiniMax (set `MM_VOICE` in `build_audio_mm.py`): `male-qn-jingying` (精英青年,
default), `male-qn-qingse` (青涩), `audiobook_male_1` (documentary), `female-shaonv`
(少女音). Edge (also usable as the *primary* engine with no key): `zh-CN-YunjianNeural`
(sober male, fallback default), `zh-CN-YunxiNeural` (young male), `zh-CN-XiaomengNeural`
(gentle female).

## Subtitle keyword highlights (vertical)

Edit `srt_to_ass_vertical.py`'s `HIGHLIGHTS`. ASS uses **BGR** hex, `&H00` prefix:
`#FF5C00` → BGR `00 5C FF` → `&H00005CFF&`. Longest-match-first; always reset color
after a highlighted word so it doesn't bleed.

## Common failure modes

| Symptom | Fix |
|---|---|
| MiniMax `status_code 1002 (RPM)` | rate limit — built-in 20s ×3 retry recovers; just wait |
| MiniMax quota/auth error | auto-falls back to Edge; top up or fix the key to restore MiniMax |
| `ModuleNotFoundError: edge_tts` | activate the venv (PEP 668 locks system python) |
| `Cannot find module '@rspack/binding-darwin-*'` | `bun` pruned an optional dep — `rm -rf node_modules && npm install` |
| ffmpeg `No option name near 'subtitle.ass'` | ffmpeg lacks libass — use `ffmpeg-full` on macOS or a Windows build with libass, then rerun doctor |
| Burned Chinese subtitles are squares | regenerate ASS with the platform default, or set `LECTURECAST_SUBTITLE_FONT` to a locally installed CJK family first |
| first Remotion browser connection times out after download | run `npx remotion browser ensure`, then retry the original render once; if it repeats, report the full error |
| Scene timing drifts from audio | you hand-edited `SECTIONS` — never do that; rerun `update_theme.py` |

## Don'ts

- Don't hardcode or commit any API key — `MINIMAX_API_KEY` from env only.
- Don't auto-write scripts for authoritative topics (medicine/law/finance/
  breaking-news/version-specific software) — demand user-verified text.
- Don't put 导流 / 诱导关注 / links in the 小红书 video or description.
- Don't hand-edit `theme.ts` `SECTIONS` — `update_theme.py` owns it.
- Don't `bun install` the Remotion project — use `npm install`.
- Don't edit files under `templates/`. Copy them into your working dir, then edit.
