---
name: lecturecast
description: Turn a topic into a finished 5-minute course video on both Bilibili (16:9) and Xiaohongshu (9:16). Two paths — render the whole thing locally as the director, or drive the hosted cloud service (see AGENTS.md / docs/LOCAL-WORKFLOW.md). Use when the user asks to "做一条课程视频 / 5 分钟讲清 X / 出一期教程 / make a course video / lecturecast about X".
---

# Lecturecast

Two ways to produce the video (full runbook: **[AGENTS.md](../../AGENTS.md)**):

- **Local (recommended today)** — you act as the director and render the whole
  video on this machine from the bundled `templates/`. Needs Node + ffmpeg
  (+ optional BYOK MiniMax key). Full pipeline:
  **[docs/LOCAL-WORKFLOW.md](../../docs/LOCAL-WORKFLOW.md)**.
- **Cloud** — drive the `lecturecast` CLI against the hosted service, zero local
  setup. *(Server-side render isn't live yet — for guaranteed output today, use
  the local path.)*

The sections below cover the **cloud** CLI. For the local path, follow
LOCAL-WORKFLOW.md.

## When to use

User says any of:
- "做一条关于 X 的 5 分钟课程视频"
- "5 分钟讲清 X / 出一期 X 教程"
- "make a course video about X"
- "lecturecast about X"

Trigger only when the topic is **didactic** — a concept, technology, or
how-to. For viral / lifestyle / hook-driven short videos, use the
`/moneyprinter` skill (auto-clipped Pexels footage).

## Prerequisites

- `lecturecast` CLI on PATH (run `curl -fsSL https://raw.githubusercontent.com/jiyangnan/AgentMesh-Lecturecast/main/scripts/install.sh | bash` if missing)
- **AgentMesh360 account key** configured (`~/.lecturecast/config.toml` has `token`).
  Free during open beta — the user signs up at <https://agentmesh360.com/app/>
  and runs `lecturecast init --key <account_key>`.
- *(Optional, BYOK)* For the warmer **MiniMax** voice instead of the free Edge
  default, ask the user for a **MiniMax** key (their own, from minimaxi.com) and
  `export MINIMAX_API_KEY=…` before running.

Verify before running: `lecturecast status` — should return ok.
If not, ask the user to run `lecturecast init --key <account_key>` first.

> **Full agent runbook** — install, workflow, BYOK, troubleshooting: see
> [`AGENTS.md`](../../AGENTS.md) at the repo root.

## How to run

For a typical request like "做一条关于 RAG 工作原理的 5 分钟课程视频":

```bash
lecturecast new "RAG 工作原理" --depth concept --platforms bilibili,xiaohongshu
```

The CLI is **interactive** — it will print the 7-section draft script and ask
for [Y]/[E]/[N]. **Surface the draft to the user in chat and pass their
response back to the CLI.** (You will need to break this into two CLI calls
or pipe approval via `--yes` if the user pre-approves.)

For **user-provided scripts** (recommended for science/medical/code topics):

```bash
lecturecast new "TOPIC" --script ./script.json
```

## Depth selection

| User intent | `--depth` |
|---|---|
| "讲清 X" / explain / introduce | `concept` (default, best for 5 min) |
| "深入讲 X" / how it works under the hood | `deep` |
| "动手 X" / write a X / hands-on | `hands_on` |

## Platform selection

- Default: `bilibili,xiaohongshu` (both)
- Bilibili-only: `--platforms bilibili`
- Xiaohongshu-only: `--platforms xiaohongshu`

The script and voice are reused across platforms; only visual rendering doubles,
so doing both is the value play. (Free during open beta — no credits enforced.)

## Voice selection

Two engines, auto-selected (or forced with `--engine`):

- `edge` — **free default**, no key. Voices: `zh-CN-YunjianNeural` (sober male,
  default), `zh-CN-XiaomengNeural` (gentle female).
- `minimax` — **BYOK upgrade**, warmer MiniMax T2A. Used automatically when the
  user has `MINIMAX_API_KEY` set in their env (see Prerequisites). Default voice
  `male-qn-jingying`.

```bash
lecturecast new "TOPIC"                              # Edge (free), or MiniMax if MINIMAX_API_KEY is set
lecturecast new "TOPIC" --engine minimax             # force MiniMax (needs MINIMAX_API_KEY)
lecturecast new "TOPIC" --engine edge --voice zh-CN-XiaomengNeural
```

The MiniMax key is BYOK: it lives only in the **user's** env, is sent over HTTPS
for that one job, and is never persisted. The CLI never handles any of our own
TTS keys — MiniMax is the user's third-party account, not a Lecturecast secret.

## Output location

CLI downloads finished files to `~/lecturecast/<topic>/`:

- `<platform>.mp4`
- `cover-<platform>.png`

## Failure modes

| Symptom | Action |
|---|---|
| `RuntimeError: No token configured` | User needs `lecturecast init --key <account_key>` |
| HTTP 401 from CLI | Token invalid/expired — re-init |
| HTTP 402 "insufficient_credits" | (rare in open beta) Out of credits — top up at `https://agentmesh360.com/account` |
| Cloud `new` queues but never renders | Expected for now — server render isn't live. Use the local path (LOCAL-WORKFLOW.md). |

## Do not

- Do not hardcode or commit any API key — `MINIMAX_API_KEY` from env only.
- Do not put 导流 / 诱导关注 / links in the 小红书 video or description (限流 risk;
  end card = soft hook only).
- Do not run more than 2 concurrent cloud `lecturecast new` calls during open
  beta (small server worker pool).

## Reference

- Public docs: https://lecturecast.agentmesh360.com
- CLI repo: https://github.com/jiyangnan/AgentMesh-Lecturecast
- AgentMesh hub: https://agentmesh360.com (shared subscription across products)
