# Narration timing contract v1

LectureCast separates two immutable concerns:

1. The cloud Director signs the creative `ProductionManifest`: narration,
   planned sections, scenes and outputs.
2. The local client derives a digest-bound execution timeline from real TTS
   audio. The signed Manifest is never rewritten.

## Static Director and client check

Both sides count language-aware spoken units before local production:

- `zh`, `ja`, `ko`: each CJK character plus each Latin/number token;
- other languages: spoken word tokens.

Every section and the complete narration must stay within these deliberately
broad safety bounds:

| Language | Minimum | Maximum |
|---|---:|---:|
| zh / ja / ko | 45 units/min | 720 units/min |
| other | 45 words/min | 360 words/min |

The Director additionally requires a five-minute course plan between 240 and
390 seconds. During generation it uses the approved Brief pace as a tighter
target. CJK uses the greater of 260 units/min or 1.4 times
`words_per_minute`, accepting 100–140% of that target; other languages accept
75–140% of `words_per_minute`. The signed-Manifest defense-in-depth check uses
the broad bounds because the public Manifest does not carry the Brief's pace
field. These checks reject
obviously impossible declarations such as a few dozen Chinese characters
assigned to a two-minute scene. They are not a substitute for measuring the
selected TTS engine.

## Human script gate

After the signed Manifest is ready, `lecturecast manifest review` returns the
complete script and timing result. The local render requires an explicit
`manifest-approval.json` receipt bound to both the Manifest digest and script
digest. A changed or different Manifest invalidates the receipt.

## Measured local execution plan

The local audio builder synthesizes one file per section, probes every duration,
re-encodes the concatenation, and writes `.lecturecast/build/audio-timing.json`.
The plan contains:

- the signed Manifest digest;
- measured per-section start and duration frames;
- the measured narration duration and render total frames;
- the actual/planned duration ratio.

Actual TTS must be within 75–125% of every planned section and the whole
narration. Outside that range the workflow stops before Remotion. Inside the
range, the measured plan drives scenes, subtitles and composition duration, so
minor voice-engine differences do not create a silent tail.

## Delivery validation

Final validation checks dimensions, expected measured duration, presence of an
audio stream, and that video duration differs from the measured narration by no
more than one second. Container padding can no longer turn a short narration
into a false PASS.
