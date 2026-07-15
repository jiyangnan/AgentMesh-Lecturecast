---
name: lecturecast
description: Create or resume LectureCast videos with optional cloud Director decisions and fully local media production. Use for course videos, tutorials, explainers, product recordings, or when the user asks to continue a LectureCast project.
---

# LectureCast for Codex

Choose the route explicitly:

- **Community:** fully local, no account or LectureCast API key. Follow [AGENTS.md](../../AGENTS.md) and [LOCAL-WORKFLOW.md](../../docs/LOCAL-WORKFLOW.md).
- **Director:** optional paid cloud creative decisions and a signed declarative Manifest; all media, voice, editing and rendering remain local. Follow the complete [shared Director workflow](../shared/director-workflow.md).

For each Director question, use Codex's native interactive choice control when it is available. Preserve the server's 2–3 options, label/description and stable `option_id`. If the control accepts at most three questions, split larger card sets and call `lecturecast director next --json` after every submitted answer. If native choices are unavailable, use the shared numbered-text fallback.

Never treat this task's conversation as project state. Resume from the supplied project path. Never create a second generation ID after a timeout. Never expose `LECTURECAST_API_KEY`.
