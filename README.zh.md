# Lecturecast

[English](README.md) · 中文


> 🟣 Part of **[AgentMesh](https://github.com/jiyangnan/agentmesh-core)** — see the [ecosystem index](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ECOSYSTEM.md) ([中文](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ECOSYSTEM.zh.md)) for all related repos, the [roadmap](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ROADMAP.md), and [architecture](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ARCHITECTURE.md).
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-active-brightgreen.svg)](#)
[![Brand](https://img.shields.io/badge/brand-AgentMesh-6E4AFF.svg)](https://agentmesh360.com)

> 一个**开源、完全本地**的视频制作工作流，专门给 AI agent 用。一句话生成可发的 5 分钟课程视频——**B 站 16:9** + **小红书 9:16** 两条同时出，全程在**你自己机器上**渲染。设计成让你的 AI agent（Claude Code / OpenClaw / Cursor / Codex）从聊天里直接驱动。

![Lecturecast demo — 两条成片并排](assets/demo.gif)

<sub>↑ 同一份脚本，两套视觉系统。左：B 站 1920×1080。右：小红书 1080×1920。12 倍速播放，原片各 ~5:21。</sub>

**全程本地。** 没有云端服务、没有账户、没有 API key。你的 agent 当导演，用仓库自带的 `templates/` 在你本机跑完整条流水线：

- **Remotion**（Node）渲染两个比例的动画场景。
- **edge-tts**（Python）配音——默认免费、零配置。
- **ffmpeg** 烧字幕、拼接音视频。

**核心流程**：你给主题 → 起 7 段草稿脚本 → 你审批 → 配音 + 场景 + 渲染 + 烧字幕 + 封面 → 成片 mp4 + 封面全部落到你本机。

**用 AI agent 驱动？** 先看 **[AGENTS.md](AGENTS.md)** 和 **[docs/LOCAL-WORKFLOW.md](docs/LOCAL-WORKFLOW.md)**——一条完整、端到端的本地出片指南。

---

## 安装

### 一行装好（推荐）

**macOS / Linux**（Terminal）：

```bash
curl -fsSL https://raw.githubusercontent.com/jiyangnan/AgentMesh-Lecturecast/main/scripts/install.sh | bash
```

### 手动安装

```bash
git clone https://github.com/jiyangnan/AgentMesh-Lecturecast.git
cd AgentMesh-Lecturecast
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

本地渲染还需要 **Node 20+**、**Python 3.11+**、**带 libass 的 ffmpeg**——一行安装命令见 [docs/LOCAL-WORKFLOW.md](docs/LOCAL-WORKFLOW.md)。

---

## 用

Lecturecast 是 **agent 驱动**的。`lecturecast` CLI 本身只是个本地辅助小工具：

```bash
lecturecast workflow   # 本地工作流在哪
lecturecast version    # 当前版本
```

真正干活的是你的 AI agent 跑本地工作流。跟 agent 说：

> 做一条关于 RAG 工作原理的 5 分钟课程视频

Agent 会读 [AGENTS.md](AGENTS.md) / [docs/LOCAL-WORKFLOW.md](docs/LOCAL-WORKFLOW.md)，然后驱动整条流水线：

```
主题
  ▼ 定范围（平台 / 深度 / 系列品牌 / 嗓音）
  ▼ 7 段草稿脚本           （你审批）
  ▼ 配音   python3 build_audio_mm.py   （Edge 免费、MiniMax 可选）
  ▼ 场景   Remotion（竖版 + 横版）
  ▼ 渲染   ./build_video.sh <slug>      （ffmpeg + libass）
  ▼ 工作目录里出 2 个 mp4 + 2 张封面
```

### 配音 — 默认免费，MiniMax 可选（BYOK 自带 key）

配音默认走 **Edge TTS**（免费、零配置）。想升级到更暖更自然的 **MiniMax** 音色，
就自带一个 MiniMax key——它是 [minimaxi.com](https://www.minimaxi.com) 的第三方账户
（你自己注册，不是 Lecturecast 的密钥）。设到环境变量里，本地的 `build_audio_mm.py`
会自动启用：

```bash
export MINIMAX_API_KEY=<你自己的-minimax-key>   # 只在你本机的环境变量里，绝不落盘
```

key 只留在你的环境变量里，出错自动回退免费的 Edge 音色。
**用 AI agent 来驱动？请读 [AGENTS.md](AGENTS.md)**——涵盖安装、完整本地工作流、BYOK 与排障。

---

## 让你的 AI agent 来调

仓库自带 agent skill，路径 `skills/claude-code/SKILL.md`。建个软链接：

```bash
ln -s "$(pwd)/skills/claude-code" ~/.claude/skills/lecturecast
ln -s "$(pwd)/skills/claude-code" ~/.codex/skills/lecturecast
```

然后跟 agent 说：

> 做一条关于 RAG 工作原理的 5 分钟课程视频

Agent 会读 runbook 并驱动本地流水线跑到完成。

---

## 隐私

- **全程在你机器上跑、留在你机器上**。音频、成片 mp4、封面都本地生成——不上传任何东西。
- 如果你选用 MiniMax 音色（BYOK），你的主题 + 脚本文本会经 HTTPS 发到**你自己的** MiniMax 账户做合成。默认 Edge 音色无需任何第三方账户。
- 无追踪、无遥测。

---

## 协议

Apache 2.0 — 见 [LICENSE](LICENSE)。
