---
name: lecturecast
description: Create or resume paid LectureCast videos with AgentMesh360 commercial access, cloud Director decisions, and local media production. Use for course videos, tutorials, explainers, product recordings, or when the user asks to continue a LectureCast project.
---

# LectureCast for OpenClaw

This Skill implements host workflow contract `1.0.0`. Start every new or resumed
task with exactly `lecturecast onboard --adapter openclaw --host-contract 1.0.0
--json`. Do not create, resume, or render a project until `workflow.ready` is
true. If the payload sets
`requires_user_action`, show its exact `user_prompt` and wait for the human. A
missing key routes to `lecturecast auth login`; missing paid access or credits
routes to AgentMesh360 pricing. Never offer or take an account-free fallback.

After every successful workflow command, execute only its returned
`workflow.next_action`. If a recovery/read-only response has no workflow field,
run `lecturecast agent status <project-path> --adapter openclaw --host-contract
1.0.0 --json`. Never improvise an alternate sequence or call a template script
directly. If this OpenClaw task predates installation or an upgrade, stop and
start a new task; an old task may not self-attest the new contract.

After onboarding succeeds, follow the complete [shared Director workflow](../shared/director-workflow.md).
All original media, voice, editing and rendering remain local.

For each Director question, use the current OpenClaw channel's native choice/form capability when exposed. Keep the server label/description and exact stable `option_id`. Submit one answer and refresh server state before asking the next. When a channel has no reliable choice control, use the shared numbered-text fallback; do not invent channel-specific IDs.

When continuing an existing project, first run `lecturecast project resume
<project-path> --adapter openclaw --host-contract 1.0.0 --json`, then execute its
returned next action. A cross-host project returns an exact `director resume`
command. This binds the current Skill digest, verifies commercial access, sends
no Director request and deducts no credit.

Never treat chat memory as project state. Resume from the supplied project path. Never create a second generation ID after a timeout. Never expose `LECTURECAST_API_KEY` in messages, tool arguments or memory.
