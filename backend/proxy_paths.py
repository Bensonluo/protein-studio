"""Explicit application mount paths; never trust forwarded headers or rewrite arbitrary URLs."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

_BASE = re.compile(r"(?:/[A-Za-z0-9_-]+)+")
_URL_FIELDS = frozenset({"url", "structureUrl", "resultDetailUrl", "resultJsonUrl",
    "previewUrl", "tileUrl", "downloadUrl", "binaryDownloadUrl", "preparedUrl", "resultUrl"})
_META = '<meta name="protein-studio-base-path" content="">'


def normalize_base_path(value):
    if value in ("", "/"):
        return ""
    if not isinstance(value, str):
        raise ValueError("PROTEIN_STUDIO_BASE_PATH must be a path string.")
    value = value.rstrip("/")
    if not _BASE.fullmatch(value):
        raise ValueError("PROTEIN_STUDIO_BASE_PATH must use /name path segments (letters, digits, underscore or hyphen).")
    return value


def strip_request_target(target, base_path):
    """Remove exactly this mount prefix, preserving the raw query and escapes."""
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.fragment:
        raise ValueError("Only origin-form application request paths are accepted.")
    path = parsed.path
    if base_path:
        if path == base_path:
            path = "/"
        elif path.startswith(base_path + "/"):
            path = path[len(base_path):]
        else:
            raise KeyError("Application route not found.")
    return path + ("?" + parsed.query if parsed.query else "")


def application_url(value, base_path):
    """Prefix only server-generated API/worker paths; external and other roots stay intact."""
    if not base_path or not isinstance(value, str):
        return value
    parsed = urlsplit(value)
    if not parsed.scheme and not parsed.netloc and parsed.path.startswith(("/api/", "/worker/")):
        return base_path + value
    return value


def public_urls(payload, base_path):
    """Translate known public URL fields at serialization, without mutating stored results."""
    if not base_path:
        return payload
    if isinstance(payload, dict):
        return {key: application_url(value, base_path) if key in _URL_FIELDS and isinstance(value, str)
                else public_urls(value, base_path) for key, value in payload.items()}
    if isinstance(payload, list):
        return [public_urls(value, base_path) for value in payload]
    return payload


def index_html(text, base_path):
    """Inject a trusted path value into the single app-owned meta tag."""
    marker = '<meta name="protein-studio-base-path" content="' + base_path + '">'
    if _META in text:
        return text.replace(_META, marker, 1)
    # Custom web roots used by local integrations may not yet include this tag.
    return marker + "\n" + text if base_path else text
