---
name: lecturecast
description: Create or resume LectureCast videos with optional cloud Director decisions and fully local media production. Use for course videos, tutorials, explainers, product recordings, or when the user asks to continue a LectureCast project.
---

# LectureCast for OpenClaw

Choose the route explicitly:

- **Community:** fully local, no account or LectureCast API key. Follow [AGENTS.md](../../AGENTS.md) and [LOCAL-WORKFLOW.md](../../docs/LOCAL-WORKFLOW.md).
- **Director:** optional paid cloud creative decisions and a signed declarative Manifest; all media, voice, editing and rendering remain local. Follow the complete [shared Director workflow](../shared/director-workflow.md).

For each Director question, use the current OpenClaw channel's native choice/form capability when exposed. Keep the server label/description and exact stable `option_id`. Submit one answer and refresh server state before asking the next. When a channel has no reliable choice control, use the shared numbered-text fallback; do not invent channel-specific IDs.

Never treat chat memory as project state. Resume from the supplied project path. Never create a second generation ID after a timeout. Never expose `LECTURECAST_API_KEY` in messages, tool arguments or memory.
