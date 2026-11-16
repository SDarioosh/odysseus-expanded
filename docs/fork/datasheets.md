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
  name". Import needs the hub (never queued offline). **From Library** next to it lists
  the PDFs in the Library and copies the one picked into a new datasheet (the Library
  document stays as it is).
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
  - *Circled*: a closed loop, or a box drawn in up to 4 strokes, round PDF words: the
    words inside it. Not read by the model.
  - *Arrow*: an arrow drawn on its own (a straight shaft with a head, in one stroke or
    with 1-2 short head strokes): the PDF line at its head. Not read by the model.
  - *Note*: other ink (handwriting), grouped by closeness. A value circled together with
    the writing gives its words; otherwise the PDF line it was written beside ("near"),
    or, when an arrow is drawn with it, the line the arrow points to. **What
    was written** is read by the local vision model (see "Reading notes"; an arrow drawn
    with it isn't sent); the panel
    shows it ("check EN pull-down", by "Input voltage range…"), "Reading…" while the
    model works, "Could not read it" with the reason, or "a drawing (no words)".
    **Edit** corrects a reading (kept until the note's strokes change).
  Tapping a mark scrolls to it and flashes its box. The panel refreshes after each save,
  and every 4 s while a note is being read. At its top, **Download with marks** (a copy
  with the marks as PDF annotations; unsaved writing is saved first) and **Original PDF**.
- **Search marks** in the Write list (shown once there is a datasheet): every word typed
  must appear in a mark's text, what was written on it, its "near" line, the note title,
  its part numbers or the PDF's name.
  Results open the datasheet at that mark.
- **Part numbers.** A datasheet carries the part numbers it is about (tags, at most 20).
  Import suggests them from the file name and the first page's largest line (its title):
  tokens mixing letters and digits such as ZX-100, TPS62130, LM317T. They show as chips
  beside the title (**Add part no.**, Enter; each chip has a remove button) and on the
  list cards; search and chat find a datasheet by them like by its title.
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
"parts": ["ZX-100", ...]                                                 # part numbers
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

Pen strokes close together (30 units) are one group; in a group each stroke is a
**circle**, an **arrow** or **writing** (the full rules are in the module docstring):

- *Circle*: one stroke whose start and end meet (at most 0.3 x its larger side apart,
  its path at least twice that side, at least 10 x 10) with a word centre inside the
  loop; or a box of 1-4 strokes (each at least 20 long): 90 % of their points on the
  box's border, each side drawn along 70 % of its length, a word centre inside. A box's
  top side under a line is no underline.
- *Arrow*: a nearly straight shaft (at least 30 long, path at most 1.2 x that) with a
  head, drawn back from its far end in the same stroke or as 1-2 short strokes touching
  one end (within 10). The head opens backwards: within 75° of the shaft, reaching back
  4 units to 0.6 x the shaft, opening sideways at least a quarter of that. It points to
  the PDF line nearest its tip, within 40. An arrow's shaft under a line is no underline.
- *Writing*: everything else.

A group with writing stays kind `ink` (its id, `m-<first stroke>`, unchanged): with an
arrow, `near` is the line it points to and `points_to` is true, and its text is taken
from the writing's box, not the arrow's. Without writing, a group with a circle is kind
`circle` (`text` the circled words; `near` the line an arrow drawn with it points to,
`points_to`), and a group of arrows only is kind `arrow` (`text` the line(s) at its
head). Every mark has `points_to` (false unless `near` came from an arrow).

### Part numbers (`fork/ink/parts.py`)

A datasheet's sidecar may have `"parts"`: at most 20 tags, each 1-40 characters
(trimmed, inner spaces collapsed), unique case-insensitively (the first spelling kept), in
the owner's order. A save or a photo upload keeps them; the note (`GET …`, every answer
of a save) and each entry of the list (`GET /api/ink/notes`) carry `parts` (`[]` when
none). `PUT …/parts` replaces them (only on a datasheet). On import (`add_pdf`) they are
suggested (at most 5): tokens of the first page's largest-font line (its tallest word
box), then of the file name, that look like part numbers: 3-20 letters and digits (inner
`-./_` allowed) with a letter and at least two digits, not a value with a unit (5.5V,
18uA, 16-bit), 4K7/3V3, a size (3x3mm), a version (rev2, v1.2), a package (SOT-23,
QFN32), a rating (IP67, AEC-Q100), nor a word of 5+ lowercase letters (datasheet2). A
file name's words lose whole-word ends (`zx100-datasheet` → ZX100), a lowercase one is
upper-cased, and a guess equal to an earlier one without its separators is dropped. The
search over marks and `pdf_marks.rank` (the brain) count parts like the title; the
brain's result names them in brackets: `<title> [ZX-100] (file), page N, …`.

### Reading notes (`fork/ink/mark_reading.py`)

Each *Note* mark (kind `ink`) on a PDF page is read on its own, so a reading belongs to
one margin note, not to the whole page:

- **What the model sees.** Only the mark's writing pen strokes (never the PDF, never the
  highlighter, never an arrow drawn with it: `mark_strokes` takes the mark's internal
  `_writing`; adding an arrow to a note that was read doesn't read it again), cut out with 16 units around them and enlarged up to 4x (pen width too)
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

Every mark in an answer carries `written` and `reading` (`none` for highlights,
underlines, circles and arrows, `waiting`, `pending`, `read`, `failed` with `reading_error`, `reviewed`);
`GET …/marks` also says `reading: true` while a run for the note is waiting or working.

### Export with marks (`fork/ink/pdf_export.py`)

`GET …/pdf?marks=1` answers a copy of the datasheet with the marks as real PDF
annotations, so any PDF reader shows them; the stored file is never changed. The copy is
built on each request, in a thread, with pypdfium2's raw pdfium API under the one pdfium
lock (a big PDF holds the lock for a moment, so page images wait), and is not cached.

- **Pages** follow the note: PDF pages in the note's order (a page the owner removed is
  left out, a reordered one moved), and each **blank page** the owner added is inserted
  where it is, as large as the PDF page before it (the one after it when it comes first;
  A4 when no PDF page is left), its ink scaled to the page's width (or height) at the top.
  **Photo pages are left out.**
- **Highlight** marks: a Highlight annotation with QuadPoints over the words it covers
  (one quad per run of words on a line; on a page whose text runs down the page, one per
  word), in the highlighter's colour, `/Contents` the covered text. One that covers no
  words is a see-through Ink annotation (opacity 35 %) of its strokes.
- **Every other mark** is an Ink annotation of its strokes (one per pen colour and width,
  the width in points), `/Contents` what was written when it was read or corrected
  (`reading` `read` or `reviewed`); for an underline (and any other kind with text, such
  as circled words or the line an arrow points to) the text it covers. An ink mark not
  read yet has no `/Contents`. Strokes in no mark (on blank pages) are Ink annotations,
  one per pen, the highlighter see-through.
- Page units map back to PDF user space through the page's crop box and `/Rotate` (the
  inverse of what `pdf.page_words` does), so marks land where they were drawn on rotated
  pages too.
- No appearance streams are written: the reader draws Ink and Highlight annotations from
  their paths and quads. pdfium (Chrome's viewer) does, as the tests check; other readers
  (Acrobat, a tablet's viewer) are in the server verification.
- Named `<file name without .pdf> (marked).pdf`, an attachment, `Cache-Control:
  private, no-store`.

### From the Library (`fork/ink/library.py`)

A PDF already in the Library (upstream's documents: imported with **Import PDF** there,
kept as a document with a hidden `pdf_source` marker naming its upload) can be opened in
Write without downloading it first. `library.py` is the only ink file that calls upstream
document or upload code, and checks owners exactly as upstream does: the list like
`GET /api/documents/library?language=pdf` (active, not archived, the owner's), opening
like upstream's own PDF routes (`_verify_doc_owner`, then the upload resolved for the
caller). The PDF is copied into a new datasheet note through `add_pdf` (same limits, 409
when the note id exists, title the document's title unless one is sent). The Library
document is not changed and not linked to the note.

### Routes

| Route | What |
|---|---|
| `POST /api/ink/notes/{id}/pdf` | multipart `pdf` (+ `title`, `edited_at`, `paper`): a new datasheet note |
| `GET /api/ink/notes/{id}/pdf` | the original PDF (download) |
| `GET /api/ink/notes/{id}/pdf?marks=1` | a copy with the marks as PDF annotations (download, `<name> (marked).pdf`) |
| `POST /api/ink/notes/{id}/pdf` (JSON) | `{document_id, edited_at, title?, paper?}`: a new datasheet note from a Library PDF; 404 not the owner's PDF document or its file gone |
| `GET /api/ink/library-pdfs` | `{documents: [{id, title, updated_at}]}`: the owner's Library PDFs, newest first |
| `GET /api/ink/notes/{id}/pdf/{page_id}.png?w=` | a PDF page at the tier for `w` |
| `GET /api/ink/notes/{id}/marks` | `{note_id, title, reading, marks: [...]}` |
| `PUT /api/ink/notes/{id}/marks/{mark_id}` | `{"written"}`: correct what a note says; answers the mark |
| `PUT /api/ink/notes/{id}/parts` | `{"parts": [...]}`: the datasheet's part numbers; answers `{"parts"}` as stored (400 bad or not a datasheet) |
| `GET /api/ink/marks?q=&limit=` | `{q, results: [{note_id, title, pdf_name, parts, mark}]}` |

A mark: `{id, page_id, page_no, kind: highlight|underline|ink|circle|arrow, color,
box: [x, y, w, h], text, near, points_to, written, reading, strokes, at}` (+
`reading_error` when failed).

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
4. **More marks and ways in**: export with marks (Ink and Highlight annotations, notes'
   text as their contents), open a PDF from the Library in Write, part-number tags
   (guessed on import, set, searched, in chat answers) and circled and arrow marks
   (see their sections). Still later: links between marks.

## Tests

`tests/fork/test_ink_pdf.py` (import, limits, rotation, render and words, cache, saves,
transcription, Athena), `test_ink_pdf_routes.py`, `test_ink_pdf_marks.py` (grouping,
circles, boxes and arrows), `test_ink_parts.py` (part numbers: the stored set, the
guesser), `test_ink_marks_routes.py` (also arrows on a real page, parts guessed, kept,
set and searched), `test_ink_mark_reading.py` (the cut-out page, settling,
runs with a fake reader, corrections, arrows not read), `test_ink_pdf_view.py` (node and a Playwright run of the view
with a fake hub), `test_brain_datasheet_marks.py` (the ranked search and the brain's tool:
scoring, owner isolation, who gets it, the gate armed, voice), `test_ink_pdf_export.py`
(the exported copy reopened with pypdfium2: pages, annotation types, `/Contents`, paths
and quads, rotation, removed and reordered pages, blank pages, rendered pixels),
`test_ink_library.py` (the Library adapter's contract test: documents made by upstream's
own import route, owners, the JSON import). Fictional PDFs:
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
9. Download a marked datasheet with marks (`/api/ink/notes/<id>/pdf?marks=1` until the
   button exists) and open it in Acrobat (or another desktop reader) and on the tablet's
   PDF viewer. Expected: highlights over the right words, pen ink where it was drawn
   (also on a rotated or landscape page), blank pages where they were added; hovering or
   tapping a highlight shows its words, a margin note shows what was written.
10. Import a PDF into the Library (Documents), then open it in Write from the Library
    (`POST /api/ink/notes/<new id>/pdf` with `{"document_id"}` until the button exists).
    Expected: a datasheet note with its pages; the Library document is unchanged.
11. Circle a value with one loop, draw a box round another, and draw an arrow from the
    margin to a line, alone and next to a short note; open **Marks** (or
    `/api/ink/notes/<id>/marks` until the labels exist). Expected: kinds `circle` with
    the circled words, `arrow` with the line at its head, and the note `ink` with
    `near` the line the arrow points to (`points_to` true); the note's reading shows its
    words only. Report any arrow or circle taken as plain ink (or the reverse).
12. Import a real datasheet whose file name or title has its part number:
    `GET /api/ink/notes/<id>` answers `parts` with it (and no units or package names).
    In a chat, ask about that part number by name: the answer lists the datasheet's marks
    and its first line shows `[<part>]`.

## Files

- `fork/ink/pdf.py`: PDF checks, rendering, words (pypdfium2).
- `fork/ink/pdf_marks.py`: strokes to marks (circles and arrows too), marks of a note,
  search, ranked search.
- `fork/ink/parts.py`: part-number tags (the stored set, the guesser on import).
- `fork/brain/datasheets.py`: the brain's `search_datasheet_marks`; hooks in
  `fork/brain/toolset.py`, `tools.py`, `prompt.py`, `settings.py`, `fork/voice/policy.py`.
- `fork/ink/mark_reading.py`: reading each handwritten note, corrections.
- `fork/routes/ink_marks_routes.py`: the marks routes.
- `fork/ink/pdf_export.py`: the copy with the marks as PDF annotations.
- `fork/ink/library.py`: the adapter to upstream's Library (documents, uploads).
- `fork/ink/notes.py`, `fork/ink/transcribe.py`, `fork/ink/athena.py`,
  `fork/routes/ink_routes.py`: PDF pages in the sidecar, import and page routes, ink-only
  transcription, no Athena.
- `static/js/forkInkPdf.js`; `static/js/forkInk.js`, `static/js/forkInkLinks.js`,
  `static/js/forkModuleWindows.js` (one line), `static/css/fork-ink.css`.
- Tests: `tests/fork/test_ink_pdf.py`, `test_ink_pdf_routes.py`, `test_ink_pdf_marks.py`,
  `test_ink_marks_routes.py`, `test_ink_mark_reading.py`, `test_ink_pdf_view.py`,
  `test_brain_datasheet_marks.py`, `test_ink_pdf_export.py`, `test_ink_library.py`,
  `pdf_fixtures.py`.
