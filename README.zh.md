# Lecturecast

[English](README.md) · 中文


> 🟣 Part of **[AgentMesh](https://github.com/jiyangnan/agentmesh-core)** — see the [ecosystem index](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ECOSYSTEM.md) ([中文](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ECOSYSTEM.zh.md)) for all related repos, the [roadmap](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ROADMAP.md), and [architecture](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/ARCHITECTURE.md).
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-early%20access-orange.svg)](https://lecturecast.agentmesh360.com)
[![Brand](https://img.shields.io/badge/brand-AgentMesh-6E4AFF.svg)](https://agentmesh360.com)

> 一句话生成可发的 5 分钟课程视频——**B 站 16:9** + **小红书 9:16** 两条同时出。专门设计成让你的 AI agent（Claude Code / OpenClaw / Cursor / Codex）从聊天里直接驱动。

![Lecturecast demo — 两条成片并排](assets/demo.gif)

<sub>↑ 同一份脚本，两套视觉系统。左：B 站 1920×1080。右：小红书 1080×1920。12 倍速播放，原片各 ~5:21。</sub>

**核心流程**：你给主题 → 云端起 7 段草稿脚本 → 你审批 → TTS、视觉、合成、烧字幕、封面全部在云端跑 → 4 个文件自动下载到本地。

**架构一句话**：CLI 客户端**很薄**——只负责把主题发到 `api.lecturecast.agentmesh360.com`、轮询任务状态、在你终端里展示草稿请你确认、等渲染完拉文件。**你本地不需要装 Docker / Playwright / Remotion / Python 任何重型环境。**

这是 **[AgentMesh](https://agentmesh360.com)** 旗下的产品——一系列垂直 AI agent 矩阵。**你订阅一次 AgentMesh，所有产品共享 credit 池**：Pro $9.9/月 给你 1,500 credits = 30 条 Lecturecast 视频 OR 1,500 次 Job Agent 投递 OR 自由组合。

> **⚠️ 早期访问**。M1 阶段付费档免费送 license。问维护者要 key 或者关注 `lecturecast.agentmesh360.com` 的公开领取入口。

---

## 架构 — 两个仓库 + 一个平台

| 仓库 | 可见性 | 内容 |
|---|---|---|
| **Lecturecast CLI**（本仓库） | 公开 · Apache 2.0 | 薄客户端，调云端 API。本地不渲染。 |
| **lecturecast-server** | **私有** | IP 重仓：脚本 + 视觉 prompt、HTML / Remotion 模板、Edge TTS、Playwright 录屏、libass 字幕烧录 |
| **agentmesh-core** | **私有** | 所有 AgentMesh 产品共用的身份、订阅、credit 服务 |

---

## 安装

### 一行装好（推荐）

**macOS / Linux**（Terminal）：

```bash
curl -fsSL https://raw.githubusercontent.com/jiyangnan/AgentMesh-Lecturecast/main/scripts/install.sh | bash
```

**Windows**（PowerShell）：

```powershell
irm https://raw.githubusercontent.com/jiyangnan/AgentMesh-Lecturecast/main/scripts/install.ps1 | iex
```

装完**开新终端**，运行：

```bash
lecturecast init --key lc_live_xxxxxxxx   # 粘贴维护者给你的 key
```

### 手动安装

```bash
git clone https://github.com/jiyangnan/AgentMesh-Lecturecast.git
cd AgentMesh-Lecturecast
python -m venv .venv
source .venv/bin/activate
pip install -e .
lecturecast init --key lc_live_xxxxxxxx
```

---

## 用

```bash
$ lecturecast new "RAG 工作原理"
→ 提交任务 … job_id=lct_5xz9k1
→ 起草脚本（~90s） ⠋
→ ┌─ 草稿（7 段 · ~5 min） ──────────┐
  │ § 1 (24s) 开场钩子                 │
  │ § 2 (38s) 什么是 RAG               │
  │ § 3 (58s) Embedding 详解            │
  │ § 4 (62s) 检索环节                  │
  │ § 5 (60s) 生成环节                  │
  │ § 6 (40s) 实际效果                  │
  │ § 7 (18s) 总结 + 下期               │
  └──────────────────────────────────┘
[Y] 通过  [E] 编辑  [N] 否决  > Y
→ 渲染 B 站 ……………………… 42%
→ 渲染 小红书 …………… 73%
→ 烧字幕 + 封面 ……… 91%
→ 下载 … ✓
✓ 4 个文件 → ~/lecturecast/RAG-工作原理/
  → bilibili.mp4 (13 MB · 5:21)
  → xiaohongshu.mp4 (20 MB · 5:21)
  → cover-bilibili.png
  → cover-xiaohongshu.png
```

其他命令：

| 命令 | 作用 |
|---|---|
| `lecturecast new "主题"` | 起新课程 |
| `lecturecast new "主题" --depth hands_on --platforms xiaohongshu` | 定制深度 / 平台 |
| `lecturecast new --script ./my-script.json` | 跳过起草，直接用你写的脚本 |
| `lecturecast list` | 历史任务 |
| `lecturecast get <job_id>` | 重新下载历史成片 |
| `lecturecast usage` | 本月 credit 余额 |
| `lecturecast status` | 云端 + token 健康检查 |

---

## 定价 — 所有 AgentMesh 产品共享

| 档 | Credits | 折算 Lecturecast | 折算 Job Agent |
|---|---|---|---|
| Free | **注册一次性赠 50** | 1 条视频 | 50 次投递 |
| Pro $9.9 / 月 | 1,500 / 月 | 30 条视频 / 月 | 1,500 次投递 |
| **Creator $19 / 月** | 3,500 / 月 | 70 条视频 / 月 | 3,500 次投递 |
| Team $39 / 月 | 8,000 / 月 | 160 条视频 / 月 | 8,000 次投递 |

**Free 是试用券不是常驻档**。注册时一次性发 50 credits——够你出 1 条 Lecturecast 视频（或体验 Job Agent 一周）做决定。继续用就升级订阅，credits 跨所有 AgentMesh 产品共享按需消耗。

**月度 credits 采用"覆盖式"重置**：每个订阅周期开始时余额重置为该档的额度，**未用完不结转**。按你真实的月度用量选档，囤积无收益。

### 硬封死规则 — 不会被意外扣款

- credits 用完：**HTTP 402** + 升级链接。**绝不自动超额计费**。账单永远可预测。
- **升级立刻补足档位差额**。Pro → Team 上来当场多 `8000 − 1500 = 6500` credits，加上你原有余额。
- **降级**：当前周期保留现有余额，下个周期重置到新档。
- **无退款**。取消订阅停止下次续费，本周期 credits 用到周期结束。

### 怎么选档

- **Pro** — 平均一天 1 条视频，或者偶尔用 Job Agent 找工作
- **Creator** — 专业内容创作者，一天 2 条视频左右，每 credit 比 Pro 便宜 18%
- **Team** — 小型工作室 / agency / 重度玩家，一天 5 条视频，每 credit 便宜 35%

**M1 阶段**：付费档免费送 license 给早期用户。

---

## 让你的 AI agent 来调

CLI 自带 agent skill，路径 `skills/claude-code/SKILL.md`。`lecturecast init` 之后建个软链接：

```bash
ln -s "$(pwd)/skills/claude-code" ~/.claude/skills/lecturecast
ln -s "$(pwd)/skills/claude-code" ~/.codex/skills/lecturecast
```

然后跟 agent 说：

> 做一条关于 RAG 工作原理的 5 分钟课程视频

Agent 会自动调 `lecturecast new` 并等结果。

---

## 隐私

- 音频文件、HTML、成片 mp4 在云端临时存 24 小时供下载，到期自动清理。
- 你的主题文本 + 草稿脚本由 DeepSeek V4 Flash（LLM 供应商）处理用于起草。支付信息只过 agentmesh-core 的 Stripe，不会到产品服务。
- 除按动作扣 credit 之外**无任何遥测**。

---

## 路线图

- [x] M0：本地自跑版（v0.1.0，已归档）
- [ ] M1：云端服务上线 + 早期免费 license（你正在的阶段）
- [ ] M2：Stripe 自助开通
- [ ] M2：完成时 webhook（飞书 / Slack / 邮件）
- [ ] M3：批量系列模式（一份脚本切 3-4 条 60s）
- [ ] M3：上传自定义主题（你的品牌色 / 字体）
- [ ] M3：B 站 / 小红书直发 API（用你自己的 auth）

## 协议

Apache 2.0 — 见 [LICENSE](LICENSE)。
