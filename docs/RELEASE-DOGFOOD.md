# Three-host release dogfood gate

This is an offline evidence gate for an already released LectureCast Public
wheel. It does not create a production key, deploy a server, call Director, or
render in the cloud. The operator runs the normal Director and local production
commands in the real Codex, Claude Code, and OpenClaw hosts; the CLI records only
stable IDs, statuses, digests, signature metadata, and local output metadata.

## Release binding

Every run must begin with both artifacts from the same completed Public-first
release:

- the signed `signing-public-first-attestation.v1` JSON;
- the exact released `lecturecast-<version>-py3-none-any.whl`.

`dogfood begin` fails closed unless the attestation signature matches the
packaged current production key, the Public release time is not later than the
attestation, the production key window is active, the wheel hash and version
match the attestation, and the running LectureCast package content matches the
wheel's `lecturecast/**/*.py|json` content exactly.
The session stores only non-secret release IDs and digests, never local paths.

```bash
lecturecast dogfood begin <project-path> \
  --run-id <unique-run-id> \
  --run-kind <native_full|handoff|text_fallback> \
  --adapter <codex|claude-code|openclaw|text> \
  --public-first-attestation <attestation.json> \
  --public-wheel <released.whl> \
  --json
```

Use a fresh local project and unique run ID for every row. Do not copy or edit
`.lecturecast/dogfood-session.json`.

## Required five-run matrix

| Count | Run kind | Required host path | Stop condition |
| --- | --- | --- | --- |
| 3 | `native_full` | one each in Codex, Claude Code, OpenClaw | ready Manifest plus four verified local outputs |
| 1 | `handoff` | Codex → Claude Code → OpenClaw in fresh Agent tasks | Claude confirms Brief; OpenClaw generates and renders four outputs |
| 1 | `text_fallback` | `text` adapter with numbered choices | stop after text answers, before confirmation/generation |

For native UI answers in an active dogfood run, add:

```bash
lecturecast director answer <project-path> ... \
  --interaction-mode native_choice \
  --json
```

For the text-only run, use `--interaction-mode text_fallback`. A missing or
incorrect interaction marker is rejected before the network request.

For handoff, run `lecturecast director handoff <project-path> --json`, create a
fresh task in the next host from that payload, and execute its exact resume
command. The dogfood payload includes and requires `--fresh-task` whenever the
adapter changes:

```bash
lecturecast director resume <project-path> \
  --adapter <claude-code|openclaw> \
  --fresh-task \
  --json
```

## Local outputs and receipt

After a paid run returns a ready, current-production-key Manifest, complete the
normal local render. The output directory must contain exactly the Manifest's
two videos and two covers:

- 16:9 MP4, 1920×1080;
- 9:16 MP4, 1080×1920;
- 16:9 PNG cover, 1920×1080;
- 3:4 PNG cover, 1242×1660.

```bash
lecturecast dogfood capture-render <project-path> \
  --output-dir <local-output-directory> \
  --json

lecturecast dogfood finish <project-path> \
  --receipt-out <new-receipt.json> \
  --json
```

`capture-render` runs the normal Manifest preflight, probes dimensions and video
duration, and hashes the actual regular files. It rejects symlinks, escaped
paths, empty files, incompatible output contracts, and non-current production
signatures. `finish` creates a mode-0600 receipt exclusively and never
overwrites existing evidence.

The text fallback run uses `finish` directly and must contain no generation,
Manifest, signature, or output evidence.

## Aggregate gate

Pass exactly five independently collected receipts:

```bash
lecturecast dogfood gate \
  <codex-native.json> \
  <claude-native.json> \
  <openclaw-native.json> \
  <handoff.json> \
  <text-fallback.json> \
  --evidence-out <new-gate-report.json> \
  --json
```

The gate requires five unique run/project IDs, four unique paid generation IDs
and Manifest digests, the exact host matrix, the same attested released wheel
across all receipts, the same current production signing key, and the exact four
local outputs on every paid run. A failed report is still written for audit and
the command exits non-zero.

Receipts and the aggregate report are release evidence, not credentials. Keep
them with the release record. Never add source media, API keys, private signing
seed, local paths, transcripts, voice files, or rendered media to them.
