# Changelog

All notable changes to Video Describer are documented here.

---

## [Unreleased]

---

## [0.4.1] — 2026-05-29

### Added

- **Docker image** — official container image published to GitHub Container Registry (`ghcr.io/solarssk/video-describer`) on every version tag. Image includes ffmpeg and faster-whisper; bind host configurable via `BIND_HOST` env variable (default `0.0.0.0` in container).

### Fixed

- Container runs as non-root user (`appuser`, uid 1000) — satisfies Docker security best practices
- `faster-whisper` pinned to `1.2.1` in Docker image for reproducible builds
- Removed redundant `import os as _os` in `web_app.py` (module was already imported at top-level)
- CI checkout action no longer persists credentials unnecessarily (`persist-credentials: false`)

---

## [0.4.0] — 2026-05-28

### Added

- **Multi-provider AI** — OpenAI GPT-4o and Google Gemini as drop-in alternatives to Anthropic Claude for image analysis. Switch in the Settings tab; each provider has its own model, token, and pricing config. Provider architecture makes it straightforward to add more.
- **NLE export** — generate sidecar marker files alongside each `.txt` after processing: FCPXML (Final Cut Pro), EDL (DaVinci Resolve), FCP7 XML (Adobe Premiere). Key moments with ★ in the description become named markers on the timeline. Configurable in Settings → NLE Export; can also be run retroactively on existing outputs via the Convert existing mode.
- **Convert existing mode** — generate NLE sidecars from already-processed `.txt` files at zero API cost. Useful when you processed a batch earlier and want to export markers now, or when you've edited the descriptions and want to regenerate the sidecars.
- **Notifications** — three independent channels, all configurable in Settings:
  - *macOS* — native system notification after each batch (filename, cost, duration)
  - *Browser* — Web Notifications API; browser asks for permission once, then pops a native notification when the batch finishes; clicking focuses the tab
  - *Webhook* — POST to any URL (Slack, Discord, Make.com, custom endpoint); payload includes `status`, `processed`, `skipped`, `errors`, `cost_usd`, `duration_sec`. Discord embed format supported automatically.
- **Batch manifest schema v2** — `batch_state.json` stores `schema_version`, `batch_id`, and a per-file manifest with UUID, output path, status, and error fields.
- **Metadata footer** — generated `.txt` outputs end with `source`, `uuid`, `batch`, `processed`, and `model` fields after a `---` separator. Summary generation ignores the footer.
- **Existing output retrofit** — `python3 describe_videos.py PATH --retrofit-existing` upgrades old `stem.txt` outputs to `name.ext.txt` naming and appends metadata without re-processing media or requiring an API key. Use `--dry-run` first to preview changes.
- **Diagnostic logs** — `logs/debug.log` with daily rotation (30 days). Each session writes a startup banner; each API call logs token counts and cost (`↳ 61,840 in / 287 out tok — $0.0891`); each completed file logs a summary line.
- **Favicon + tab title** — tab icon and title change to reflect batch state: ⚙ Processing / ✓ Done / ✗ Error.
- **Language dropdown** — UI language selector replaced with a dropdown with flag emojis (🇵🇱 / 🇬🇧), ready for additional languages.

### Changed

- **Output language decoupled from UI language** — `defaults.output_language` in `config.json` controls the language of generated descriptions; the PL/EN toggle controls only the interface. Previously changing UI language also affected output.
- **Processing logic extracted** — `processor.py` contains the full batch loop (was part of `web_app.py`). `web_app.py` handles only HTTP endpoints and SSE.
- **Batch state secret redaction** — recursive; removes nested API keys, webhook URLs, tokens, and secrets before writing state to disk.

### Fixed

- Timestamps beyond 1 hour now format correctly (was showing wrong minutes)
- Filename collision between `video.mp4` and `video.jpg` (both would map to `video.txt`) — resolved by always appending the full extension
- Convert mode toggle and NLE export edge cases (duplicate sidecars, wrong FPS fallback)
- Single-tab guard now uses a TTL heartbeat instead of tab-close detection — survives page refreshes correctly
- Settings buttons (Save, Restore defaults) remain enabled and functional during active processing
- API key no longer wiped on Settings save — `config.json` uses deep-merge so fields not in the form are preserved
- Swift path picker returns focus to the browser window after folder/file selection
- Skip detection improved — correctly identifies both `name.ext.txt` (current) and `name.txt` (legacy) as existing output

---

## [0.3.0] — 2026-05-25

### Added

- **Batch resume** — state saved to `batch_state.json` after each file; on restart the app shows a banner ("Resume? 7/15 files, $0.43 spent") and picks up from where it left off. Resume is filename-verified, not just index-based, so adding or removing files between crash and resume is handled safely.
- **Budget guard** — optional USD limit in the Start form; pre-batch check blocks the run if the estimate exceeds the limit, mid-batch check stops gracefully before each new file.
- **Folder summary** — `_summary.txt` written to the input folder after each batch: one line per file (filename + first-line description), plus totals (files, cost, model). Skipped and pre-resume files are included so the summary is always complete.
- **File selection checkboxes** — deselect individual files from the list before starting; backend `/start` accepts an optional `files` filter.
- **Frame interval warning** — when the interval is auto-adjusted to fit the `max_frames` cap, a yellow `⚠` log line says exactly what changed and why, instead of silently changing settings.
- **Rotating log file** — every print-to-UI line is also written to `logs/app.log` (daily rotation, 30 days retention, gitignored). Each startup writes a banner with timestamp and port. A per-API-call token and cost breakdown (`↳ 61,840 in / 287 out tok — $0.0891`) is logged during processing immediately after each API response; a separate per-file summary (`[file:name.mp4] in=… out=… cost=…`) is logged at file completion.
- **Waitress WSGI server** — replaces the Flask development server; no more "WARNING: This is a development server" on startup. SSE streaming and long-running batches work without timeout issues.
- **Native macOS folder/file picker** — compiled Swift helper (`tools/macos_path_picker.swift`) opens the system-native panel with a title bar and Cancel button; compiled once on first use, cached for subsequent runs. Falls back to osascript when Swift compiler is unavailable. Remembers the last picked directory across sessions. Picker errors (timeout, missing toolchain, user cancel) are shown inline below the path field instead of silently failing.

### Fixed

- **Whisper SIGABRT crash** (exit code −6) — `multiprocessing` context changed from `fork` to `spawn`. `fork` + Flask threads + ObjC runtime caused `+[NSCheapMutableString initialize]` to race across the fork boundary, crashing every Whisper job silently. `spawn` gives the child a clean interpreter.
- **Whisper crash messages** — instead of `"Whisper process exited with code -6"`, the UI now shows `"Whisper crashed (SIGABRT (ObjC/Metal crash)) — transcription skipped for this file"`. Batch continues with frame-only description.
- **Resume: errored files retried** — state was saved with `next_index = abs_index` after an error, so resume would skip the failed file. Now saves `abs_index − 1` so the file is retried.
- **Resume: complete summary** — `_summary.txt` now includes files processed before the interruption (reconstructed from existing `.txt` outputs), not just the tail processed after resume.
- **Resume: skipped files in summary** — files with existing `.txt` that are skipped on re-run now contribute their first line to the summary, so a re-run over a completed batch regenerates the full `_summary.txt` at zero API cost.
- **Budget: resume offset in preflight** — pre-batch estimate check now adds `resume_cost_offset` to `est_cost` before comparing against the limit, so a resumed batch with $0.80 already spent correctly fails a $1.00 budget check.
- **Budget message** — mid-batch "budget reached" message now shows absolute file count (`10/100`) instead of the remaining slice count (`10/40`) when resuming.

---

## [0.2.0] — 2026-05-24

### Added

- **Web UI** — Flask-based interface at `localhost:5555`; replaces running the CLI directly for most workflows
- **Connectors tab** — API keys stored in `config.json` (gitignored), no longer passed as env vars or CLI flags
- **Insta360 `.insv` support** — dual-lens detection, both cameras analyzed separately
- **Speech transcription** — optional Whisper integration; mlx-whisper on Apple Silicon, faster-whisper on Intel
- **Whisper auto-fallback** — steps down to a lighter model automatically when the system overheats during a long batch
- **Cost tracking** — live token count and USD cost shown in the header during processing
- **Pre-flight check** — verifies the API key and ffmpeg before doing any heavy work
- **Settings tab** — model, pricing, frame interval, system prompt editable in the UI
- **PL / EN UI toggle** — interface language independent from output language
- **OpenAI provider** — optional fallback; Whisper via OpenAI API when no local backend is installed
- **Provider architecture** — `providers/` module makes it straightforward to add new AI backends

### Changed

- Config structure migrated from flat `claude.*` keys to `ai.anthropic.*` (legacy configs auto-migrate on first load)
- Frame extraction defaults tuned: 640 px wide, up to 100 frames per video

---

## [0.1.0] — 2025-08-01

Initial release. CLI only.

- Frame extraction via ffmpeg
- Timestamped descriptions via Claude API
- GoPro `.mp4` support
- Auto-resume (skips files with existing `.txt`)
- `--people` and `--context` flags
