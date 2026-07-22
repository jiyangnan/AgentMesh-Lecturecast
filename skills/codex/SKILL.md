---
name: lecturecast
description: Create or resume paid LectureCast videos with AgentMesh360 commercial access, cloud Director decisions, and local media production. Use for course videos, tutorials, explainers, product recordings, or when the user asks to continue a LectureCast project.
---

# LectureCast for Codex

This Skill implements host workflow contract `1.0.0`. Start every new or resumed
task with exactly `lecturecast onboard --adapter codex --host-contract 1.0.0
--json`. Do not create, resume, or render a project until `workflow.ready` is
true. If the payload sets
`requires_user_action`, show its exact `user_prompt` and wait for the human. A
missing key routes to `lecturecast auth login`; missing paid access or credits
routes to AgentMesh360 pricing. Never offer or take an account-free fallback.

After every successful workflow command, execute only the returned
`workflow.next_action`. If a recovery/read-only response has no workflow field,
run `lecturecast agent status <project-path> --adapter codex --host-contract
1.0.0 --json` to recover the one next action. Replace documented placeholders
only with values obtained from the human or local project. Never invent an
alternative sequence or call a template script directly. Decision-card answers,
Brief approval, the 10-credit generation, full-script approval and local render
still require their explicit human checkpoints.

If LectureCast was installed or upgraded after this Codex task started, this task
cannot attest the new Skill. Stop and create a new Codex task; do not copy the
contract version into an old task and continue.

After onboarding succeeds, follow the complete [shared Director workflow](../shared/director-workflow.md).
All original media, voice, editing and rendering remain local.

When the signed Manifest becomes ready, use `lecturecast manifest review` and
surface the complete narration to the user. Do not run TTS or render until the
user explicitly approves and `lecturecast manifest approve
--confirm-reviewed-script` succeeds. Never replace a rejected signed script with
an improvised local script.

For each Director question, use Codex's native interactive choice control when it is available. Preserve the server's 2–3 options, label/description and stable `option_id`. If the control accepts at most three questions, split larger card sets and call `lecturecast director next --json` after every submitted answer. If native choices are unavailable, use the shared numbered-text fallback.

When continuing an existing project, first run `lecturecast project resume
<project-path> --adapter codex --host-contract 1.0.0 --json`, then execute its
returned next action. A cross-host project will return an exact `director resume`
command. These operations bind a digest of the current installer-owned Skill,
verify commercial access, send no Director request and deduct no credit.

Never treat this task's conversation as project state. Resume from the supplied project path. Never create a second generation ID after a timeout. Never expose `LECTURECAST_API_KEY`.
