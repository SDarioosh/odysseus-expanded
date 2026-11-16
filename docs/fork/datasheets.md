# Datasheets: PDFs in Write, marked with the pen

Component datasheets (or any PDF) opened in the Write view and marked with the same
pens as a handwritten note: highlight a value, underline a line, write in the margin.
Each mark knows the PDF text it covers, so it can be found again later: the Marks panel
of a datasheet, and a search over every datasheet's marks.

Built on [handwritten notes](handwritten-notes.md): a datasheet is one handwritten note
whose pages are **PDF pages**, so the pens, palm rejection, eraser, undo, autosave,
offline sync and notebooks all work on it unchanged.

## Privacy

As for handwritten notes: the PDF, its rendered pages, the ink and the marks stay in the
hub (`<DATA_DIR>/ink/<owner>/`). PDF text comes from the file itself (pypdfium2, local);
no OCR service. **Datasheets are never sent to Athena** (they are reference material,
not something the owner said or wrote): Send to Athena is hidden for them, the hub
refuses it ("Datasheets stay in Odysseus") and `INK_AUTO_SEND` skips them.

## What the owner sees

- **Import PDF** in the Write list (up to 100 MB, 500 pages; password-protected PDFs are
  refused). The note takes the file's name, has a **PDF** badge and "PDF, N pages:
  name". Import needs the hub (never queued offline).
- **Writing on it.** Every pen and the highlighter work on a PDF page (a photo page takes
  only the highlighter). The highlighter is drawn under the ink, so the print stays
  readable. A PDF page is as tall as its PDF page (Letter, A4, landscape); **Page**
  inserts a blank page for notes after the page shown.
- **Sharp at any zoom.** The hub renders each page at the width the screen needs (tiers
  600, 1200, 2000, 3000 px for the page's on-screen width times the pixel ratio); a
  sharper tier replaces a softer one after a pinch. Only the pages on screen (and one
  either side) are loaded; the rest are freed, so a 300-page PDF stays light.
- **Marks** (button in a datasheet): every mark, by page, with what it covers:
  - *Highlight*: the PDF words under the highlighter.
  - *Underline*: a nearly straight, nearly level pen line under text: the words of the
    line just above it.
  - *Note*: other ink (handwriting, arrows, circles), grouped by closeness. A circled
    value gives its words; otherwise the PDF line it was written beside ("near"). **What
    was written** is read by the local vision model (see "Reading notes"); the panel
    shows it ("check EN pull-down", by "Input voltage range…"), "Reading…" while the
    model works, "Could not read it" with the reason, or "a drawing (no words)".
    **Edit** corrects a reading (kept until the note's strokes change).
  Tapping a mark scrolls to it and flashes its box. The panel refreshes after each save,
  and every 4 s while a note is being read.
- **Search marks** in the Write list (shown once there is a datasheet): every word typed
  must appear in a mark's text, what was written on it, its "near" line, the note title
  or the PDF's name.
  Results open the datasheet at that mark.
- **Links.** `/#open=write&note=<id>&page=<page id>&mark=<mark id>` opens Write at a mark
  (chat answers link to marks this way; other notes later).
- **Asking in chat.** "What did I note about the ZX-100's enable pin?": the brain looks
  through the marks (`search_datasheet_marks`, [brain.md](brain.md#datasheet-marks-2026-11-16))
  and quotes what you marked or wrote, with a link to each mark.
- Handwriting on a PDF page can still be transcribed (Text tab): the hub reads the ink
  only, without the PDF under it.

## How it works

### Storage (`fork/ink/notes.py`)

The sidecar of a datasheet has, besides the usual fields:

```
"pdf": {"file": "pdfs/<note_id>.pdf", "hash", "name", "pages", "at"}    # the note
pages[i].pdf = {"page": <1-based>, "width_pt", "height_pt"}              # a PDF page
```

The original file is `<ink folder>/pdfs/<note_id>.pdf` and never changes. Like a photo
page's `photo`, a page's `pdf` is set only by the import and kept by page id on every
save; a save must keep a PDF page's height (`round(1000 * h / w)`, 250 to 4000) and every
other page at 1000 x 1414. PDF pages count toward `MAX_PDF_PAGES` (500), other pages
toward `MAX_PAGES` (60). The file, and the rendered pages, go with the note (delete and
sweep, as for photos).

### Rendering (`fork/ink/pdf.py`)

pypdfium2 (already a requirement; Apache/BSD), every call under one lock (pdfium is not
thread-safe). Pages are rendered as displayed (rotation applied) into a cache
`<ink folder>/pdfs/cache/<note_id>/<page id>-<width>.png`, at most `MAX_RENDER_PIXELS`
(16 MP, so a very tall page is rendered a little narrower) and at most 300 MB per note
(least recently used dropped). A page image is served with `immutable` caching: a page
id is one page of one stored PDF.

### Marks (`fork/ink/pdf_marks.py`)

Marks are computed from the strokes, not stored: grouping is a pure function of a page's
strokes and the PDF's words (`pdf.page_words`: words with boxes in page units, by line).
They are cached in memory per page by the strokes' hash, so only a changed page is
grouped again. Thresholds are constants at the top of the file (page units, a page is
1000 wide).

### Reading notes (`fork/ink/mark_reading.py`)

Each *Note* mark (kind `ink`) on a PDF page is read on its own, so a reading belongs to
one margin note, not to the whole page:

- **What the model sees.** Only the mark's pen strokes (never the PDF, never the
  highlighter), cut out with 16 units around them and enlarged up to 4x (pen width too)
  on a page of their own, through H2's reader (`transcribe.read_page`: the hub's
  `vision_model`, local endpoints only, one image at a time across the hub, chat first).
  The question adds that a shape alone (circle, arrow, line, tick) is no words.
- **When.** A save of a datasheet schedules a run for that note (debounced); so does
  opening the Marks panel when something is unread. A note is read once it is
  **settled**: its last stroke is 20 s old, or the note hasn't been saved for 20 s (a
  tablet clock ahead of the hub's). One run per note at a time; marks are read one by
  one and each result is stored as it comes.
- **Stored** in the sidecar, `"mark_readings": {<mark id>: {"hash", "text", "status",
  "model", "at", "error"?}}`, status `read`, `failed` or `reviewed` ("pending" is only
  in memory while the model reads, so a restart leaves nothing stuck). `hash` is the
  mark's strokes: when they change, the mark is read again (a correction too). Readings
  of marks that are gone are dropped at the next run. A failed reading is retried at most
  every 10 minutes ("No vision model is set" when there is none).
- **Correcting** (`PUT …/marks/{mark id}` `{"written"}`, at most 2000 characters) stores
  the owner's text as `reviewed`; only *Note* marks have writing.

Every mark in an answer carries `written` and `reading` (`none` for highlights and
underlines, `waiting`, `pending`, `read`, `failed` with `reading_error`, `reviewed`);
`GET …/marks` also says `reading: true` while a run for the note is waiting or working.

### Routes

| Route | What |
|---|---|
| `POST /api/ink/notes/{id}/pdf` | multipart `pdf` (+ `title`, `edited_at`, `paper`): a new datasheet note |
| `GET /api/ink/notes/{id}/pdf` | the original PDF (download) |
| `GET /api/ink/notes/{id}/pdf/{page_id}.png?w=` | a PDF page at the tier for `w` |
| `GET /api/ink/notes/{id}/marks` | `{note_id, title, reading, marks: [...]}` |
| `PUT /api/ink/notes/{id}/marks/{mark_id}` | `{"written"}`: correct what a note says; answers the mark |
| `GET /api/ink/marks?q=&limit=` | `{q, results: [{note_id, title, pdf_name, mark}]}` |

A mark: `{id, page_id, page_no, kind: highlight|underline|ink, color, box: [x, y, w, h],
text, near, written, reading, strokes, at}` (+ `reading_error` when failed).

### Write view (`static/js/forkInk.js`, `static/js/forkInkPdf.js`)

`forkInkPdf.js` holds the pure parts (tier choice, mixed page heights, which pages load
and which keep their image, the image cache with a 40 MP budget of `ImageBitmap`s, mark
grouping for the panel, the deep link). The PDF background is drawn with the paper in the
cached base layer, never per pen sample; an image that arrives while the pen is down is
drawn when it lifts.

## Stages

1. **PDF pages in Write**: built (above).
2. **Marks**: built: highlights, underlines and ink notes with their PDF text, the Marks
   panel, search, deep links, and each handwritten note read on its own by the local
   vision model, correctable by hand.
3. **Finding them from chat**: built: the brain's read-only `search_datasheet_marks`
   (`fork/brain/datasheets.py`) ranks the owner's marks for a loose query
   (`pdf_marks.rank`: words found in the mark's text, what was written and its near line,
   plus the datasheet's title or PDF name, so naming a part lists its datasheet's marks)
   and answers each with its datasheet, page, kind, text, what was written (corrected or
   not), the line beside it and a link that opens Write at it. Owner-only, not for API
   tokens or non-admin users, refused by voice for a voice that isn't the owner's.
4. **Later**: export a copy with the marks as real PDF annotations; part-number tags;
   box and arrow marks; links between marks; open a PDF from the Library in Write.

## Tests

`tests/fork/test_ink_pdf.py` (import, limits, rotation, render and words, cache, saves,
transcription, Athena), `test_ink_pdf_routes.py`, `test_ink_pdf_marks.py` (grouping),
`test_ink_marks_routes.py`, `test_ink_mark_reading.py` (the cut-out page, settling,
runs with a fake reader, corrections), `test_ink_pdf_view.py` (node and a Playwright run of the view
with a fake hub), `test_brain_datasheet_marks.py` (the ranked search and the brain's tool:
scoring, owner isolation, who gets it, the gate armed, voice). Fictional PDFs:
`tests/fork/pdf_fixtures.py`.

## Server verification (owner)

On the server (read-only checks) and the tablet:

1. `docker exec <odysseus container> python -c "import pypdfium2; print('ok')"` prints
   `ok` (pypdfium2 is in `requirements.txt`; no new dependency).
2. On the Lenovo Tab Plus, in Write: **Import PDF** with a real datasheet (50+ pages).
   Expected: the note opens within a few seconds; pages appear as you scroll; zoomed to
   4x the small print is sharp after a moment.
3. Write and highlight on a PDF page: no lag compared with a blank page; palm rejection
   as usual, also near the Marks panel.
4. Highlight a value, underline a line, write a note in the margin; open **Marks**:
   the highlight and underline show the right words, the margin note "near" its line.
   Tap one: the view scrolls there and flashes it.
5. In the list, **Search marks** for a word you highlighted: the result opens at it.
6. Write a short margin note ("check EN") and wait about 30 s with Marks open: it shows
   "Reading…", then what you wrote (needs Settings, Vision set to the local vision
   model). Edit it, then search for the corrected word.
7. Offline (Wi-Fi off): ink on loaded pages still saves; a page not loaded says so;
   back online, it loads.
8. In a chat, ask about a part and a pin you marked ("what did I note about the
   <part>'s enable pin?"). Expected: the answer quotes what you highlighted or wrote and
   links each mark; the link opens Write at the mark and flashes it.

## Files

- `fork/ink/pdf.py`: PDF checks, rendering, words (pypdfium2).
- `fork/ink/pdf_marks.py`: strokes to marks, marks of a note, search, ranked search.
- `fork/brain/datasheets.py`: the brain's `search_datasheet_marks`; hooks in
  `fork/brain/toolset.py`, `tools.py`, `prompt.py`, `settings.py`, `fork/voice/policy.py`.
- `fork/ink/mark_reading.py`: reading each handwritten note, corrections.
- `fork/routes/ink_marks_routes.py`: the marks routes.
- `fork/ink/notes.py`, `fork/ink/transcribe.py`, `fork/ink/athena.py`,
  `fork/routes/ink_routes.py`: PDF pages in the sidecar, import and page routes, ink-only
  transcription, no Athena.
- `static/js/forkInkPdf.js`; `static/js/forkInk.js`, `static/js/forkInkLinks.js`,
  `static/js/forkModuleWindows.js` (one line), `static/css/fork-ink.css`.
- Tests: `tests/fork/test_ink_pdf.py`, `test_ink_pdf_routes.py`, `test_ink_pdf_marks.py`,
  `test_ink_marks_routes.py`, `test_ink_mark_reading.py`, `test_ink_pdf_view.py`,
  `test_brain_datasheet_marks.py`, `pdf_fixtures.py`.
