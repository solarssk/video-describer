# Video Describer — Roadmap

> Dokument dla Claude Code / VS Code. Zawiera pełny kontekst projektu, historię decyzji i planowane kierunki.

---

## Kontekst projektu

**Video Describer** to lokalna aplikacja macOS do automatycznego opisywania nagrań wideo i zdjęć za pomocą Claude AI. Typowy use-case: 1 TB surowego materiału z wyprawy motocyklowej, editor potrzebuje wiedzieć co jest w każdym klipie zanim go otworzy.

Architektura: Flask SSE backend + single-page JS frontend. Wszystko działa lokalnie (`localhost:5555`), klucze API w `config.json` (gitignored, nigdy nie wychodzą z komputera).

---

## Status: v0.2.0 — ZROBIONE

### Core processing
- [x] Ekstrakcja klatek przez ffmpeg (konfigurowalny interwał, max klatek)
- [x] VideoToolbox hardware decode na macOS (odciążenie CPU/Media Engine)
- [x] Opis przez Claude API — timestamped opisy per-plik
- [x] GoPro `.mp4` — single stream
- [x] Insta360 `.insv` — dual-lens, oba aparaty analizowane osobno
- [x] Zdjęcia `.jpg`, `.jpeg`, `.png`
- [x] Auto-resume — pomija pliki które już mają `.txt`
- [x] Pre-flight check — weryfikuje klucz API + ffmpeg przed ciężką robotą

### Transkrypcja Whisper
- [x] mlx-whisper na Apple Silicon (Neural Engine — szybko)
- [x] faster-whisper na Intel Mac (CPU fallback)
- [x] Whisper auto-fallback przy przegrzaniu — schodzi o jeden model niżej (konfigurowalny tier list)
- [x] Scalanie transkrypcji z opisem klatek w jeden output

### Web UI
- [x] Flask SSE — live progress bez pollingu
- [x] Connectors tab — klucze API zarządzane w UI, zapisane w `config.json`
- [x] Settings tab — model, cena, interwał, system prompt, edytowalne bez dotykania plików
- [x] PL / EN language toggle — niezależny od języka outputu (który kontroluje system prompt)
- [x] Live metrics w headerze — CPU, RAM, load, temperatura (co 3s)
- [x] Locked state podczas przetwarzania — `.panel-left.locked` klasa blokuje UI
- [x] Lock banner + guard przed zamknięciem karty podczas przetwarzania
- [x] File cards — live status per-plik (progress bar, ETA, tokeny/koszt inline)
- [x] Cost tracking — tokeny i USD w nagłówku + per plik

### UX / CSS fixes (v0.2.x)
- [x] Responsive header — `flex-wrap: nowrap`, progressive hiding metryk
- [x] Step-status bar — progressive media queries dla wąskich okien (ETA znika @1060px, elapsed @940px)
- [x] Bypass-narrow mode — stack layout, poprawne proporcje log vs file-cards
- [x] Tabs emoji-only na wąskich ekranach
- [x] File card name truncation (overflow: hidden, text-overflow: ellipsis)
- [x] "Open" button → `.btn-mini` styl (był nieczytelny na ciemnym tle)
- [x] Connectors — przepisane w stylu Settings (spójny design)

### Error handling
- [x] `_clean_error_msg()` w `anthropic_provider.py` — wyciąga `body['error']['message']` zamiast surowego JSON z `request_id`
- [x] `_is_fatal_api_error()` — rozróżnia błędy fatalne (brak kredytów, invalid key) od transientnych
- [x] Czytelne komunikaty błędów w UI i logu

### Estymacja kosztów
- [x] Pre-batch cost estimate — przed startem liczy przewidywany koszt na podstawie długości wideo × interwał × heurystyka tokenów (~600 in + 60 out per frame)
- [x] Drukowane w logu: `Estimated cost: ~$X.XX (N frames across M files)`

### Provider architecture
- [x] `providers/base.py` — `AIProvider` ABC z `verify()` i `describe()`
- [x] `providers/anthropic_provider.py` — Anthropic Claude
- [x] OpenAI provider — Whisper API fallback gdy brak lokalnego backendu
- [x] Łatwe dodawanie nowych providerów

### DevOps
- [x] `.gitignore` — `config.json` (klucz API!), `__pycache__`, `.DS_Store`, `venv/`, `.claude/`
- [x] `CHANGELOG.md`, `VERSION`, `SECURITY.md`
- [x] GitHub repo: `https://github.com/solarssk/video-describer`

---

## W toku / Niedokończone

### GitHub setup
Repo założone, ale kod trzeba wgrać ręcznie z bundla:
```bash
cd ~/Claude/video-describer
git init
git add -A
git commit -m "feat: initial commit — video-describer v0.2.0"
git remote add origin https://github.com/solarssk/video-describer.git
git push -u origin main
```
> ⚠️ Przed commitem sprawdź `git status` że `config.json` nie jest staged.

### UI_PLAN.md items (zaplanowane, nie zaimplementowane)
Z dokumentu `UI_PLAN.md` pozostały:
- [ ] **Tokeny per-plik inline** — `GX010123.MP4 · 1.2k tok · $0.012` na jednej linii z nazwą (Issue #2)
- [ ] **File list checkboxes** — deselect pojedynczych plików przed startem (Issue #3); backend `/start` przyjmuje opcjonalne `files: [...]`

---

## Planowane: v0.3.0

### Budget guard — zatrzymaj zanim skończą się kredyty
**Problem:** Plik 9/15 — 347s ekstrakcji klatek + 24s Whisper — zmarnowane gdy kredyty kończą się w trakcie.

**Propozycja:**
- Opcjonalne pole "Budget limit" w formularzu (USD)
- Pre-batch estimate już istnieje — porównaj `est_cost` z limitem przed startem
- Mid-batch: akumuluj rzeczywisty koszt per plik, sprawdzaj przed każdym nowym plikiem
- Jeśli `running_cost + est_cost_next_file > budget_limit` → zatrzymaj gracefully z informacją

### File selection checkboxes
- Checkboxy przy każdym pliku na liście before startu
- Backend `/start` przyjmuje `files: [...]` (lista nazw)
- `describe_videos.py` — `process_folder()` z `file_filter`

### Batch resume po przerwaniu
- Zapisuj stan batch w `batch_state.json` (gitignored, automatycznie usuwany po zakończeniu)
- Po restarcie aplikacji → "Resume previous batch? (9/15 plików, $0.43 zużyte)"
- Przydatne przy awarii prądu, przegrzaniu, błędzie sieci

### Windows / Linux support
Aktualnie: `IS_MACOS` guard w wielu miejscach, `VideoToolbox` hardcode, mlx-whisper tylko ARM.
- Wyabstrahować platform detection
- Testy na Windows/Linux CI
- Dokumentacja instalacji dla nie-macOS

---

## Planowane: v0.4.0+

### Więcej providerów AI
- **Google Gemini** — alternatywny model opisu wideo; scaffolding już w Connectors UI (badge "Wkrótce")
- **AssemblyAI** — cloud transcription z diarization (rozróżnianie mówców); scaffolding w Connectors UI

Implementacja: 2 metody w nowej klasie dziedziczącej `AIProvider`, rejestracja w `providers/__init__.py`, config block w `config.default.json`.

### Output formats
- JSON output obok `.txt` — machine-readable, z timestampami jako obiektami
- Opcja: jeden plik zbiorczy dla całego folderu zamiast per-plik
- Markdown output z linkami do klatek

### Edytor promptu — lepsza UX
- Diff między presetem a aktualnym promptem
- Preset lock / override warning
- Podgląd "jak będzie wyglądać output"

---

## Architektura — kluczowe decyzje

| Decyzja | Powód |
|---------|-------|
| Flask + SSE (nie WebSocket) | Prostota, brak zależności, działa przez zwykły HTTP |
| `config.json` gitignored | Zawiera prawdziwy klucz API — absolutnie nigdy nie commitować |
| Provider ABC pattern | Łatwa wymienialność modeli AI bez ruszania logiki przetwarzania |
| mlx-whisper jako primary | Apple Neural Engine — 5-10x szybszy od CPU na M-series |
| Tokeny per-plik w UI | User musi widzieć ile kosztuje każdy plik, nie tylko globalnie |
| VideoToolbox w ffmpeg | Odciąża CPU podczas batch — ważne przy 1TB materiału |

---

## Pliki kluczowe

```
video-describer/
├── describe_videos.py       — cała logika przetwarzania + CLI
├── web_app.py               — Flask server, SSE, /start /stop /status
├── config_loader.py         — ładowanie config.json z auto-migracją
├── config.default.json      — fabryczne ustawienia (w git)
├── config.json              — GITIGNORED — klucze API + user settings
├── providers/
│   ├── base.py              — AIProvider ABC
│   └── anthropic_provider.py— Claude + _clean_error_msg()
├── prompts/
│   ├── system.pl.default.md — preset PL
│   ├── system.en.default.md — preset EN
│   └── system.md            — aktywny prompt usera (gitignored)
├── templates/index.html     — cały frontend (single template)
└── static/
    ├── style.css            — CSS z media queries
    ├── app.js               — cała logika JS frontend
    └── i18n/pl.json, en.json— tłumaczenia UI
```

---

## Styl kodu

- Python 3.9+ (brak `match`, brak 3.10+ type hints)
- Brak zewnętrznych JS frameworków — vanilla JS + CSS variables
- CSS variables w `:root` dla theming; dark mode przez `prefers-color-scheme`
- i18n: `t('key.nested')` w JS, `{{placeholder}}` dla wartości dynamicznych
- SSE events: `type` + `text` + opcjonalnie `file`, `pct`, `tokens`, `cost`
