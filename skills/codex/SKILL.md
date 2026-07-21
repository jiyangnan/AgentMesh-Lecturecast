---
name: lecturecast
description: Create or resume paid LectureCast videos with AgentMesh360 commercial access, cloud Director decisions, and local media production. Use for course videos, tutorials, explainers, product recordings, or when the user asks to continue a LectureCast project.
---

# LectureCast for Codex

Start every new or resumed task with `lecturecast onboard --json`. Do not create,
resume, or render a project until `workflow.ready` is true. If the payload sets
`requires_user_action`, show its exact `user_prompt` and wait for the human. A
missing key routes to `lecturecast auth login`; missing paid access or credits
routes to AgentMesh360 pricing. Never offer or take an account-free fallback.

After onboarding succeeds, follow the complete [shared Director workflow](../shared/director-workflow.md).
All original media, voice, editing and rendering remain local.

For each Director question, use Codex's native interactive choice control when it is available. Preserve the server's 2–3 options, label/description and stable `option_id`. If the control accepts at most three questions, split larger card sets and call `lecturecast director next --json` after every submitted answer. If native choices are unavailable, use the shared numbered-text fallback.

When continuing an existing Director project, first run `lecturecast director resume <project-path> --adapter codex --json`. This verifies commercial access with AgentMesh360 Core, sends no Director request, and deducts no credit; run it before any further Director operation.

Never treat this task's conversation as project state. Resume from the supplied project path. Never create a second generation ID after a timeout. Never expose `LECTURECAST_API_KEY`.
