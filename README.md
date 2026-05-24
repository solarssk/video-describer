# Video Describer

Automatically generates descriptions of GoPro / Insta360 video recordings (and photos) with timestamps, using Claude AI + optional Whisper for speech transcription.

> The UI supports **PL** and **EN** (toggle in the header). The default system prompt is Polish (the original author records in Polish) but you can switch the prompt to an EN preset in Settings without losing your customizations to the UI language.

**Version:** 0.1.0

## Requirements

- **macOS / Windows / Linux**, Python 3.9+
- `ffmpeg` available on PATH
  - macOS: `brew install ffmpeg`
  - Windows: [ffmpeg.org/download](https://ffmpeg.org/download.html) or `winget install ffmpeg`
  - Linux: `apt install ffmpeg` (Debian/Ubuntu) / `dnf install ffmpeg` (Fedora)
- `tkinter` for the native file picker (Windows/Linux)
  - macOS: not required (uses AppleScript)
  - Windows: usually bundled with Python
  - Linux: `apt install python3-tk` (Debian/Ubuntu)
- Anthropic API key — [console.anthropic.com](https://console.anthropic.com/settings/api-keys)
- Browser: Chrome, Brave, Edge, Safari (recommended) or Firefox (works, but the API key field falls back to `type=password` so password managers may try to autofill)

### Cross-platform compatibility

| Feature | macOS | Windows | Linux |
|---|:-:|:-:|:-:|
| Video / photo processing | ✅ | ✅ | ✅ |
| Native file picker | ✅ AppleScript | ✅ tkinter | ✅ tkinter |
| Sleep prevention during processing | ✅ caffeinate | ✅ SetThreadExecutionState | ❌ none |
| Thermal throttling detection | ✅ pmset | ❌ none | ❌ none |
| Whisper auto-fallback | ✅ (thermal + load) | ⚠ load avg only | ⚠ load avg only |

## Installation

```bash
git clone https://github.com/<your-account>/video-describer.git
cd video-describer
pip3 install -r requirements.txt
```

## Usage

### Web UI (recommended)

```bash
python3 web_app.py
```

Open `http://localhost:5555`. Paste your API key in the form (it is stored in browser localStorage).

### CLI

```bash
export ANTHROPIC_API_KEY='sk-ant-...'

# Whole folder
python3 describe_videos.py /Volumes/trip/2025-08-29/

# With speech transcription (Whisper on CPU — slow but accurate)
python3 describe_videos.py . --transcribe --whisper-model medium

# With custom context and people
python3 describe_videos.py . \
  --people "Alice, Bob, Charlie" \
  --context "road trip USA west coast"
```

## Features

- **GoPro `.mp4`** — single video stream
- **Insta360 `.insv` dual-lens** — automatically detects 2 cameras, analyzes both
- **Photos `.jpg/.jpeg/.png`** — single-frame analysis
- **Two independent feature toggles:**
  - **AI image analysis** — frames sent to Claude for description (requires API key)
  - **Speech transcription** — Whisper runs locally, merged into the description or saved as transcript-only when AI is off
- **AI provider abstraction** — switch providers via `config.json` (currently: Anthropic; OpenAI/Gemini = plug new class into `providers/`)
- **Hard-block before any work** — Start is disabled if AI is on but no API key (form or `ANTHROPIC_API_KEY` env); pre-flight catches bad keys before loading Whisper or running ffmpeg
- **Auto-resume** — skips files that already have a `.txt`
- **Web UI** with progress bar, live log, sleep-resilient (caffeinate / SetThreadExecutionState)
- **Whisper auto-fallback** — when the system overheats, downgrades model one tier
- **Token usage + cost tracking** — shown live in the header
- **Settings tab** — model, pricing, frame size, system prompt — all editable without touching code
- **PL/EN UI toggle** — independent from output language (which is driven by the system prompt preset)

## Configuration

Everything that makes sense to tweak without editing code is in the **Settings tab** in the UI, or directly in the files:

### `config.json`

Generated on first launch from `config.default.json`. Sections:

| Section | Field | What it does |
|---|---|---|
| `ai` | `provider` | Active AI provider key (currently only `anthropic`) |
| `ai.anthropic` | `model` | Anthropic model, defaults to `claude-sonnet-4-6` |
| | `max_tokens_video` / `_photo` | Response length limit |
| | `price_input_per_mtok_usd` | Input token price (for cost calculation) |
| | `price_output_per_mtok_usd` | Output token price |
| | `timeout_sec` | Max wait time for the API response |
| `frames` | `video_width_px` | Width of frames sent to the AI (smaller = cheaper) |
| | `photo_width_px` | Width of resized photos |
| | `jpeg_quality` | JPG quality, 1=best, 31=worst |
| | `max_per_video` | Max frames per single video |
| `defaults` | `people` | People list pre-populated in the form |
| | `context` | Default recording context |
| | `interval_sec` | Default frame interval |
| `whisper` | `default_model` | Default Whisper model |
| | `fallback_tiers` | Downgrade order when overheating |

> Pre-v0.2 configs with a top-level `claude` section are migrated automatically to `ai.anthropic` on first load (a warning is logged).

## Adding a new AI provider

1. Create `providers/<name>_provider.py` implementing the `AIProvider` base (see `providers/base.py`). Two methods required: `verify()` and `describe(content_blocks, system_prompt, max_tokens)`.
2. Register it in `providers/__init__.py` `REGISTRY` dict: `'name': YourProvider`.
3. Add a config section under `ai.<name>` in `config.default.json` with provider-specific fields (at minimum `model` and `timeout_sec` + any cost fields if you want cost tracking).
4. To switch, change `ai.provider` in `config.json` to your provider name.

The translation between provider SDKs happens inside the provider class — `describe_video`/`describe_photo` only know about the `AIProvider` interface.

### `prompts/system.md`

The system prompt for Claude — how it should describe recordings. Edit freely to change tone/format/rules. Available in the UI under Settings → "System prompt".

Two factory presets ship with the project:
- `prompts/system.pl.default.md` — Polish output
- `prompts/system.en.default.md` — English output

In Settings → System prompt section there are two buttons: **📥 Load PL preset** and **📥 Load EN preset**. Each shows a confirm dialog before overwriting your current prompt.

### UI language vs output language

These are **two independent settings**:

- **UI language** (PL/EN toggle in header) — purely cosmetic, changes interface labels. Stored per-browser in `localStorage`. Switching it **never** touches your system prompt.
- **Output language** — determined entirely by the system prompt. To switch output language, go to Settings → System prompt → "Load EN preset" (or PL).

So you can have e.g. EN UI with PL output, or vice versa.

### Reset to defaults

- **Config values** (model, prices, frame settings, etc.): Settings → "↺ Restore defaults" — does NOT touch the prompt.
- **System prompt**: use the preset buttons (PL / EN) under the prompt textarea.
- **Full nuke**: delete `config.json` and `prompts/system.md`, restart server — both regenerate from `.default` files.

## Cost

For `claude-sonnet-4-6` with default settings (100 frames at 640px per video):
- ~$0.15–0.25 per 30-minute recording
- Live token usage / cost visible in the header (Tokens / Cost)

## Project structure

```
video-describer/
├── VERSION                  ← version number
├── README.md
├── requirements.txt
├── config.default.json      ← factory settings (in git)
├── config.json              ← user-specific (gitignored)
├── config_loader.py         ← config load/save + legacy migration
├── describe_videos.py       ← processing logic + CLI
├── web_app.py               ← Flask backend
├── providers/               ← AI provider abstraction
│   ├── __init__.py          ← registry + make_provider()
│   ├── base.py              ← AIProvider + ProviderResponse
│   └── anthropic_provider.py
├── prompts/
│   ├── system.pl.default.md ← Polish preset (in git)
│   ├── system.en.default.md ← English preset (in git)
│   └── system.md            ← user-editable prompt (gitignored)
├── templates/
│   └── index.html
└── static/
    ├── style.css
    ├── app.js
    └── i18n/
        ├── pl.json          ← Polish UI strings
        └── en.json          ← English UI strings
```

## Output format

```
VID_20250829_173904_00_003 - dzień wyjazdu spod domu, Filip i Jadzia pakują motocykl...
00:15 Filip zapina rollbagi.
02:30 Jadzia sprawdza mapę na telefonie.
08:12 wyjeżdżają spod bloku.
```

(Polish output by default — driven by the system prompt. Change `prompts/system.md` to get English output.)

## License

MIT — add a `LICENSE` file if you publish to GitHub.
