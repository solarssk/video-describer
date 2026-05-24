# Video Describer

Automatically generates text descriptions of GoPro / Insta360 recordings (and photos) with timestamps, using Claude AI. Optionally transcribes speech with Whisper.

> **Current status:** macOS only. Cross-platform support is on the roadmap.

**Version:** 0.2.0

---

## What it does

Point it at a folder of video files. It extracts frames every few seconds, sends them to Claude, and gets back a timestamped description of what's happening — who's there, what they're doing, the mood. Useful for quickly cataloguing action camera footage without watching everything.

Example output:
```
VID_20250829_173904_00_003 - departure day, Filip and Jadzia pack the motorcycle outside the building
00:15 Filip buckles the roll bags.
02:30 Jadzia checks the map on her phone.
08:12 they pull out from the building.
```

---

## Requirements

- **macOS**, Python 3.9+
- **ffmpeg** — `brew install ffmpeg`
- **Anthropic API key** — [console.anthropic.com](https://console.anthropic.com/settings/api-keys)
- A browser (Safari, Chrome, Firefox all work)

### Optional: speech transcription

If you want Whisper to transcribe what people say in the recordings, install one of:

```bash
# Apple Silicon (M1/M2/M3/M4) — uses the Neural Engine, much faster:
pip3 install mlx-whisper

# Intel Mac — CPU only:
pip3 install faster-whisper
```

If neither is installed, image analysis still works fine — just no speech transcription.

---

## Installation

```bash
git clone https://github.com/solarssk/video-describer.git
cd video-describer
pip3 install -r requirements.txt
```

---

## Usage

### Web UI (recommended)

```bash
python3 web_app.py
```

Open `http://localhost:5555` in your browser. On first launch, go to the **Connectors** tab and paste your Anthropic API key — it's stored locally in `config.json` (never leaves your machine).

### CLI

```bash
export ANTHROPIC_API_KEY='sk-ant-...'

# Whole folder
python3 describe_videos.py /Volumes/GoPro/DCIM/

# With speech transcription
python3 describe_videos.py . --transcribe --whisper-model medium

# With context and people
python3 describe_videos.py . \
  --people "Alice, Bob" \
  --context "motorcycle trip, Poland to Serbia"
```

---

## Features

- **GoPro `.mp4`** — single video stream
- **Insta360 `.insv` dual-lens** — detects both cameras, analyzes each separately
- **Photos `.jpg/.jpeg/.png`** — single-frame analysis
- **AI image analysis on/off** — frames sent to Claude for description (requires API key)
- **Speech transcription on/off** — Whisper runs locally; can be used alongside AI or as standalone transcript
- **Auto-resume** — skips files that already have a `.txt`, so you can rerun after interruption
- **Pre-flight check** — verifies the API key before loading Whisper or running ffmpeg
- **Cost tracking** — live token usage and cost visible in the header
- **Whisper auto-fallback** — if the system overheats, automatically steps down to a lighter model
- **Settings tab** — model, pricing, frame size, system prompt — editable in the UI without touching code
- **PL/EN UI toggle** — interface language is independent from output language (which is driven by the system prompt)

---

## Configuration

Everything useful is in the **Settings tab** in the UI. For direct edits:

### `config.json`

Created automatically on first launch from `config.default.json`. Key fields:

| Section | Field | What it does |
|---|---|---|
| `ai.anthropic` | `model` | Claude model (default: `claude-sonnet-4-6`) |
| | `max_tokens_video` / `_photo` | Max response length |
| | `price_input_per_mtok_usd` | Input token price (for cost display) |
| `frames` | `video_width_px` | Frame width sent to AI — smaller = cheaper |
| | `max_per_video` | Max frames per video |
| `defaults` | `people` | People list pre-filled in the form |
| | `context` | Default recording context |
| `whisper` | `default_model` | Default Whisper model |
| | `fallback_tiers` | Downgrade order when the system overheats |

### `prompts/system.md`

The system prompt — controls tone, format, and output language. Edit freely, or use the preset buttons in **Settings → System prompt**:
- **Load PL preset** — Polish output
- **Load EN preset** — English output

### UI language vs. output language

These are independent:
- **UI language** (toggle in header) — changes interface labels only, stored in browser `localStorage`
- **Output language** — set by the system prompt; change it in Settings → System prompt

### Reset to defaults

- **Config values** — Settings → "↺ Restore defaults" (does not touch the prompt)
- **System prompt** — use the PL / EN preset buttons
- **Full reset** — delete `config.json` and `prompts/system.md`, restart; both regenerate from defaults

---

## Cost

With `claude-sonnet-4-6` and default settings (up to 100 frames at 640 px per video):
- roughly $0.15–0.25 per 30-minute recording
- live token count and cost are shown in the header during processing

---

## Project structure

```
video-describer/
├── VERSION
├── README.md
├── requirements.txt
├── config.default.json      ← factory settings (tracked in git)
├── config.json              ← your settings + API keys (gitignored)
├── config_loader.py
├── describe_videos.py       ← processing logic + CLI
├── web_app.py               ← Flask server
├── providers/
│   ├── __init__.py
│   ├── base.py
│   └── anthropic_provider.py
├── prompts/
│   ├── system.pl.default.md
│   ├── system.en.default.md
│   └── system.md            ← your prompt (gitignored)
├── templates/
│   └── index.html
└── static/
    ├── style.css
    ├── app.js
    └── i18n/
        ├── pl.json
        └── en.json
```

---

## Adding a new AI provider

1. Create `providers/<name>_provider.py` implementing `AIProvider` from `providers/base.py` — two methods: `verify()` and `describe(content_blocks, system_prompt, max_tokens)`.
2. Register it in `providers/__init__.py` `REGISTRY`.
3. Add a config block under `ai.<name>` in `config.default.json`.
4. Switch providers by changing `ai.provider` in `config.json`.

---

## License

MIT
