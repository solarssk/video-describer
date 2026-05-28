# Video Describer

[![CI](https://github.com/solarssk/video-describer/actions/workflows/ci.yml/badge.svg)](https://github.com/solarssk/video-describer/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.9+-3776ab)
![macOS](https://img.shields.io/badge/macOS-only-lightgrey)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](#license)

---

We rode from Warsaw to Muscat, Oman — through Turkey, Iraq, Kuwait, Saudi Arabia, and the UAE — and came back with 1 TB of footage across two cameras. Our editor needed to start cutting, but figuring out what was even on each clip was going to take days of scrubbing before any real work could begin.

**video-describer** points Claude at a folder of recordings and comes back with timestamped descriptions of what's happening in each file. Who's there, what they're doing, where they are, what the light looks like. Enough for an editor to know which clips are worth opening before they open them.

It works on common camera and phone video formats backed by ffmpeg, including GoPro, Insta360, and iPhone `.mov` clips. Whisper transcription is optional — useful when there's actual dialogue you'd want to find later.

---

## 📄 What the output looks like

```
VID_20250829_173904 — departure day, Filip and Jadzia pack the motorcycle outside the building
00:15  Filip buckles the roll bags, checks the straps twice
02:30  Jadzia looks at the map on her phone, points at something
08:12  they pull out onto the street, morning light, long shadows
```

One `.txt` per file, next to the original. New outputs use the source filename plus `.txt`, for example `video.mp4.txt`, so `video.mp4` and `video.jpg` cannot collide. Your editor can grep it, read it, feed it into their own workflow — it's just text.

New `.txt` files also end with a small metadata footer:

```text
---
source: video.mp4
uuid: d1e2f3a4-...
batch: a3f8b2c1-...
processed: 2026-05-26T12:34:00+00:00
model: claude-sonnet-4-6
```

Older `video.txt` outputs from previous versions are still treated as valid legacy results. To update an existing folder without re-processing the media, run:

```bash
python3 describe_videos.py /path/to/folder --retrofit-existing
```

This renames unambiguous legacy files such as `video.txt` to `video.mp4.txt` and adds the metadata footer. It does not call any AI provider and does not require an API key. Use `--dry-run` first if you want to see counters without writing changes.

---

## ⚙️ Requirements

- macOS (Apple Silicon or Intel), Python 3.9+
- [ffmpeg](https://ffmpeg.org/) — `brew install ffmpeg`
- [Anthropic API key](https://console.anthropic.com/settings/api-keys)

---

## 🚀 Quick start

```bash
git clone https://github.com/solarssk/video-describer.git
cd video-describer
pip3 install -r requirements.txt
python3 web_app.py
```

Open `http://localhost:5555`. Go to **Connectors**, paste your API key. Point it at a folder. That's it.

The key is stored locally in `config.json` — it never leaves your machine.

---

## 🎙️ Speech transcription (optional)

If the footage has dialogue worth capturing, install a Whisper backend:

```bash
# Apple Silicon — runs on the Neural Engine, fast
pip3 install mlx-whisper

# Intel Mac — CPU only
pip3 install faster-whisper
```

You can run it alongside image analysis or as a standalone transcript. If neither is installed, image analysis still works fine.

---

## 🎬 NLE export (optional)

After processing, the app can write marker sidecar files next to each `.txt`:

| Format | File | Works with |
|---|---|---|
| FCPXML | `video.mp4.fcpxml` | Final Cut Pro |
| EDL | `video.mp4.edl` | DaVinci Resolve |
| FCP7 XML | `video.mp4.xml` | Adobe Premiere |

Key moments marked with ★ in the description become named markers on the timeline. Enable formats in **Settings → NLE Export**.

Already processed a batch and want to add markers now? Use **Convert existing** — it reads your `.txt` files and writes the sidecars at zero API cost.

---

## 🔔 Notifications (optional)

Long batches run in the background. Three ways to know when they're done:

- **Browser notification** — the browser asks for permission once, then pops a native notification when the batch finishes. Clicking it focuses the tab. Works in Chrome, Firefox, Safari.
- **macOS notification** — native system notification with filename, cost, and duration.
- **Webhook** — POST to any URL: Slack, Discord, Make.com, or your own endpoint. Discord embed format is supported automatically.

Configure all three in **Settings → Notifications**.

---

## 💻 CLI

If you prefer terminal over browser:

```bash
export ANTHROPIC_API_KEY='sk-ant-...'

python3 describe_videos.py /Volumes/GoPro/DCIM/

# with transcription
python3 describe_videos.py . --transcribe --whisper-model medium

# with context
python3 describe_videos.py . \
  --people "Filip, Jadzia" \
  --context "motorcycle trip, Poland to Oman"
```

---

## 🔍 How it works

1. ffmpeg extracts one frame every N seconds (default: 5s, configurable)
2. Frames are sent to the AI provider with a system prompt that tells it who the people are and what the trip is about
3. The AI returns a timestamped description
4. The description is saved as a `.txt` next to the original file

For Insta360 `.insv` files, it detects both lenses and analyzes them separately.

---

## 📁 Supported files

The app scans the selected folder non-recursively and processes files with these extensions:

| Type | Extensions | Notes |
|---|---|---|
| Video | `.mp4`, `.mov`, `.avi`, `.mkv`, `.mts`, `.m2ts`, `.insv` | Includes typical iPhone `.mov` clips. Actual codec support depends on your local `ffmpeg`. |
| Photos | `.jpg`, `.jpeg`, `.png` | iPhone `.heic` / `.heif` photos are not currently included. |

Unsupported files are ignored when scanning a folder, and a directly selected unsupported file is shown as unsupported in the UI.

---

## ✨ Features

**AI & analysis**
- 🤖 **Claude, OpenAI GPT-4o, Google Gemini** — switch providers in Settings; each has its own model and pricing config
- 🖼️ **Image analysis** — frames extracted by ffmpeg, described by AI with timestamps
- 🎙️ **Speech transcription** — optional Whisper integration; mlx-whisper on Apple Silicon, faster-whisper on Intel; auto-fallback to lighter model when the system overheats
- 🌡️ **Thermal protection** — Whisper steps down to a lighter model automatically during long batches if the Mac overheats

**Batch & workflow**
- 💾 **Batch resume** — if the batch stops (crash, power loss, Stop button), the app stores a manifest in `batch_state.json` with one UUID and status per file; on next launch it offers to pick up from file 7/15, $0.43 already spent
- 💰 **Budget guard** — set a USD cap before starting; the batch stops gracefully before it would exceed it
- ✅ **File selection** — deselect individual files from the list before starting
- 📝 **Folder summary** — after each batch, `_summary.txt` is written: one line per file with a short description, plus totals
- 🔄 **Convert existing** — generate NLE sidecars from already-processed `.txt` files, no AI calls, no API cost
- 🔁 **Existing output retrofit** — upgrade old `stem.txt` naming to `name.ext.txt` and add metadata footers without re-processing

**Export**
- 🎬 **NLE export** — FCPXML (Final Cut Pro), EDL (DaVinci Resolve), FCP7 XML (Premiere); ★ key moments become timeline markers

**Notifications**
- 🔔 **Browser notification** — Web Notifications API; pops when batch finishes, click focuses the tab
- 🍎 **macOS notification** — native system popup with filename, cost, duration
- 🔗 **Webhook** — POST to Slack, Discord, Make.com, or any HTTP endpoint

**UI & observability**
- 📊 **Live cost tracking** — token count and running USD cost in the header
- 🔍 **Pre-flight check** — verifies the API key and ffmpeg before doing any heavy work
- 📋 **Log file** — everything written to the UI is also appended to `logs/debug.log` (daily rotation, 30 days, gitignored)
- 🌐 **PL / EN UI** — language dropdown with flag emojis, independent from output language
- ⚙️ **Settings tab** — model, pricing, frame interval, system prompt — editable in the UI without touching files

---

## 💵 Cost

With `claude-sonnet-4-6` at default settings (up to 100 frames per video, 640 px wide):

> roughly **$0.15–0.25 per 30-minute recording**

Live token count and running cost are shown in the header while processing.

---

## 🛠️ Configuration

Everything is in the **Settings tab**. For direct edits, see `config.json` (created from `config.default.json` on first launch):

| Field | Default | What it does |
|---|---|---|
| `ai.provider` | `anthropic` | Active AI provider (`anthropic`, `openai`, `gemini`) |
| `ai.anthropic.model` | `claude-sonnet-4-6` | Claude model |
| `frames.video_width_px` | `640` | Smaller = cheaper, lower detail |
| `frames.max_per_video` | `100` | Cap per file |
| `defaults.output_language` | `pl` | Language for output scaffolding; independent from UI language |
| `defaults.people` | — | Pre-filled people list |
| `defaults.context` | — | Pre-filled trip context |
| `whisper.default_model` | `medium` | Starting Whisper model |
| `notifications.browser_notify` | `false` | Browser Web Notification on batch done |
| `notifications.macos_notify` | `false` | macOS system notification on batch done |
| `notifications.webhook_url` | — | Webhook POST URL |

The system prompt lives in `prompts/system.md`. Change it to change the output language, tone, or format. PL and EN presets are available in Settings. The UI language toggle does not change output language.

---

## 🗂️ Project structure

```
video-describer/
├── web_app.py               — Waitress/Flask app, HTTP endpoints, SSE
├── processor.py             — batch loop, resume state, cost/log plumbing
├── batch_metadata.py        — batch manifest + .txt metadata helpers
├── describe_videos.py       — media/frame/transcription helpers + CLI
├── output_paths.py          — new/legacy output path handling
├── retrofit_outputs.py      — safe upgrade path for existing .txt outputs
├── timefmt.py               — timestamp formatting
├── nle_export.py            — FCPXML / EDL / FCP7 XML sidecar export
├── config_loader.py
├── config.default.json      — factory settings (in git)
├── config.json              — your settings + API key (gitignored)
├── providers/
│   ├── base.py
│   ├── anthropic_provider.py
│   ├── openai_provider.py
│   └── gemini_provider.py
├── prompts/
│   ├── system.pl.default.md
│   ├── system.en.default.md
│   └── system.md            — your prompt (gitignored)
├── templates/index.html
├── static/
│   ├── style.css
│   ├── app.js
│   ├── icons/               — favicon + notification icon
│   └── i18n/pl.json, en.json
└── tools/
    └── macos_path_picker.swift  — native folder/file picker (compiled on first use)
```

---

## 🔌 Adding a provider

Implement `AIProvider` from `providers/base.py` — two methods: `verify()` and `describe()`. Register it in `providers/__init__.py` and add a config block under `ai.<name>` in `config.default.json`.

---

## 🌍 Origin

[Desert Horizons 2025](https://warsawtravelers.pl/en/desert-horizons-2025/) — Warsaw to Muscat, Oman, through Turkey, Iraq, Kuwait, Saudi Arabia, and the UAE. 11,000+ km on a BMW R1250GS, two cameras, about 1 TB of raw footage.

Miłosz, who does post-production for our [YouTube channel](https://www.youtube.com/@filipchochol), needed to start cutting. But before any editing, someone had to figure out what was on each clip. That was going to take days.

Editors are already using AI in their workflows. This is the part before that: giving them a map of the material before they open a single file.

---

## 💡 Tips

**External disk ejection** — when the batch writes `.txt` files to an external volume, macOS Spotlight indexes them automatically, which can delay the "eject" command for a few seconds after processing ends. If that's annoying, disable Spotlight for the volume:

```bash
sudo mdutil -i off /Volumes/your-disk
```

Or add it via System Settings → Siri & Spotlight → Spotlight Privacy.

---

## 📄 License

MIT
