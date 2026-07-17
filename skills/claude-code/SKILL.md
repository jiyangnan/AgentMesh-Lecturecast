---
name: lecturecast
description: Create or resume LectureCast videos with optional cloud Director decisions and fully local media production. Use for course videos, tutorials, explainers, product recordings, or when the user asks to continue a LectureCast project.
---

# LectureCast for Claude Code

Choose the route explicitly:

- **Community:** fully local, no account or LectureCast API key. Follow [AGENTS.md](../../AGENTS.md) and [LOCAL-WORKFLOW.md](../../docs/LOCAL-WORKFLOW.md).
- **Director:** optional paid cloud creative decisions and a signed declarative Manifest; all media, voice, editing and rendering remain local. Follow the complete [shared Director workflow](../shared/director-workflow.md).

For each Director question, use Claude Code's `AskUserQuestion` choice UI when available. Preserve the server's label/description and map the response to the exact stable `option_id`; never infer it from display text. Submit one answer and refresh server state before asking the next question. If the choice UI is unavailable, use the shared numbered-text fallback.

When continuing an existing Director project, first run `lecturecast director resume <project-path> --adapter claude-code --json`. This is an offline, zero-credit rebind; run it before any further Director operation.

Never treat conversation history as project state. Resume from the supplied project path. Never create a second generation ID after a timeout. Never expose `LECTURECAST_API_KEY`.
