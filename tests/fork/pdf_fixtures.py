"""Fork: tiny fictional PDFs for the datasheet tests (no PDF library needed to make them).

``make_pdf(pages)`` returns the bytes of a valid PDF. Each page is
``{"size": (w_pt, h_pt), "lines": [(x_pt, y_pt_from_top, font_pt, "text"), ...],
"rotate": 0}``; the text is Helvetica, so pypdfium2 extracts it with real character boxes.
Only fictional text belongs in these files.
"""
from __future__ import annotations

A4 = (595.0, 842.0)
LETTER = (612.0, 792.0)


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[dict]) -> bytes:
    objs: list[bytes] = []  # object n is objs[n-1]
    objs.append(b"")  # 1: catalog, filled below
    objs.append(b"")  # 2: page tree, filled below
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    kids = []
    for page in pages:
        w, h = page.get("size", A4)
        ops = []
        for x, y_top, size, text in page.get("lines", []):
            ops.append(f"BT /F1 {size} Tf {x} {h - y_top} Td ({_esc(text)}) Tj ET")
        stream = "\n".join(ops).encode("latin-1")
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        content_no = len(objs)
        rotate = int(page.get("rotate", 0))
        objs.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}] /Rotate {rotate} "
                     f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_no} 0 R >>").encode())
        kids.append(len(objs))
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = (f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] "
               f"/Count {len(kids)} >>").encode()
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def datasheet(pages: int = 2, size=A4) -> bytes:
    """A fictional two-page "datasheet" for a made-up part, ZX-100."""
    sample = [
        {"size": size, "lines": [
            (72, 80, 20, "ZX-100 Buck Regulator"),
            (72, 140, 11, "Input voltage range 2.7 V to 5.5 V"),
            (72, 160, 11, "Output current up to 1.2 A"),
            (72, 180, 11, "Quiescent current 18 uA typical"),
        ]},
        {"size": size, "lines": [
            (72, 80, 14, "Absolute maximum ratings"),
            (72, 120, 11, "VIN to GND -0.3 V to 6.0 V"),
            (72, 140, 11, "EN to GND -0.3 V to VIN"),
        ]},
    ]
    while len(sample) < pages:
        sample.append({"size": size, "lines": [(72, 80, 11, f"Page {len(sample) + 1} notes")]})
    return make_pdf(sample[:pages])
