# LectureCast Community 首次客户旅程验收报告

> 日期：2026-07-19（Asia/Taipei）
> 测试对象：`lecturecast 0.3.1` / Git commit `b9b95a77035e279ce6e9a3b9d4cffe9346a86706`
> 路线：Community（完全本地、无账户、无 LectureCast API Key）
> 结论：**价值主张清楚，CLI 与本地模板能够工作；但当前 macOS 首次安装链路仍存在会阻断首条成片的 P0 问题，尚不适合把“一行安装后几分钟出片”作为普遍承诺。**

## 1. 为什么做这次测试

本次测试不从开发者已经配置好的工作区开始，而是模拟一名第一次访问
<https://lecturecast.agentmesh360.com/> 的 Codex 用户：

1. 从线上官网理解产品与两条路线；
2. 复制官网公开的一行安装命令；
3. 在隔离的临时 `HOME` 中安装，安装与运行时不挂载现有 LectureCast checkout、虚拟环境或 skill；
4. 按安装后的 `AGENTS.md`、`docs/LOCAL-WORKFLOW.md` 与 Codex skill 操作；
5. 创建真实课程项目，安装 Remotion，运行 `doctor`、能力采集、合规 grep 与最小真实渲染；
6. 在脚本审批关卡停止，不绕过产品自己要求的人类确认。

本报告的目标不是评审视觉风格，而是回答一个更基础的问题：

> 一个具备一般开发环境的真实新用户，能否只依赖官网与仓库公开说明，从零走到第一条可发布视频？

## 2. 来源与版本证明

测试使用的产品来源如下：

| 来源 | 实际使用内容 |
|---|---|
| 线上官网 | `https://lecturecast.agentmesh360.com/` |
| 线上安装脚本 | `https://raw.githubusercontent.com/jiyangnan/AgentMesh-Lecturecast/main/scripts/install.sh` |
| 安装目标 | 隔离 HOME 下的全新 `~/.lecturecast/app` |
| Git 远端 | `https://github.com/jiyangnan/AgentMesh-Lecturecast.git` |
| 测试提交 | `b9b95a77035e279ce6e9a3b9d4cffe9346a86706` |
| CLI | `lecturecast 0.3.1` |
| 当前公开运行手册 | `AGENTS.md`、`docs/LOCAL-WORKFLOW.md`、`skills/codex/SKILL.md` |

本机另有一个历史 checkout，但它版本更旧且存在未提交改动。**它没有参与本次安装、运行、问题复现或结论形成。**测试 agent 在任务开始时因宿主 skill 触发规则读取过一份历史 LectureCast skill；远端安装完成后，所有实际命令、运行步骤与问题判断均切换到新安装提交中的 `AGENTS.md`、`LOCAL-WORKFLOW.md` 和 Codex skill。下文没有任何问题仅以历史 skill 为依据。

## 3. 测试角色与环境

### 3.1 客户画像

- 使用 Codex 驱动本地课程视频制作；
- 第一次安装 LectureCast；
- 不使用 Director，不提供 AgentMesh360 API Key；
- 不提供 MiniMax Key，预期走免费 Edge TTS；
- 目标为 B 站横版 + 小红书竖版；
- 测试主题为非敏感实用教程《番茄工作法：如何开始困难任务》。

### 3.2 机器环境

| 项目 | 值 |
|---|---|
| 操作系统 | macOS 26.5.1（Build 25F80） |
| 主机架构 | arm64 |
| 默认 Python | 3.14.2，进程架构 x86_64 |
| 备用 Python | 3.12.12，进程架构 x86_64 |
| Node | 22.23.1，进程架构 x64 |
| npm | 10.9.8 |
| Git | Apple Git 2.39.2 |
| 初始 ffmpeg | Homebrew ffmpeg 8.0.1，未启用 libass |
| 补救 ffmpeg | Homebrew ffmpeg-full 8.1.2_1，已启用 libass |

这是一个 **arm64 主机 + x86_64 Homebrew/Python/Node** 的混合架构环境。它不是所有
Mac 用户的默认状态，因此涉及 Python wheel 与 Rust 的结论必须限定在该环境；但这类
Rosetta/历史迁移机器是真实客户环境，安装器至少应该识别并给出可执行的错误信息。

## 4. 执行范围与边界

### 已完成

- 真实浏览官网和 FAQ；
- 使用官网一行命令进行隔离安装；
- 验证 CLI `version`、`workflow`、`doctor`；
- 创建持久化本地项目；
- 运行项目能力采集；
- 复制官方 Remotion 模板并执行干净 `npm install`；
- 下载 Remotion Headless Chrome；
- 真正渲染一张 1080×1920 的 `VideoVertical` QA still；
- 检查系统与 `ffmpeg-full` 的 libass 能力；
- 对脚本、模板与字幕目录运行公开工作流要求的禁词 grep；
- 生成 8 段、约 1,288 字的真实课程脚本，并停在用户审批关卡。

### 未完成

- 没有在用户批准脚本前生成 TTS；
- 没有渲染两条完整 5 分钟视频；
- 没有使用 Director、credits、通用 API Key 或任何云端创作接口；
- 没有测试 Windows、Linux、原生 arm64 Python 或纯 Intel Mac。

因此，本报告是一次完整的**安装与首次渲染前链路验收**，不是最终双成片质量验收。

## 5. 客户旅程结果

| 阶段 | 操作 | 结果 | 客户感受 |
|---|---|---|---|
| 官网理解 | 阅读首屏、路线、能力、FAQ | PASS | Community/Director 边界、隐私与双平台价值非常清楚 |
| 原始安装 | 官网 `curl ... install.sh | bash` | FAIL | pip 构建 `cryptography` 时需要未说明的 Rust/Cargo |
| 补救安装 | 切换 Python 3.12，并借用已有 Rust 工具链完成构建 | PASS | CLI 可安装，但已经超出公开“一行安装”说明 |
| CLI 入口 | `lecturecast version` / `workflow` | PASS | 命令结构和 agent handoff 文案清楚 |
| 环境体检 | `lecturecast doctor` | PARTIAL | 能发现 libass，但错误地持续报告 Remotion 缺失 |
| 项目初始化 | `lecturecast project init` | PASS | 本地项目状态文件成功生成 |
| 能力采集 | `lecturecast project capabilities` | FAIL | 因 doctor 的 Remotion 误判返回 `manifest_incompatible` |
| Remotion 安装 | 项目模板内 `npm install` | PASS | 186 packages，冷启动约 2 分钟 |
| 浏览器准备 | 自动下载 Headless Chrome | PASS | 额外约 98 MB，官网未量化说明 |
| 首次 QA still | 官方命令渲染 `VideoVertical` | FAIL → PASS | 第一次浏览器连接 25 秒超时；原命令立即重试成功 |
| 字幕能力 | 普通 Homebrew ffmpeg | FAIL | 没有 `ass` / `subtitles` filter |
| 字幕补救 | 安装并优先使用 `ffmpeg-full` | PASS | 依赖和系统影响远大于文档描述 |
| 小红书合规 | 对整个项目执行禁词 grep | FAIL | 官方 End 模板自身含有“爬虫” |
| 内容审批 | 8 段真实脚本 | PASS / WAIT | 产品正确停在人工审批关卡 |

## 6. 问题清单

严重级别定义：

- **P0**：会阻止新客户产出第一条成片，且公开主路径没有可靠自救步骤；
- **P1**：结果错误或公开承诺与实际不一致，有明显 workaround；
- **P2**：不会直接阻断，但显著增加等待、困惑或支持成本。

### LC-FTUX-001 · P0 · 混合架构 Mac 安装会触发未声明的 Rust 构建依赖

#### 现象

官网安装器接受 Python 3.14，创建 venv 后执行：

```bash
pip install --quiet -e "$INSTALL_DIR"
```

在本次 arm64 主机、x86_64 Python 环境中，pip 为 `cryptography>=43` 选择源码包，随后失败：

```text
error: rustup could not choose a version of cargo to run
Cargo, the Rust package manager, is not installed or is not on PATH.
metadata-generation-failed: cryptography
```

切换到 Python 3.12 后仍然构建了 `cryptography` wheel；只有借用机器上已经配置好的
Rust/Cargo 才完成安装。

#### 已确认事实

- `pyproject.toml` 把 `cryptography>=43` 放在所有安装都会加载的核心依赖中；
- 公开前置条件只列 Python 3.11+、Git、Node、ffmpeg，没有 Rust；
- 安装器只验证 Python 主次版本，没有验证 Python/主机架构或 wheel 可用性；
- 本次错误发生在最新公开提交，而不是历史 checkout。

#### 根因推断

混合架构环境没有匹配的预编译 wheel，pip 回退到源码构建。`cryptography` 主要服务于
签名 Manifest/Director 路线，但 Community 安装也必须承担该依赖。

#### 建议

1. 评估将签名/Director 依赖拆到可选 extra，Community 基础安装不强制编译 cryptography；
2. 安装前检测 `uname -m` 与 `platform.machine()`，对 Rosetta/混合架构给明确提示；
3. 在 pip 失败时自动重跑一次非 quiet 诊断，打印精确补救命令；
4. venv 存在但不完整时，不应在下次安装中直接假设它可用；
5. CI 增加 macOS arm64 原生、Rosetta x86_64、Python 3.11/3.12/3.14 安装矩阵。

#### 验收标准

- 支持矩阵中的 Community 一行安装无需预装 Rust；或
- 不支持的架构在修改系统前就 fail-fast，并给出一条确定可执行的替代命令；
- 中断后再次运行安装器能够自动恢复，不留下“目录存在但 pip 不完整”的半安装状态。

### LC-FTUX-002 · P0 · macOS ffmpeg 安装说明与当前 Homebrew 事实不符

#### 现象

`AGENTS.md` 与 `docs/LOCAL-WORKFLOW.md` 都建议：

```bash
brew install ffmpeg
```

并明确写道普通 brew build 含 libass。实测 Homebrew `ffmpeg 8.0.1` 的 build configuration
没有 `--enable-libass`，`ffmpeg -filters` 也没有 `ass` / `subtitles`。

Homebrew 当前 `brew info ffmpeg` 明确提示额外库在 `ffmpeg-full` 中；安装
`ffmpeg-full 8.1.2_1` 后才得到：

```text
--enable-libass
ass        V->V  Render ASS subtitles using libass
subtitles  V->V  Render text subtitles using libass
```

#### 额外风险

`ffmpeg-full` 是 keg-only，并带来大量依赖。本次实际安装了 47 个依赖、升级 23 个现有
依赖。依赖升级还使原先链接的旧 `ffmpeg 8.0.1` 暂时找不到旧版 `libx265` 动态库。
这属于具体 Homebrew 状态的连带影响，但说明把它当作“一行无感依赖”风险很高。
测试结束后已把普通 ffmpeg 升级到 8.1.2_1，恢复其动态链接；普通版本仍不包含 libass，
LectureCast 测试继续通过 keg-only 的 `ffmpeg-full` 验证字幕 filter。

#### 建议

1. 立即纠正文档，不再声称普通 `brew install ffmpeg` 自带 libass；
2. 如果继续使用 `ffmpeg-full`，明确说明它 keg-only，并给出仅对 LectureCast 生效的 PATH；
3. 更稳妥的方案是提供隔离、固定版本的 ffmpeg/libass runtime，避免修改用户全局 Homebrew 图；
4. 安装器应以能力为准，不以包名为准：同时检查 buildconf 和 filters；
5. 对已经安装普通 ffmpeg 的用户，给出不会破坏现有链接的迁移/回滚说明。

#### 验收标准

安装完成后以下命令必须成功，并且安装器要自行执行：

```bash
ffmpeg -buildconf 2>&1 | grep -- '--enable-libass'
ffmpeg -hide_banner -filters 2>/dev/null | grep -E '(^| )ass |(^| )subtitles '
```

### LC-FTUX-003 · P1 · 安装器显示成功，但没有检查完整本地出片能力

#### 现象

官网写明 `install.sh` 会检查本地三件套缺失项。实际 `scripts/install.sh` 只检查：

```bash
need_cmd python3
need_cmd git
```

它不检查 Node、npm、ffmpeg、libass、Remotion 或浏览器。即使无法渲染，也会打印
`Installed. Next:`。

#### 建议

- 区分“CLI 安装成功”和“本地出片就绪”；
- 安装末尾自动运行 `lecturecast doctor --json`；
- doctor 未就绪时不要只打印成功，而要列出每个缺项、检测证据与下一条命令；
- 官网文案改为与安装器真实行为一致，直到自动预检落地。

#### 验收标准

干净机器执行官网命令后，终端必须明确落在以下两种状态之一：

1. `CLI installed; renderer ready`；或
2. `CLI installed; renderer not ready`，后跟可复制的缺项修复命令。

### LC-FTUX-004 · P1 · doctor 与项目能力采集错误报告 Remotion 缺失

#### 现象

公开工作流要求把模板复制到项目后执行：

```bash
cp -R <repo>/templates/remotion/. remotion/
cd remotion && npm install --no-fund --no-audit
```

项目内 Remotion 4.0.479 已安装，`node_modules/.bin/remotion` 可执行，且真实 still 已成功
渲染。然而 `lecturecast doctor` 仍返回：

```json
{
  "ready": false,
  "missing": ["remotion"],
  "remotion_version": null
}
```

`lecturecast project capabilities` 随后以 `manifest_incompatible` 失败。

#### 代码证据

`src/lecturecast/capabilities.py` 只读取安装仓库内部：

```text
<repo_root>/templates/remotion/node_modules/remotion/package.json
```

而 CLI 的 doctor、project、manifest 与 Director 都把 package repo root 传给能力采集。
它们不会检查用户按照文档安装依赖的 episode 项目目录。

#### 建议

定义明确的 Remotion runtime 解析顺序：

1. 显式 `--project-root` / 当前 LectureCast 项目的 `remotion/node_modules`；
2. 可选共享引擎目录；
3. 安装仓库模板目录；
4. PATH 上的 `remotion` 或 `npx remotion versions`；
5. 均不存在时才报告缺失。

能力快照应记录实际解析到的 runtime 路径类型与版本，但不要把用户绝对路径上传给 Director。

#### 验收标准

- 按 `LOCAL-WORKFLOW.md` 在项目内执行 `npm install` 后，doctor 必须识别 Remotion；
- `project capabilities` 必须能够保存能力快照；
- 在没有 Remotion 的干净项目中仍然准确 fail-closed；
- 增加 CLI 集成测试，而不仅是向仓库模板手工放置 fake package.json 的单元测试。

### LC-FTUX-005 · P1 · 官方 End 模板自身违反强制小红书禁词检查

#### 现象

公开工作流要求最终在整个视频目录执行：

```bash
grep -rno "扒\|私信\|领取\|暗号\|起底\|爬虫\|爬取\|关注我" scripts remotion/src assets/*.srt
```

预期无匹配，但官方模板以下文件都包含 `AI 写爬虫`：

- `templates/remotion/src/scenes/End.tsx`
- `templates/remotion/src/scenesH/EndH.tsx`

因此一个刚复制、尚未定制的官方项目会立即违反官方自己的 mandatory gate。

#### 建议

- 将模板词替换为合规表达，例如 `AI 做数据采集` 或其他与主题无关的安全占位；
- 把同一禁词规则做成仓库 CI，扫描 `templates/`、`site/` 示例与测试 fixture；
- 不要只依赖 agent 在每期内容最后手工 grep。

#### 验收标准

上述禁词扫描在干净仓库和刚复制的模板项目中都返回 0 个匹配。

### LC-FTUX-006 · P2 · 首次 Remotion 浏览器冷启动超时，但原命令重试成功

#### 现象

首次执行：

```bash
npx remotion still VideoVertical qa/hook.png --frame=120
```

Remotion 下载约 98 MB Headless Chrome 并完成 bundle，随后 25 秒内无法连接浏览器：

```text
TimeoutError: Timed out after 25000 ms while trying to connect to the browser
```

不修改任何配置，立即重跑同一命令后约数秒成功生成 1080×1920 PNG。

#### 建议

- 在首次渲染前增加显式 browser warm-up；
- 对“刚完成浏览器下载后的首次连接超时”自动重试一次；
- 输出下载大小、缓存位置和“后续渲染会更快”的预期；
- 在不隐藏真实错误的前提下，把可恢复冷启动从用户支持问题变为自动恢复。

### LC-FTUX-007 · P2 · Agent skill 注册条件未对用户可见

#### 现象

`manage_adapters.sh` 只有在以下目录已经存在时才注册 skill：

```text
~/.codex/skills
~/.claude/skills
~/.openclaw/skills 或 ~/.openclaw/workspace/skills
```

如果用户只有 `~/.codex` 而没有 `~/.codex/skills`，安装器会静默跳过；用户仍看到整体安装
成功，但新会话不会自动加载 LectureCast skill。

#### 建议

- 保留“不擅自创建未安装 agent 的目录”原则；
- 已检测到 `~/.codex` 但缺 `skills/` 时，打印明确提示和一条注册命令；
- 安装总结列出每个 adapter 的 `registered / skipped / custom-preserved` 状态。

### LC-FTUX-008 · P2 · 首次冷启动体积与时间缺少预期管理

本次项目级 Remotion 安装结果：

| 项目 | 观测值 |
|---|---:|
| npm packages | 186 |
| `node_modules` 总占用 | 约 527 MB |
| 其中 Remotion Headless Chrome | 约 197 MB |
| CLI checkout + Python venv | 约 47 MB |
| 首次 `npm install` | 约 2 分钟 |
| 浏览器下载与首次启动 | 数分钟，第一次启动曾超时 |

这些成本不是错误，但会影响“几分钟出片”的理解。建议把首次环境准备与后续每期制作时间分开
说明，并优先复用共享引擎或全局缓存，避免每个 episode 重复 500 MB。

## 7. 做得好的部分

问题较多，但以下产品判断与实现值得保留：

1. **官网定位清楚。** Community 与 Director 的数据边界、付费边界和价值差异容易理解；
2. **隐私承诺与本次行为一致。** Community 流程没有要求账户或 LectureCast API Key；
3. **CLI handoff 友好。** `lecturecast workflow` 能把 agent 指向明确的 runbook；
4. **项目状态是持久化的。** `project init` 创建本地状态，不依赖聊天记录；
5. **审批关卡正确。** 真实脚本生成后按规则停止，没有在用户确认前进入 TTS/渲染；
6. **模板确实可渲染。** 浏览器冷启动重试后，官方纵版 composition 成功输出真实 PNG；
7. **doctor 的 libass 检测方向正确。** 它以 build capability 而非仅包名判断字幕能力；
8. **安装器保护自定义 skill。** 不覆盖现有用户目录或自定义 symlink 是正确的安全策略。

## 8. 建议修复顺序

### 立即修复：同日可完成

1. 更正文档中的 macOS ffmpeg 安装说明；
2. 删除两个 End 模板里的 `爬虫`；
3. 官网与安装器统一“会检查什么”的文案；
4. 安装完成时自动运行 doctor，并区分 CLI installed 与 renderer ready；
5. adapter 被跳过时打印可执行提示。

### 下一小版本

1. 修正 Remotion runtime 发现逻辑和 `project capabilities`；
2. 增加真实项目目录的 doctor/能力采集集成测试；
3. 改善 pip 失败恢复与混合架构诊断；
4. 对首次浏览器启动做一次受控自动重试；
5. 评估将 cryptography/Director 依赖从 Community 核心安装中拆出。

### 后续体验优化

1. 提供共享 Remotion engine/cache，避免每期重复依赖；
2. 在官网列出首次安装体积、预计耗时与后续增量成本；
3. 提供 `lecturecast setup` 或 `lecturecast doctor --fix`，但必须保持显式、可审计且不写入密钥；
4. 把“安装 → still → libass burn → 合规 grep”做成发布前 canary。

## 9. 建议的自动化首次客户验收

每次 release 或官网安装说明变更时，在干净 runner 上执行：

```text
[1] 创建隔离 HOME
[2] 执行官网原始一行安装命令
[3] lecturecast version / workflow / doctor --json
[4] lecturecast project init <fixture>
[5] 复制 Remotion 模板并 npm ci
[6] project capabilities --json
[7] 分别渲染一张 Vertical 与 Landscape still
[8] 生成 1 秒测试视频并真实烧录一条 ASS 中文字幕
[9] 对 templates + fixture 执行小红书禁词扫描
[10] 再跑一次安装器，验证幂等与失败恢复
```

建议最小矩阵：

| 平台 | Python/Node | 目的 |
|---|---|---|
| macOS arm64 原生 | Python 3.11、3.12、3.14；Node 20/22 | 主支持路径 |
| macOS arm64 + Rosetta x86_64 | 至少一个受支持组合 | 识别真实迁移机器风险 |
| Ubuntu x86_64 | Python 3.11/3.12；Node 20/22 | Linux 主路径 |

Release gate 必须保证：

- 一行安装不会得到假成功；
- doctor 与实际渲染能力一致；
- 两个比例的 still 都能生成；
- libass 真的完成字幕烧录，不只存在 filter 名；
- 官方模板不会触发官方禁词规则；
- 整条 Community canary 不需要账户、API Key 或媒体上传。

## 10. 最终判断

LectureCast 的产品方向是成立的：对 AI agent 来说，它把“写一段脚本”推进到了“交付双平台
媒体资产”，官网对本地隐私与可选 Director 的表达也足够清楚。当前主要风险不在创作概念，
而在安装契约：文档、安装器、doctor 与项目实际依赖位置还没有形成同一套可验证事实。

在修复 LC-FTUX-001 至 LC-FTUX-005 之前，建议将对外承诺表述为：

> 一行安装 LectureCast CLI；本地渲染依赖由 doctor 检查并按提示准备。

当首次客户 canary 能在支持矩阵上稳定通过后，再升级为：

> 一行安装并准备本地渲染环境，给主题后由 agent 端到端交付双平台成片。

这两句话的差异，就是当前从“产品可用”走向“新客户可自助成功”的主要工作量。
