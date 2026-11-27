"""Fork: PDF datasheets, open a Library PDF in Write: the **adapter** to upstream's
documents (the Library) and its upload store. Nothing else in the ink code imports
upstream document or upload code; ``tests/fork/test_ink_library.py`` runs every function
here against the real upstream code (documents made by upstream's own import route).

The Library keeps an imported PDF as a Document whose content starts with a hidden
``pdf_source`` (or ``pdf_form_source``) marker naming an upload
(``routes/document/document_routes.py`` ``import-pdf``, ``src/pdf_form_doc.py``
``find_source_upload_id``). Owners are checked exactly as upstream checks them:

* :func:`pdf_documents` lists like ``GET /api/documents/library?language=pdf`` (active,
  not archived, ``_owner_session_filter``, newest first);
* :func:`document_pdf` opens like ``GET /api/document/{id}/render-pdf``
  (``_verify_doc_owner``, then the upload resolved for the caller,
  ``_resolve_user_upload_path``).

``user`` is upstream's ``get_current_user(request)`` (``None`` in single-user mode), as
upstream's routes pass it. The Library document is only read: never changed, never linked
back to the datasheet note made from it (``fork/ink/notes.py`` ``add_pdf``).
"""

from __future__ import annotations

import os
from typing import Any

from fork.ink.notes import InkError

MAX_DOCUMENTS = 500


def _iso(value: Any) -> str | None:
    return (value.isoformat() + "Z") if value is not None else None  # as upstream's Library


def pdf_documents(user: str | None) -> list[dict[str, Any]]:
    """``[{"id", "title", "updated_at"}]``: the user's Library documents that are PDFs,
    newest first (at most ``MAX_DOCUMENTS``)."""
    from sqlalchemy import or_

    from core.database import Document, SessionLocal
    from fork.upstream import owner_documents_filter
    from src.pdf_form_doc import find_source_upload_id

    db = SessionLocal()
    try:
        q = (db.query(Document)
             .filter(Document.is_active == True)  # noqa: E712 -- as upstream
             .filter(or_(Document.archived == False,  # noqa: E712
                         Document.archived.is_(None)))
             .filter(or_(Document.current_content.like('%<!-- pdf_source upload_id="%'),
                         Document.current_content.like('%<!-- pdf_form_source upload_id="%'))))
        q = owner_documents_filter(q, user)
        rows = q.order_by(Document.updated_at.desc()).limit(MAX_DOCUMENTS).all()
        return [{"id": d.id, "title": d.title or "", "updated_at": _iso(d.updated_at)}
                for d in rows if find_source_upload_id(d.current_content or "")]
    finally:
        db.close()


def document_pdf(user: str | None, document_id: str, auth_manager: Any = None
                 ) -> tuple[bytes, str, str]:
    """``(pdf bytes, file name, title)`` of the user's Library PDF document (the name is
    its title plus ``.pdf``, folders' slashes made dashes). Raises :class:`InkError`: 404 when it isn't the user's document,
    isn't a PDF or its file is gone, 413 over ``fork.ink.pdf.MAX_PDF_BYTES``."""
    from fastapi import HTTPException

    from core.database import Document, SessionLocal
    from fork.ink import pdf as ink_pdf
    from fork.upstream import resolve_user_upload_path, verify_document_owner
    from src.pdf_form_doc import find_source_upload_id
    from src.tool_utils import get_upload_handler

    if not isinstance(document_id, str) or not 0 < len(document_id) <= 64:
        raise InkError("Document not found", 404)
    db = SessionLocal()
    try:
        doc = db.query(Document).filter(Document.id == document_id).first()
        if doc is None:
            raise InkError("Document not found", 404)
        try:
            verify_document_owner(db, doc, user)
        except HTTPException:
            raise InkError("Document not found", 404) from None
        upload_id = find_source_upload_id(doc.current_content or "")
        title = (doc.title or "").strip()
    finally:
        db.close()
    if not upload_id:
        raise InkError("That document is not a PDF", 404)
    path = resolve_user_upload_path(get_upload_handler(), upload_id, user, auth_manager)
    if not path:
        raise InkError("The document's PDF is no longer available", 404)
    try:
        if os.path.getsize(path) > ink_pdf.MAX_PDF_BYTES:
            raise InkError(f"A PDF is at most {ink_pdf.MAX_PDF_BYTES // (1024 * 1024)} MB", 413)
        with open(path, "rb") as fh:
            data = fh.read(ink_pdf.MAX_PDF_BYTES + 1)
    except OSError:
        raise InkError("The document's PDF is no longer available", 404) from None
    name = (title or "document").replace("/", "-").replace("\\", "-")
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return data, name, title
