# LectureCast Director shared workflow

Director is the required LectureCast creative workflow. It sends only a bounded
source summary, stable option IDs, the Creative Brief, ClientCapabilities and the
resulting declarative Manifest; raw media, voice, local paths and rendered files
stay on the user's machine.

LectureCast requires a current AgentMesh360 monthly pass.
Use an AgentMesh360 universal API key; an older Job Agent-only key does not cover
LectureCast. There is no separate LectureCast pass.

## Commercial gate

Run `lecturecast onboard --json` before every new or resumed task. Continue only
when `workflow.ready` is true. When `requires_user_action` is true, show the exact
`user_prompt`, follow `next_suggested`, and wait for the human before continuing.
Never offer an account-free route or silently continue when commercial access is
missing.

## State rule

The local project is the durable state source. Never reconstruct IDs from chat history.

```bash
lecturecast project resume <project-path> --json
lecturecast director resume <project-path> \
  --adapter <codex|claude-code|openclaw|text> \
  --json
lecturecast director next <project-path> --json
```

Read `.lecturecast/project.json` and `.lecturecast/director-state.json` through the CLI. Never write them by hand. Never put an API key in an argument, project file, prompt, log or stdout.

On every existing Director project, run `director resume` with the current host before `next`, `answer`, `brief`, `generate`, or `status`. This command verifies commercial access with AgentMesh360 Core, makes no Director request, and deducts no credit. If the host changed, the CLI refreshes its saved ClientCapabilities before the paid generation request, so the server receives the current host identity instead of stale handoff state.

## Start

1. Confirm `lecturecast onboard --json` reports `workflow.ready: true`.
2. Create or resume a local project.
3. Write a bounded UTF-8 source-summary JSON containing exactly `source_type`, `title`, `summary`, and `language`. The user-confirmed summary must contain at least 20 characters of concrete facts or explicitly state the intended general framework. Do not include media, transcripts, local paths or credentials.
4. Run:

```bash
lecturecast director start <project-path> \
  --source <source-summary.json> \
  --adapter <codex|claude-code|openclaw|text> \
  --json
```

The production Director URL is built in. `LECTURECAST_DIRECTOR_URL` or a one-time
`--server` is only for controlled staging/development and is persisted without
credentials. If this agent task started before installation, run `lecturecast
director handoff <project-path> --json`. The payload keeps the generic
`resume_argv` and also returns `director_resume_argv_by_adapter`; the new task
must run the exact entry for its current host before continuing Director work.
When the host exposes a task-creation tool, use the returned `prompt` to create
the new task; otherwise give the user that exact prompt as the one short copyable
fallback. Do not claim a new task was created unless the host confirms it.

## Decision cards

Call `director next --json`. For every question in `decision_card_set`:

1. Present the server's label and description with the host's native choice UI.
2. Keep the associated `question_id` and `option_id` internally.
3. Submit only the stable IDs, never infer an ID from translated/display text.
4. Submit one answer, then call `next` again before presenting another question.

```bash
lecturecast director answer <project-path> \
  --question-id <stable-question-id> \
  --option-id <stable-option-id> \
  --catalog-version <catalog-version> \
  --json
```

For `other`, place the user's bounded text in a temporary UTF-8 file and use `--custom-text-file`; do not expose it in shell history. If the host has no native choice control, show numbered choices and map the selected number back to the exact option ID.

For `source_readiness`, preserve the user's exact stable option ID. `facts_confirmed` means the summary is the factual boundary; `framework_only` forbids product-specific or technical details that are absent from the summary. If the user selects `need_more_source`, do not confirm or generate: tell them to supplement the local summary and start a new Director Session. This path never deducts credit.

## Brief and paid generation

When status becomes `ready_to_confirm`:

```bash
lecturecast director brief show <project-path> --json
```

Show the full Brief and ask for explicit approval. Confirmation itself does not deduct credit:

```bash
lecturecast director brief confirm <project-path> --json
```

Before `generate`, clearly tell the user that the next command requests one paid ProductionManifest and deducts 10 credits. Run it only after that explicit approval. Never work around a declined or insufficient-credit response.

```bash
lecturecast director generate <project-path> --json
lecturecast director status <project-path> --json
```

The CLI reserves and persists one stable generation ID before the network call. On timeout or 503, rerun the same command without inventing a new ID. Internal model retries do not need a new ID. A ready Manifest is verified and saved read-only to the local project. An already delivered valid Manifest is not freely regenerated; local changes belong in `local-overrides.json`.

## Local production and deletion

After `status` returns `ready`, do not start TTS or rendering yet. First run:

```bash
lecturecast manifest review <project-path> --json
```

Show the complete returned script, planned section durations and timing result to
the human. Wait for explicit `通过 / approved`. Only then record approval of the
exact Manifest and script digests:

```bash
lecturecast manifest approve <project-path> \
  --confirm-reviewed-script --json
```

Approval fails closed when the narration is too sparse or dense for the signed
timeline. The bundled render workflow also checks that this approval receipt is
current before it creates audio.

Then run the local preflight and local render workflow. Voice, subtitles,
Remotion, ffmpeg, covers and all media remain local. The signed Manifest is
read-only. Per-section TTS produces a digest-bound local audio timing plan; that
measured execution plan drives scene timing, subtitles and final duration.
Intentional style edits remain in `local-overrides.json`.

To remove retained cloud content while keeping local work:

```bash
lecturecast director delete <project-path> --json
```

## Optional local outcome evidence

Do not record, export, or transmit outcome evidence automatically. Only after
the user explicitly opts into limited-cohort evidence, ask bounded questions
with the host's native choice control:

1. Render result: `completed`, `partial`, or `failed/not_attempted`; split the
   last choice into a second two-option question when selected.
2. Adoption: `published`, `exported`, or `not adopted`; split the last choice
   into `discarded` or `undecided` when selected.
3. For `partial` or `failed`, ask for one bounded `failure_reason` from the CLI
   help. Never collect free text.

Record the answer locally with `lecturecast outcome record`. The private
receipt is marked `shareable: false`. Only if the user separately agrees to
share an anonymous report may you run:

```bash
lecturecast outcome export <project-path> \
  --report-out <new-local-file> \
  --consent share-anonymous-outcome \
  --json
```

Show the exported JSON to the user before they decide how to share it. This
command makes no network request. Never upload the file, infer consent, include
source/path/media/free text, or claim it was sent. Full contract:
`docs/LOCAL-OUTCOME-EVIDENCE.md`.

## Output discipline

- With `--json`, parse stdout as exactly one JSON document.
- Diagnostics and ErrorEnvelope JSON are on stderr.
- Never paste Rich tables, progress text or secrets into machine-readable output.
- Do not upload raw media or substitute a cloud render path.
