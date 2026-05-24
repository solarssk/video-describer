# Changelog

All notable changes to Video Describer are documented here.

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
