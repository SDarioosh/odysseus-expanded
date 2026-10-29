"""Auto-detect a vision model from the endpoints the owner configured.

Settings → Vision → "Auto-detect" leaves ``vision_model`` empty. Upstream then tries a
fixed list of model names (gpt-4o, claude-*, gemini-*, llava, pixtral, qwen2-vl) and
gives up when none of them is served, so an install with only local models (Qwen3.5,
Gemma 3/4, Qwen-VL tags...) found no vision model at all, although picking the same
model by hand worked.

Here we look at the models the owner actually has, in this order:

1. the Utility model, then the Default chat model, when they accept images;
2. every other enabled model, endpoints on the owner's network first.

A model counts as vision-capable by :func:`src.chat_helpers.model_supports_vision`
(what LM Studio reports, else the name), the same check chat uses to decide whether a
model sees an attached image.

Hook: ``src/document_processor.py`` ``_resolve_vl_model`` and ``src/ink_transcribe.py``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

Route = tuple[str, str, dict]

# Never offer these as a vision model even if the name looks multimodal
# (e.g. "qwen3-vl-embedding", image generators, speech models).
_NOT_VISION_CHAT = (
    "embedding", "embed", "rerank", "tts", "whisper", "audio", "realtime",
    "dall-e", "image-gen", "flux", "stable-diffusion", "sdxl", "qwen-image",
    "moderation",
)


def _usable(model: str) -> bool:
    m = (model or "").lower()
    return bool(m) and not any(k in m for k in _NOT_VISION_CHAT)


def _sees_images(model: str, url: str) -> bool:
    from src.chat_helpers import model_supports_vision

    if not _usable(model):
        return False
    try:
        return bool(model_supports_vision(model, url))
    except Exception:
        return False


def _is_local(url: str) -> bool:
    try:
        from src.model_context import is_local_endpoint

        return bool(url) and is_local_endpoint(url)
    except Exception:
        return False


def _role_routes(owner: str | None) -> list[Route]:
    """The Utility and Default chat models as ``(url, model, headers)``."""
    from src.endpoint_resolver import resolve_endpoint

    out: list[Route] = []
    for prefix in ("utility", "default"):
        try:
            url, model, headers = resolve_endpoint(prefix, owner=owner)
        except Exception:
            continue
        if url and model:
            out.append((url, model, headers or {}))
    return out


def _endpoint_routes(owner: str | None) -> list[Route]:
    """Every enabled model on the owner's enabled chat endpoints, local ones first."""
    from src.endpoint_resolver import (
        SessionLocal,
        ModelEndpoint,
        build_chat_url,
        build_headers,
        resolve_endpoint_runtime,
    )

    from fork.upstream import endpoint_enabled_models

    local: list[Route] = []
    remote: list[Route] = []
    db = SessionLocal()
    try:
        query = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        if owner:
            from src.auth_helpers import owner_filter

            query = owner_filter(query, ModelEndpoint, owner)
        for ep in query.all():
            if (getattr(ep, "model_type", None) or "llm") != "llm":
                continue
            try:
                base, api_key = resolve_endpoint_runtime(ep, owner=owner)
            except Exception:
                continue
            url = build_chat_url(base)
            headers = build_headers(api_key, base)
            bucket = local if _is_local(url) else remote
            for model in endpoint_enabled_models(ep):
                bucket.append((url, model, headers))
    except Exception as exc:
        logger.debug("vision auto-detect: could not list endpoints: %s", exc)
    finally:
        db.close()
    return local + remote


def auto_detect_vision_model(owner: str | None = None, *, local_only: bool = False) -> Route | None:
    """The first configured model that reads images, or None.

    ``local_only`` leaves out endpoints off the owner's network.
    """
    seen: set[tuple[str, str]] = set()
    for url, model, headers in _role_routes(owner) + _endpoint_routes(owner):
        key = (url.rstrip("/"), model)
        if key in seen:
            continue
        seen.add(key)
        if local_only and not _is_local(url):
            continue
        if _sees_images(model, url):
            logger.info("vision auto-detect: using %s", model)
            return url, model, headers
    return None
