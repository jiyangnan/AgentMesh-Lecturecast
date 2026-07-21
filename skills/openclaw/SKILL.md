---
name: lecturecast
description: Create or resume paid LectureCast videos with AgentMesh360 commercial access, cloud Director decisions, and local media production. Use for course videos, tutorials, explainers, product recordings, or when the user asks to continue a LectureCast project.
---

# LectureCast for OpenClaw

Start every new or resumed task with `lecturecast onboard --json`. Do not create,
resume, or render a project until `workflow.ready` is true. If the payload sets
`requires_user_action`, show its exact `user_prompt` and wait for the human. A
missing key routes to `lecturecast auth login`; missing paid access or credits
routes to AgentMesh360 pricing. Never offer or take an account-free fallback.

After onboarding succeeds, follow the complete [shared Director workflow](../shared/director-workflow.md).
All original media, voice, editing and rendering remain local.

For each Director question, use the current OpenClaw channel's native choice/form capability when exposed. Keep the server label/description and exact stable `option_id`. Submit one answer and refresh server state before asking the next. When a channel has no reliable choice control, use the shared numbered-text fallback; do not invent channel-specific IDs.

When continuing an existing Director project, first run `lecturecast director resume <project-path> --adapter openclaw --json`. This verifies commercial access with AgentMesh360 Core, sends no Director request, and deducts no credit; run it before any further Director operation.

Never treat chat memory as project state. Resume from the supplied project path. Never create a second generation ID after a timeout. Never expose `LECTURECAST_API_KEY` in messages, tool arguments or memory.
