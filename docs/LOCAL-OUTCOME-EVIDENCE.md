# Local outcome evidence

LectureCast does not track users and does not send telemetry. Limited-cohort
participants may explicitly create a local outcome record and, only after a
second explicit consent step, export a bounded anonymous report for manual
sharing.

No command in this workflow opens a network connection.

## Two separate artifacts

### Private local receipt

`lecturecast outcome record` stores `.lecturecast/outcome-receipt.json` with
mode `0600`. It is bound to the current local project and verified signed
ProductionManifest. It may contain the local project ID and Manifest digest,
so it is marked `shareable: false` and must stay on the user's machine.

The receipt records only structured choices:

- render status: `completed`, `partial`, `failed`, or `not_attempted`;
- adoption status: `published`, `exported`, `discarded`, or `undecided`;
- an optional bounded failure-reason code.

There is no free-text field. Updates require the current receipt revision so
two agents cannot silently overwrite each other.

### Anonymous manual report

`lecturecast outcome export` requires the exact consent value
`share-anonymous-outcome`. It creates a new `0600` file and refuses to
overwrite an existing path. The report contains only:

- a random report ID used for duplicate rejection;
- the structured outcome choices;
- the installed LectureCast version;
- fixed privacy declarations.

It never contains an account, user, project, Director session/generation,
Manifest digest or signing key ID, source, prompt, local path, output name,
media metadata, file hash, credential, timestamp, or free text. Exporting does
not transmit it; the user decides whether and how to share the file.

## Commands

```bash
# Create the first private local receipt.
lecturecast outcome record ./my-video \
  --render-status completed \
  --adoption-status exported \
  --json

# Inspect it before any export. The result remains marked non-shareable.
lecturecast outcome status ./my-video --json

# Update after publishing; use the revision returned by status.
lecturecast outcome record ./my-video \
  --render-status completed \
  --adoption-status published \
  --expected-revision 1 \
  --json

# Explicitly create a bounded report. This still performs no upload.
lecturecast outcome export ./my-video \
  --report-out ./anonymous-outcome.json \
  --consent share-anonymous-outcome \
  --json

# A recipient can validate the exact public shape offline.
lecturecast outcome verify ./anonymous-outcome.json --json

# Aggregate at least three unique reports without retaining report IDs.
lecturecast outcome aggregate ./report-1.json ./report-2.json ./report-3.json \
  --evidence-out ./outcome-aggregate.json \
  --json
```

For `partial` or `failed`, provide one failure reason from the CLI help. A
`published` or `exported` outcome requires `render-status=completed`. A failure
reason is rejected when rendering completed.

## Agent behavior

Agents must not run this workflow automatically. When a user explicitly opts
into outcome evidence, use the host's native choice control for the bounded
questions and show the anonymous report before the user decides whether to
share it. Text fallback may use the same stable enum values. Never infer an
outcome from files, upload a render, or claim the export was transmitted.
