# UI/UX Fix Plan — v0.2.x

> Napisane przed implementacją. Każda sekcja ma: diagnozę, decyzję projektową, konkretne zmiany.

---

## 1. "Open" — nieczytelny przycisk na file card

**Diagnoza:** `.reveal-btn` ma `opacity: 0.55` i `background: none` — na ciemnym tle karty daje prawie niewidoczny tekst.

**Decyzja:** Styl przycisku ujednolicamy z `.btn-mini` (używanym w Settings). Pełna widoczność, border `var(--border)`, kolor `var(--text)`. Hover = `var(--border)` fill.

**Zmiany:**
- `style.css` — usuń `.reveal-btn`, zamień na alias `.btn-mini` (lub nadpisz styl na ten sam)
- `app.js` — zmień `class="reveal-btn"` → `class="btn-mini"` w `addFileCard()`

---

## 2. Tokeny/koszt — pozycja w UI

**Diagnoza:** `#usage-widget` jest w headerze, schowany do momentu startu. Małe, wąskie pole rozszerza header na bok. User chce widzieć koszt per-plik, a nie globalnie na górze.

**Decyzja:** 
- Usunąć `#usage-widget` (Tokens / Cost) z headera całkowicie.
- Koszt per-plik jest już renderowany w `addFileCard()` jako `.file-usage`. Przenieść go inline — na tej samej linii co nazwa pliku, oddzielony `·`:
  ```
  GX010123.MP4  ·  1.2k tok · $0.012
  ```
- Globalne podsumowanie (tokeny + koszt sesji) pokazywać w status-barze po zakończeniu, już jako część `status.finished_summary` (ew. dodać tam koszt).

**Zmiany:**
- `index.html` — usuń `#usage-widget` span z headera
- `style.css` — usuń min-width dla `#m-tokens`, `#m-cost`; dodaj `.file-card-name-row` (flex row z name + usage inline)
- `app.js` — w `addFileCard()` zmień layout: name + usage w jednym `<div class="file-card-name-row">`, usage inline (z `·` separatorem); usuń wywołania `updateUsage()` z headera lub zostaw tylko dla `finished_summary`

---

## 3. Lista plików — rozmiar + deselect

**Diagnoza:** 
- Rozmiary plików są w `.path-info-files li` jako `<span class="muted">`, ale są wyrównane do prawej przez `justify-content: space-between` — wygląda OK, ale przy długich nazwach pliku rozmiar uciekał daleko od nazwy.
- Nie ma możliwości odznaczenia pojedynczych plików przed startem.

**Decyzja:**
- Rozmiary — zostają po prawej stronie (jest to właściwe miejsce), ale dodajemy `min-width` dla kolumny rozmiaru żeby wyrównanie było stabilne.
- Deselect — dodajemy checkboxy przed każdym plikiem na liście `path-info-files`. Domyślnie wszystkie zaznaczone. Po odznaczeniu plik jest wyszarzony i nie będzie wysłany do backendu.
- Backend `/start` przyjmuje opcjonalne pole `files` (lista nazw plików). Jeśli `files` jest puste = wszystkie z folderu.

**Zmiany:**
- `app.js` — `onPathChange()`: podczas renderowania listy dodaj `<input type="checkbox" checked>` przed każdą nazwą; odczytuj zaznaczone pliki w `startProcessing()` i wysyłaj jako `files: [...]`
- `style.css` — `.path-info-files li` dostaje `gap: 8px` i stałą kolumnę rozmiaru; checkbox w liście
- `web_app.py` — `/start` handler: czyta `data.get('files', [])` i przekazuje do `describe_videos`
- `describe_videos.py` — `process_folder()` przyjmuje opcjonalny `file_filter` (lista nazw)

---

## 4. Locked state — mylące przyciski

**Diagnoza:** Podczas przetwarzania formularz jest `disabled`, ale:
- Przyciski pick (`📁 Folder`, `🎬 Plik`) wizualnie wyglądają klikalnie (brak `cursor: not-allowed`)
- `.btn-pick:disabled` ma `pointer-events: none` w CSS — ale `disabled` nie jest ustawiane na wszystkich przyciskach

**Decyzja:** Dodać klasę `locked` na `<body>` lub `.panel-left` podczas przetwarzania. CSS dla `.locked *:not(.btn-stop)` blokuje interakcję + zmienia kursor. Nie polegamy na HTML `disabled` (który musi być ustawiany ręcznie na każdym elemencie).

**Zmiany:**
- `style.css` — dodaj blok:
  ```css
  .panel-left.locked { pointer-events: none; opacity: 0.6; }
  .panel-left.locked .form-actions { pointer-events: auto; opacity: 1; }
  ```
- `app.js` — `setLocked(true/false)` dodaje/usuwa klasę `.locked` z `.panel-left`; wywołać przy start/stop/done

---

## 5. Connectors — spójność z Settings

**Diagnoza:** Settings używa `section { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); }` z `max-width: 900px` centrowanym. Connectors używa `conn-card` z `background: var(--panel)` (CSS variable która nie jest zdefiniowana → `undefined`), inny padding, inny max-width.

**Decyzja:** Przepisać Connectors tak, żeby używał **dokładnie tej samej struktury co Settings** — `section` + `h2` + `field`/row layout. Karty providerów stają się sekcjami Settings-style. Zachowujemy ikony providerów, badge "Connected", input + przyciski Verify/Save — ale w Settings-consistent shell.

Layout per karta:
```
┌─ section ──────────────────────────────────────┐
│  [icon] Anthropic                  [Connected] │  ← h2-like row z ikoną i badge
│  Claude · AI description of video & photos     │  ← subtitle (muted)
│  ─────────────────────────────────────────────  │
│  API Key  [•••••••••••••••••••] [Verify] [Save]│
│  ✓ Connected (claude-sonnet-4-6)               │
└────────────────────────────────────────────────┘
```

**Zmiany:**
- `index.html` — przepisać `pane-connectors` używając `<div class="settings-scroll"><div class="settings">` jako wrapper, każda karta = `<section>`
- `style.css` — usunąć `.conn-card`, `.conn-card-header`, `.conn-card-body`, `.connectors-scroll`, `.connectors-header`; dodać style wspólne z Settings (`.conn-header-row` do wyrównania ikony + nazwy + badge w sekcji); `.conn-icon` zostaje (ikony kolorowe)
- `app.js` — zmienić selektory (ID pozostają, tylko parent structure się zmienia)

---

## 6. Header — brak zawijania (flex-wrap: nowrap)

**Diagnoza:** `header { flex-wrap: wrap; }` — przy zwężaniu okna metrics bar spada do drugiej linii, co zwiększa wysokość headera.

**Decyzja:** Zmienić na `flex-wrap: nowrap`. Metryki są już progressive-hidden: Whisper znika przy <1150px, Usage widget przy <980px. Poniżej 900px jest narrow warning. Między 900-980px mamy tylko CPU/RAM/Load + thermal dot — te się mieszczą. Dodać jeszcze jeden breakpoint: przy <920px ukryć też CPU/RAM/Load (zostaje tylko thermal dot jako wskaźnik), żeby nagłówek się nie ścisnął.

**Zmiany:**
- `style.css`:
  ```css
  header { flex-wrap: nowrap; }  /* było: flex-wrap: wrap */
  @media (max-width: 920px) {
    .metric:not(.thermal) { display: none !important; }
  }
  ```

---

## 7. Narrow bypass — proporcje log vs file-cards

**Diagnoza:** W `bypass-narrow` (okno <900px, użytkownik kliknął "Pokaż mimo to"), `.file-cards { max-height: 35vh }` jest za duże gdy log ma mało miejsca, a `.log` elastycznie dostaje resztę — ale jeśli jest dużo kart, log spada do <20vh.

**Decyzja:** W `bypass-narrow` zmniejszyć `max-height` file-cards do `25vh`, a log dostaje `min-height: 30vh`. Na bardzo wąskich (<600px) — file-cards spada do `20vh`.

**Zmiany:**
- `style.css`:
  ```css
  body.bypass-narrow .file-cards { max-height: 25vh; }
  body.bypass-narrow .log { min-height: 30vh; }
  @media (max-width: 600px) {
    body.bypass-narrow .file-cards { max-height: 20vh; }
  }
  ```

---

## 8. Bardzo wąski — tabs zawijają pod logo

**Diagnoza:** Przy <700px (bypass mode lub po prostu zwężonym oknie) `.tabs { margin-left: 16px }` z trzema tabami nie mieści się obok "🎬 Video Describer". Teraz header wrappuje — ale po zmianie na `nowrap` (punkt 6) tabs po prostu się utną.

**Decyzja:** Przy bardzo wąskich oknach (bypass mode) skrócić labele tabów do samych emoji: `🎬` / `⚙️` / `🔌`. Zaimplementować przez CSS content override lub JS — CSS jest czystsze.

```css
@media (max-width: 720px) {
  body.bypass-narrow .tab { font-size: 0; padding: 6px 8px; }
  body.bypass-narrow .tab::before { font-size: 16px; }
  /* Każda zakładka dostaje swój emoji przez data-tab attribute */
  body.bypass-narrow .tab[data-tab="run"]::before       { content: "🎬"; }
  body.bypass-narrow .tab[data-tab="settings"]::before  { content: "⚙️"; }
  body.bypass-narrow .tab[data-tab="connectors"]::before { content: "🔌"; }
}
```

**Zmiany:**
- `style.css` — dodać powyższy blok media query

---

## Priorytet i kolejność implementacji

| # | Issue | Ryzyko | Rozmiar |
|---|-------|--------|---------|
| 6 | Header flex-wrap → nowrap | niskie | małe |
| 4 | Locked state — klasa `.locked` | niskie | małe |
| 1 | "Open" button → btn-mini | niskie | małe |
| 8 | Tabs emoji-only na wąskim | niskie | małe |
| 7 | Narrow bypass proporcje | niskie | małe |
| 5 | Connectors → Settings style | średnie | średnie |
| 2 | Tokeny per-plik inline | średnie | średnie |
| 3 | File list checkboxes + deselect | wysokie | duże |

**Kolejność:** 6 → 4 → 1 → 8 → 7 (CSS-only, szybkie) → 5 (HTML+CSS) → 2 (JS+CSS) → 3 (JS+CSS+Python)

---

## Pliki dotknięte

- `static/style.css` — wszystkie poprawki CSS
- `static/app.js` — locked state, file card layout, path-info checkboxes
- `templates/index.html` — Connectors pane rewrite, usunięcie usage-widget
- `web_app.py` — `/start` przyjmuje `files` filter
- `describe_videos.py` — `process_folder()` file_filter
- `static/i18n/en.json` + `pl.json` — ewentualne nowe klucze
