# Lecturecast

[English](README.md) · 中文


> 🟣 Part of **[AgentMesh](https://github.com/jiyangnan/agentmesh-core)** — see the [ecosystem index](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ECOSYSTEM.md) ([中文](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ECOSYSTEM.zh.md)) for all related repos, the [roadmap](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ROADMAP.md), and [architecture](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ARCHITECTURE.md).
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-active-brightgreen.svg)](#)
[![Brand](https://img.shields.io/badge/brand-AgentMesh-6E4AFF.svg)](https://agentmesh360.com)
[![Website](https://img.shields.io/badge/website-lecturecast.agentmesh360.com-CC785C.svg)](https://lecturecast.agentmesh360.com)

> 一个**开源、完全本地**的视频制作工作流，专门给 AI agent 用。一句话生成可发的 5 分钟课程视频——**B 站 16:9** + **小红书 9:16** 两条同时出，全程在**你自己机器上**渲染。设计成让你的 AI agent（Claude Code / OpenClaw / Cursor / Codex）从聊天里直接驱动。

官网：**[lecturecast.agentmesh360.com](https://lecturecast.agentmesh360.com)** · AgentMesh360 主站：**[agentmesh360.com](https://agentmesh360.com)**

![Lecturecast demo — 两条成片并排](assets/demo.gif)

<sub>↑ 同一份脚本，两套视觉系统。左：B 站 1920×1080。右：小红书 1080×1920。12 倍速播放，原片各 ~5:21。</sub>

**Community 全程本地。** 不需要账户，也不需要 LectureCast API Key。你的 agent 可以用仓库自带的 `templates/` 在本机跑完整条流水线：

- **Remotion**（Node）渲染两个比例的动画场景。
- **edge-tts**（Python）配音——默认免费、零配置。
- **ffmpeg** 烧字幕、拼接音视频。

**核心流程**：你给主题 → 起 7 段草稿脚本 → 你审批 → 配音 + 场景 + 渲染 + 烧字幕 + 封面 → 成片 mp4 + 封面全部落到你本机。

**Director 是可选增值路线。** 它提供结构化创作选择和付 credit 的签名声明式 ProductionManifest，只接收受限的素材摘要、稳定选项 ID、Brief 与客户端能力。原始媒体、配音、字幕、编辑、Remotion、ffmpeg 与所有成片仍留在本机。

已有 AgentMesh360 付费账户可直接使用 Director，不需要另购 LectureCast 订阅；请使用 AgentMesh360 通用 API Key，credits 与其他 AgentMesh360 产品共享。

**用 AI agent 驱动？** 先看 **[AGENTS.md](AGENTS.md)** 和 **[docs/LOCAL-WORKFLOW.md](docs/LOCAL-WORKFLOW.md)**——一条完整、端到端的本地出片指南。

---

## 安装

正式支持的原生宿主为 **macOS 和 Windows**。不支持 Linux 发行版与 WSL；边界说明见
[支持平台](docs/SUPPORTED-PLATFORMS.md)。

### 一行装好（推荐）

**macOS**（Terminal）：

```bash
curl -fsSL https://raw.githubusercontent.com/jiyangnan/AgentMesh-Lecturecast/main/scripts/install.sh | bash
```

**Windows**（PowerShell）：

```powershell
irm https://raw.githubusercontent.com/jiyangnan/AgentMesh-Lecturecast/main/scripts/install.ps1 | iex
```

### 手动安装

macOS：

```bash
git clone https://github.com/jiyangnan/AgentMesh-Lecturecast.git
cd AgentMesh-Lecturecast
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Windows PowerShell：

```powershell
git clone https://github.com/jiyangnan/AgentMesh-Lecturecast.git
Set-Location AgentMesh-Lecturecast
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

本地渲染还需要 **Node 20+**、**Python 3.11+**、**带 libass 的 ffmpeg**——一行安装命令见 [docs/LOCAL-WORKFLOW.md](docs/LOCAL-WORKFLOW.md)。
基础安装只包含完全本地的 Community 路线，不会编译可选的 Director 签名验证依赖。
手动 checkout 在首次使用 Director 前运行 `pip install -e '.[director]'`；一行安装用户可运行
`~/.lecturecast/app/.venv/bin/pip install 'cryptography>=43'`。

---

## 用

Lecturecast 是 **agent 驱动**的。`lecturecast` CLI 本身只是个本地辅助小工具：

```bash
lecturecast workflow   # 本地工作流在哪
lecturecast version    # 当前版本
```

可选 Director 在 Codex、Claude Code、OpenClaw 之间共享同一个本地项目：

```bash
lecturecast project init ./my-video --name "我的视频" --json
lecturecast director start ./my-video --source source-summary.json --adapter codex --json
lecturecast director resume ./my-video --adapter openclaw --json  # 切换宿主后
lecturecast director next ./my-video --json
```

用隐藏输入的 `lecturecast auth login`（或 `LECTURECAST_API_KEY`）配置凭证，并设置 `LECTURECAST_DIRECTOR_URL`。API Key 不会写入项目。`director resume` 只在本地重新绑定，不扣 credit，并保证付费请求使用当前宿主的真实能力。每次确认生成一份 ProductionManifest 固定扣 10 credits；只有在确认 Brief 并明确同意该扣除后才运行 `director generate`。

真正干活的是你的 AI agent 跑本地工作流。跟 agent 说：

> 做一条关于 RAG 工作原理的 5 分钟课程视频

Agent 会读 [AGENTS.md](AGENTS.md) / [docs/LOCAL-WORKFLOW.md](docs/LOCAL-WORKFLOW.md)，然后驱动整条流水线：

```
主题
  ▼ 定范围（平台 / 深度 / 系列品牌 / 嗓音）
  ▼ 7 段草稿脚本           （你审批）
  ▼ 配音   python3 build_audio_mm.py   （Edge 免费、MiniMax 可选）
  ▼ 场景   Remotion（竖版 + 横版）
  ▼ 渲染   build_video.sh / build_video.ps1 <slug>（ffmpeg + libass）
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

安装器只会为已经存在的 Agent Skill 目录注册对应 Skill，遇到用户自定义的同名 Skill 会跳过、不覆盖。手动链接方式：

```bash
ln -s "$(pwd)/skills/claude-code" ~/.claude/skills/lecturecast
ln -s "$(pwd)/skills/codex" ~/.codex/skills/lecturecast
ln -s "$(pwd)/skills/openclaw" ~/.openclaw/skills/lecturecast
```

然后跟 agent 说：

> 做一条关于 RAG 工作原理的 5 分钟课程视频

Agent 会读 runbook 并驱动本地流水线跑到完成。

---

## 隐私

- **Community 不向 LectureCast 服务发送任何内容**。音频、成片、封面和原始媒体都留在本机。
- 如果选择 Director，只有受限摘要、稳定选项、Brief 和能力元数据进入 Director；原始媒体、TTS 文件、本地路径和成片不上传。
- 如果你选用 MiniMax 音色（BYOK），你的主题 + 脚本文本会经 HTTPS 发到**你自己的** MiniMax 账户做合成。默认 Edge 音色无需任何第三方账户。
- 无追踪、无遥测。

---

## 真实客户验收

- [Community 首次客户旅程验收报告 — 2026-07-19](docs/FIRST-TIME-CUSTOMER-VALIDATION-2026-07-19.md)

---

## 协议

Apache 2.0 — 见 [LICENSE](LICENSE)。
