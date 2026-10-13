"""URL slug helpers."""

import re
import unicodedata

_NON_WORD = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_length: int = 60) -> str:
    """Lowercase ASCII slug, words joined by hyphens, trimmed to max_length."""
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = _NON_WORD.sub("-", normalized.lower()).strip("-")
    return slug[:max_length].rstrip("-")
