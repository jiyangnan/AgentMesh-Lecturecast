# Lecturecast

[English](README.md) · 中文


> 🟣 Part of **[AgentMesh](https://github.com/jiyangnan/agentmesh-core)** — see the [ecosystem index](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ECOSYSTEM.md) ([中文](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ECOSYSTEM.zh.md)) for all related repos, the [roadmap](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ROADMAP.md), and [architecture](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ARCHITECTURE.md).
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-active-brightgreen.svg)](#)
[![Brand](https://img.shields.io/badge/brand-AgentMesh-6E4AFF.svg)](https://agentmesh360.com)
[![Website](https://img.shields.io/badge/website-lecturecast.agentmesh360.com-CC785C.svg)](https://lecturecast.agentmesh360.com)

> AgentMesh360 的商业课程视频产品，专门给 AI agent 使用。云端 Director 生成签名制作方案；原始媒体、配音、编辑、渲染和导出仍留在**你自己的机器**。一个主题，同时产出 B 站 16:9 与小红书 9:16。

官网：**[lecturecast.agentmesh360.com](https://lecturecast.agentmesh360.com)** · AgentMesh360 主站：**[agentmesh360.com](https://agentmesh360.com)**

![Lecturecast demo — 两条成片并排](assets/demo.gif)

<sub>↑ 同一份脚本，两套视觉系统。左：B 站 1920×1080。右：小红书 1080×1920。12 倍速播放，原片各 ~5:21。</sub>

Lecturecast 要求有效的 AgentMesh360 monthly pass、通用 API Key，以及每次明确批准
Manifest generation 前需要有有效的付费权益（monthly pass）。公开客户端会先验证商业权限，用户
Agent 通过后才可以开始制作。`monthly_pass_required` 指 AgentMesh360 账户权益，
不是另购一份 LectureCast pass。

云端 Director 返回签名方案后，以下制作能力在本机运行：

- **Remotion**（Node）渲染两个比例的动画场景。
- **edge-tts**（Python）配音——默认免费、零配置。
- **ffmpeg** 烧字幕、拼接音视频。

**核心流程**：商业账户绑定 → Director 选择 → Brief 审批 → 明确批准扣除
per-milestone 扣费 → 签名 ProductionManifest → 完整签名脚本审批 → 本机配音、场景与渲染
→ 成片与封面。

Director 只接收受限素材摘要、稳定选项 ID、Brief 与客户端能力。它使用
AgentMesh360 账户的共享 credits，无需另购 LectureCast 独立订阅；原始媒体、
配音、字幕、编辑、Remotion、ffmpeg 与所有成片仍留在本机。

**用 AI agent 驱动？** 先看 **[AGENTS.md](AGENTS.md)** 和
**[Director 工作流](skills/shared/director-workflow.md)**。商业 onboarding 成功后
才进入本地制作指南。一次完整真实客户路径沉淀出的宿主、凭证、素材、浏览器、时长
与交付规则见
[商业工作流经验与防回归清单](docs/COMMERCIAL-WORKFLOW-LESSONS.zh.md)。

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

安装包默认包含安全凭证存储与签名 Manifest 验证。本地渲染还需要
**Node 20+**、**Python 3.11+**、**带 libass 的 ffmpeg**。安装后必须新建宿主
Agent 任务；新版 Skill 会运行带 `--adapter` 与 `--host-contract 1.0.0` 的
onboard 命令，同时验证 Skill、商业账户和渲染器。

宿主工作流合同 `1.0.0` 只用于证明当前任务加载了安装器提供的 Skill。全新项目默认
使用 Director 协议 `1.1`，`onboard` 会在 `contracts` 中分别报告两者；已有
Director Session 继续使用创建时锁定的协议，不会被静默迁移。

---

## 用

Lecturecast 是 **agent 驱动**的。第一步运行 `onboard` 并读取其下一安全动作；
只有 `requires_user_action` 要求配置 key 时才运行 `auth login`：

```bash
lecturecast onboard --adapter codex --host-contract 1.0.0 --json
# 如果提示需要 key：
lecturecast auth login
lecturecast onboard --adapter codex --host-contract 1.0.0 --json
```

当 `workflow.ready` 为 true 后，只执行返回的 `workflow.next_action`。下列命令是
工作流可能返回的示例，不是让用户手工顺序执行：

```bash
lecturecast project init ./my-video --name "我的视频" --adapter codex --host-contract 1.0.0 --json
lecturecast director start ./my-video --source source-summary.json --adapter codex --json
lecturecast director resume ./my-video --adapter openclaw --host-contract 1.0.0 --json  # 切换宿主后
lecturecast director next ./my-video --json
```

`renderer.next_actions` 只在 onboarding 被本地依赖阻塞时修复 renderer，不推进项目
状态。完成其返回的修复动作后重新运行 `onboard`；只有 `workflow.ready=true` 才回到
唯一 `workflow.next_action` 链。`--host-contract 1.0.0` 证明宿主 Skill，与
Director 协议 `1.1`、ProductionManifest schema `1.0` 都是不同概念。

API Key 不会写入项目。生产 Director URL 已内置，`LECTURECAST_DIRECTOR_URL`
只用于受控的测试环境。`director resume` 仅本地重新绑定，不扣 credit。每次确认
按里程碑计费（charge-on-success-before-release，每里程碑成功后扣费）；只有在确认 Brief 并明确同意
该扣除后才运行 `director generate`。

真正干活的是你的 AI agent 跑本地工作流。跟 agent 说：

> 做一条关于 RAG 工作原理的 5 分钟课程视频

Agent 会读 [AGENTS.md](AGENTS.md) / [docs/LOCAL-WORKFLOW.md](docs/LOCAL-WORKFLOW.md)，然后驱动整条流水线：

```
主题
  ▼ 商业 onboarding（有效 monthly pass）
  ▼ Director v1.1 选择（包含数字人和背景音乐）
  ▼ 签名 ProductionManifest + 适用的 PresenterPlan / OrchestrationPlan
  ▼ 展示完整签名脚本         （你审批）
  ▼ 分节本地配音 + 实测执行时间线
  ▼ 选中时在本机完成数字人合成 / 背景音乐混音
  ▼ 场景与字幕共同使用同一份实测时间线
  ▼ 渲染   build_manifest_video.sh / .ps1（Remotion + ffmpeg + libass）
  ▼ 旁白覆盖验收 + 2 个 mp4 + 2 张封面
```

### 配音 — 默认免费，MiniMax 可选（BYOK 自带 key）

配音默认走 **Edge TTS**（免费、零配置）。想升级到更暖更自然的 **MiniMax** 音色，
就自带一个 MiniMax key——它是 [minimaxi.com](https://www.minimaxi.com) 的第三方账户
（你自己注册，不是 Lecturecast 的密钥）。设到环境变量里，本地的 `build_audio_mm.py`
会自动启用：

```bash
export MINIMAX_API_KEY=<你自己的-minimax-key>   # LectureCast 不主动持久化
```

LectureCast 不会持久化 MiniMax key；但 shell 可能把字面量 `export` 命令写入历史，
因此优先使用宿主的安全环境注入。MiniMax 不可用时，builder 会先报告原因再使用
Edge；不能假定所有 fallback 都是同一个原因。
**用 AI agent 来驱动？请读 [AGENTS.md](AGENTS.md)**——涵盖安装、完整本地工作流、BYOK 与排障。

---

## 让你的 AI agent 来调

安装器会为检测到的 Agent 宿主注册当前商业 Skill。普通目录形式的旧 Skill 会
先备份再升级；只有指向其他安装源的 symlink/junction 才会阻塞。手动链接方式：

```bash
ln -s "$(pwd)/skills/claude-code" ~/.claude/skills/lecturecast
ln -s "$(pwd)/skills/codex" ~/.codex/skills/lecturecast
ln -s "$(pwd)/skills/openclaw" ~/.openclaw/skills/lecturecast
```

然后跟 agent 说：

> 做一条关于 RAG 工作原理的 5 分钟课程视频

安装器会把普通目录形式的旧 Skill 先做时间戳备份，再换成当前安装拥有的
adapter；每次安装或升级后都必须新建宿主 Agent 任务。新任务会证明已加载当前
Skill，随后只执行 CLI 返回的唯一 `workflow.next_action`。缺失或过期的 Skill
摘要会在项目写入和本地渲染前硬阻断。

修改安装器、宿主 adapter、恢复或渲染行为之前，先读
[商业工作流经验与防回归清单](docs/COMMERCIAL-WORKFLOW-LESSONS.zh.md)。

---

## 隐私

- 只有受限摘要、稳定选项、Brief 和能力元数据进入 Director；原始媒体、TTS 文件、本地路径和成片不上传。
- 默认 Edge TTS 由本地客户端调用、无需账户，但旁白文本会发送到 Microsoft Edge
  语音服务做合成；它不会收到原始媒体或成片。
- 如果你选用 MiniMax 音色（BYOK），签名旁白脚本文本会经 HTTPS 发到**你自己的**
  MiniMax 账户做合成。
- 无追踪、无遥测。
- 受邀 limited cohort 用户可以
  [显式生成本地 outcome receipt](docs/LOCAL-OUTCOME-EVIDENCE.md)，并手工导出受限的
  匿名报告；CLI 不会自动上传。

---

## 协议

Apache 2.0 — 见 [LICENSE](LICENSE)。
