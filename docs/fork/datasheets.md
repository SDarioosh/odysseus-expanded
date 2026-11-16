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
    value gives its words; otherwise the PDF line it was written beside ("near").
  Tapping a mark scrolls to it and flashes its box. The panel refreshes after each save.
- **Search marks** in the Write list (shown once there is a datasheet): every word typed
  must appear in a mark's text, its "near" line, the note title or the PDF's name.
  Results open the datasheet at that mark.
- **Links.** `/#open=write&note=<id>&page=<page id>&mark=<mark id>` opens Write at a mark
  (for chat answers and other notes later).
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

### Routes

| Route | What |
|---|---|
| `POST /api/ink/notes/{id}/pdf` | multipart `pdf` (+ `title`, `edited_at`, `paper`): a new datasheet note |
| `GET /api/ink/notes/{id}/pdf` | the original PDF (download) |
| `GET /api/ink/notes/{id}/pdf/{page_id}.png?w=` | a PDF page at the tier for `w` |
| `GET /api/ink/notes/{id}/marks` | `{note_id, title, marks: [...]}` |
| `GET /api/ink/marks?q=&limit=` | `{q, results: [{note_id, title, pdf_name, mark}]}` |

A mark: `{id, page_id, page_no, kind: highlight|underline|ink, color, box: [x, y, w, h],
text, near, strokes, at}`.

### Write view (`static/js/forkInk.js`, `static/js/forkInkPdf.js`)

`forkInkPdf.js` holds the pure parts (tier choice, mixed page heights, which pages load
and which keep their image, the image cache with a 40 MP budget of `ImageBitmap`s, mark
grouping for the panel, the deep link). The PDF background is drawn with the paper in the
cached base layer, never per pen sample; an image that arrives while the pen is down is
drawn when it lifts.

## Stages

1. **PDF pages in Write**: built (above).
2. **Marks**: built: highlights, underlines and ink notes with their PDF text, the Marks
   panel, search, deep links. Next: each handwritten note (ink mark) read on its own by
   the local vision model, so a mark carries what was written, not only the line it is
   near.
3. **Finding them from chat**: a read-only brain tool over the marks ("what did I note
   about the ZX-100's enable pin?"), answers linking to the mark.
4. **Later**: export a copy with the marks as real PDF annotations; part-number tags;
   box and arrow marks; links between marks; open a PDF from the Library in Write.

## Tests

`tests/fork/test_ink_pdf.py` (import, limits, rotation, render and words, cache, saves,
transcription, Athena), `test_ink_pdf_routes.py`, `test_ink_pdf_marks.py` (grouping),
`test_ink_marks_routes.py`, `test_ink_pdf_view.py` (node and a Playwright run of the view
with a fake hub). Fictional PDFs: `tests/fork/pdf_fixtures.py`.

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
6. Offline (Wi-Fi off): ink on loaded pages still saves; a page not loaded says so;
   back online, it loads.

## Files

- `fork/ink/pdf.py`: PDF checks, rendering, words (pypdfium2).
- `fork/ink/pdf_marks.py`: strokes to marks, marks of a note, search.
- `fork/routes/ink_marks_routes.py`: the marks routes.
- `fork/ink/notes.py`, `fork/ink/transcribe.py`, `fork/ink/athena.py`,
  `fork/routes/ink_routes.py`: PDF pages in the sidecar, import and page routes, ink-only
  transcription, no Athena.
- `static/js/forkInkPdf.js`; `static/js/forkInk.js`, `static/js/forkInkLinks.js`,
  `static/js/forkModuleWindows.js` (one line), `static/css/fork-ink.css`.
- Tests: `tests/fork/test_ink_pdf.py`, `test_ink_pdf_routes.py`, `test_ink_pdf_marks.py`,
  `test_ink_marks_routes.py`, `test_ink_pdf_view.py`, `pdf_fixtures.py`.
