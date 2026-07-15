# LectureCast Director shared workflow

Director is optional. Community remains fully local and needs no account or LectureCast API key. Director sends only a bounded source summary, stable option IDs, the Creative Brief, ClientCapabilities and the resulting declarative Manifest; raw media, voice, local paths and rendered files stay on the user's machine.

## State rule

The local project is the durable state source. Never reconstruct IDs from chat history.

```bash
lecturecast project resume <project-path> --json
lecturecast director next <project-path> --json
```

Read `.lecturecast/project.json` and `.lecturecast/director-state.json` through the CLI. Never write them by hand. Never put an API key in an argument, project file, prompt, log or stdout.

## Start

1. Create or resume a local project.
2. Write a bounded UTF-8 source-summary JSON containing exactly `source_type`, `title`, `summary`, and `language`. Do not include media, transcripts, local paths or credentials.
3. Run:

```bash
lecturecast director start <project-path> \
  --source <source-summary.json> \
  --adapter <codex|claude-code|openclaw|text> \
  --json
```

`LECTURECAST_DIRECTOR_URL` supplies the server. A one-time `--server` is also accepted and then persisted without credentials. If this agent task started before installation, open a new task with the project path when the host supports it; otherwise give the user one short copyable resume command.

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

## Brief and paid generation

When status becomes `ready_to_confirm`:

```bash
lecturecast director brief show <project-path> --json
```

Show the full Brief and ask for explicit approval. Confirmation itself does not deduct credit:

```bash
lecturecast director brief confirm <project-path> --json
```

Before `generate`, clearly tell the user that the next command requests one paid ProductionManifest and deducts the published fixed credit amount. Run it only after that explicit approval. Never work around a declined or insufficient-credit response.

```bash
lecturecast director generate <project-path> --json
lecturecast director status <project-path> --json
```

The CLI reserves and persists one stable generation ID before the network call. On timeout or 503, rerun the same command without inventing a new ID. Internal model retries do not need a new ID. A ready Manifest is verified and saved read-only to the local project. An already delivered valid Manifest is not freely regenerated; local changes belong in `local-overrides.json`.

## Local production and deletion

After `status` returns `ready`, run the local preflight and local render workflow. Voice, subtitles, Remotion, ffmpeg, covers and all media remain local. The signed Manifest is read-only; timeline and style edits go into `local-overrides.json`.

To remove retained cloud content while keeping local work:

```bash
lecturecast director delete <project-path> --json
```

## Output discipline

- With `--json`, parse stdout as exactly one JSON document.
- Diagnostics and ErrorEnvelope JSON are on stderr.
- Never paste Rich tables, progress text or secrets into machine-readable output.
- Do not upload raw media or substitute a cloud render path.
