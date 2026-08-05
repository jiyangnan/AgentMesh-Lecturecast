# Local workflow — execute one approved signed Manifest

This is the only production path for the commercial LectureCast client. Before
using it:

1. the exact host-Skill command, such as `lecturecast onboard --adapter codex
   --host-contract 1.0.0 --json`, reports `workflow.ready: true`;
2. the Director returned a verified signed `ProductionManifest`;
3. the human explicitly approved the per-milestone credit cost (from server pricing_estimate);
4. the complete signed script was shown and explicitly approved.

Original media, voice, subtitles, rendering and exports remain local. The cloud
never receives those files. macOS and native Windows are supported; Linux and
WSL are not. See [SUPPORTED-PLATFORMS.md](SUPPORTED-PLATFORMS.md).

## 1. Resume durable project state

```bash
lecturecast project resume ./my-video --adapter codex --host-contract 1.0.0 --json
lecturecast director resume ./my-video --adapter codex --host-contract 1.0.0 --json
lecturecast director status ./my-video --json
```

Never reconstruct IDs from chat and never copy only the final videos out of a
temporary project. The `.lecturecast/` directory is the recovery source.

## 2. Review and approve the full script

```bash
lecturecast manifest review ./my-video --json
```

The Agent must show every returned `script[].narration` and planned duration to
the human. Wait for explicit `通过 / approved`. Do not invent a replacement
script after a Director result.

After approval:

```bash
lecturecast manifest approve ./my-video \
  --confirm-reviewed-script --json
lecturecast manifest approval ./my-video --json
```

Approval is bound to the Manifest and script digests. It fails closed when the
text density cannot plausibly cover the signed timeline.

## 3. Renderer prerequisites

| Tool | Purpose |
|---|---|
| Node 20+ and npm | Remotion |
| Installer-owned Python 3.11+ with bundled `edge-tts` | local TTS |
| ffmpeg with libass | audio concat and subtitle burn |

On macOS, use `ffmpeg-full` only in the current shell when ordinary ffmpeg lacks
libass:

```bash
export PATH="$(brew --prefix ffmpeg-full)/bin:$PATH"
lecturecast doctor --project-root ./my-video
```

Do not change global Homebrew links. MiniMax is optional BYOK through the
process-only `MINIMAX_API_KEY`; without it the workflow uses Edge TTS.
The build selects the installer-owned Python environment by default, so a fresh
official install does not depend on an unrelated system or project venv.

## 4. Install the bundled Remotion project once

The public installer lives at `~/.lecturecast/app` on macOS. Copy the template
into the episode only when the episode does not already contain it:

```bash
cp -R ~/.lecturecast/app/templates/remotion/. ./my-video/remotion/
cd ./my-video/remotion
npm install --no-fund --no-audit
npx remotion browser ensure
cd -
lecturecast doctor --project-root ./my-video
```

Use npm, not bun. Do not edit the installed template in place.

## 5. Run the signed-Manifest build

macOS:

```bash
bash ~/.lecturecast/app/templates/shared/build_manifest_video.sh ./my-video
```

Windows PowerShell:

```powershell
& "$HOME\.lecturecast\app\templates\shared\build_manifest_video.ps1" `
  -ProjectRoot .\my-video
```

The build performs eight fail-closed stages:

1. verify the digest-bound full-script approval receipt;
2. verify signature, capabilities, components and static narration timing;
3. synthesize each section and measure real audio durations;
4. build a local execution plan from measured audio;
5. render both aspect ratios with that execution plan;
6. burn subtitles generated from the same timing plan;
7. render both covers;
8. validate dimensions, measured duration, audio presence and narration coverage.

The signed Manifest remains read-only. The generated
`.lecturecast/build/audio-timing.json` is a local execution artifact bound to its
digest. If TTS differs from the planned timeline by more than 25%, rendering
stops and asks for a corrected Manifest.

### BGM 配乐（CreatorCut 菜单策略 → 本地现配乐）

当项目根目录存在签名 `orchestration-plan.json`（客户端 `generation-orchestration-plan`
命令验签落盘）且 `bgm_enabled=true` 时，step 6 会按 `bgm_genre`
（`light_tech` / `bright_launch`，CreatorCut 配乐菜单）调用
`templates/shared/gen_bgm.py`（`creatorcut_local_synth`，纯 stdlib 本地程序化合成，
无下载/无 AI/无 API）现配乐，然后与旁白 noduck 混音：

- **BGM 时长** = `render_total_frames / fps`（audio-timing.json，与成片容器严格对齐）。
- **混音参数**（照 `lecturecast-server/docs/manifest-schema-v1.1.md`）：
  `[0:a]anull[a0];[a0][1:a]amix=inputs=2:duration=first:normalize=0[amx];[amx]alimiter=limit=0.89[aout]`
  —— noduck（不衰减旁白），limiter 0.89 防削波。
- **降级**：无 orchestration-plan.json 或 `bgm_enabled=false` → 保持现状（无 BGM，
  音频 `-c:a copy` 不重编码）。
- 合成器确定性（seed 42），两档听感由云端 Director 只签 `genre`（不暴露合成配方）。
- **canonical 唯一实现**：`templates/shared/gen_bgm.py` 是全链路唯一合成器（产品执行引擎 + 数字人 harness `~/heygen-presenter/scripts/gen_bgm.py` 均使用它，后者为同 md5 副本）。改 BGM 只改 canonical 并同步 harness 副本，禁止另起 numpy/soundfile 版本（双份漂移源头，2026-08-05 已统一）。

## 6. Compliance and delivery

Before delivery, scan narration, scene text and subtitles for Xiaohongshu banned
or diversion language:

```bash
grep -rno "扒\|私信\|领取\|暗号\|起底\|爬虫\|爬取\|关注我" \
  ./my-video/.lecturecast ./my-video/remotion/src ./my-video/output
```

Expected result: no matches. Deliver the two videos and two covers only after
the build's final narration-coverage validation passes.

## Failure meanings

| Failure | Meaning | Correct action |
|---|---|---|
| `narration_timing` preflight | signed text cannot cover its declared timeline | stop; request a corrected Director Manifest |
| approval missing/mismatch | full current script was not explicitly approved | review, obtain approval, record it |
| `actual_to_planned_ratio` | real TTS differs materially from the signed plan | stop before render; do not pad silence |
| `audio coverage mismatch` | final video and measured narration differ | reject output; inspect execution-plan wiring |
| missing libass/font | subtitle runtime is incomplete | fix current-shell ffmpeg/font configuration, then retry locally |
