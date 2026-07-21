# Supported platforms

LectureCast supports two native desktop environments:

- **macOS** through Terminal and `scripts/install.sh`.
- **Windows** through PowerShell and `scripts/install.ps1`.

Linux distributions and WSL are not supported product surfaces. The repository
may contain transitive Linux packages inside `package-lock.json`, but that does
not constitute a Linux support commitment.

Both supported platforms keep the same product boundary: original media,
voice, subtitles, Remotion, ffmpeg, covers, and exported videos remain local.
Director requests contain structured creative inputs only.

## Runtime requirements

Both platforms require Python 3.11+, Node 20+ with npm, and an ffmpeg build with
libass. On macOS, Python must match the host architecture; the installer rejects
a Rosetta x86_64 Python on Apple Silicon before installing dependencies.
`lecturecast doctor --project-root <project>` is the source of truth for renderer
readiness.

- macOS uses `Arial Unicode MS` for ASS subtitles by default.
- Windows uses the system `Microsoft YaHei` family by default.
- `LECTURECAST_SUBTITLE_FONT` can select another locally installed CJK family.

LectureCast does not bundle, upload, or redistribute operating-system fonts.
