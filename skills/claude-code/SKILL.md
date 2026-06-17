---
name: lecturecast
description: Turn a topic into a finished 5-minute course video on both Bilibili (16:9) and Xiaohongshu (9:16). Uses the hosted Lecturecast service at api.lecturecast.agentmesh360.com — no local rendering. Use when the user asks to "做一条课程视频 / 5 分钟讲清 X / 出一期教程 / make a course video / lecturecast about X".
---

# Lecturecast (cloud edition)

This skill drives the `lecturecast` CLI installed in `~/.lecturecast/app`.
All script generation, TTS, scene rendering, and subtitle burning happen on
`api.lecturecast.agentmesh360.com`. No local Docker / Playwright / Remotion
needed.

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

Each video costs 50 credits regardless of platform count — the script and
voice are reused; only visual rendering doubles. Doing both is the value play.

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
| `RuntimeError: No token configured` | User needs `lecturecast init --key lc_live_xxx` |
| HTTP 401 from CLI | Token invalid/expired — re-init |
| HTTP 402 "insufficient_credits" | User out of credits — direct to `https://agentmesh360.com/account` to top up |
| Rendering takes >15 min | Server queue is congested; CLI keeps polling, no user action needed |

## Do not

- Do not attempt to run lecturecast locally without the CLI/server.
  Pre-v0.2.0 templates lived in this repo but are now in the private
  `lecturecast-server`. All rendering is server-side now.
- Do not invent prompts or scripts to bypass the API — the cost model
  depends on the server doing the LLM work.
- Do not run more than 2 concurrent `lecturecast new` calls on M1 phase
  (server worker pool is small; queue exists but UX degrades).

## Reference

- Public docs: https://lecturecast.agentmesh360.com
- CLI repo: https://github.com/jiyangnan/AgentMesh-Lecturecast
- AgentMesh hub: https://agentmesh360.com (shared subscription across products)
