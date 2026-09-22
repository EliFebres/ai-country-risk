"""
Content hashing — the one place a hash is computed.

Every cache key in this project is a hash of the exact text a model read. That
only works if there is a single definition of "the same text": two call sites
that normalize differently produce two hashes for one article, the cache misses
forever, and the bill looks like a cache that is working because nothing errors.

So there is one function, and nothing else computes a digest.

The normalization is deliberately small — Unicode NFC, trimmed, runs of
whitespace collapsed to one space. A re-scrape that reflows a paragraph or
changes a non-breaking space to a space is the same article and gets the same
hash; a re-scrape that gains a sentence is not, and does not.

Nothing here lowercases or strips punctuation. A digest of "US SANCTIONS LIFTED"
is not a digest of "us sanctions lifted", and pretending otherwise would serve a
cached answer for text the model never saw.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

__all__ = ["content_hash", "normalize"]


_WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Return `text` in the canonical form that :func:`content_hash` hashes.

    Exposed so a caller can store, log or compare the exact string that produced
    a hash. Hashing normalized text without being able to see it is how a cache
    key becomes unauditable.

    Args:
        text: Any string. ``None`` is not accepted; an empty string is.

    Returns:
        NFC-normalized, whitespace-collapsed, trimmed text.
    """
    if text is None:
        raise TypeError("content hashing needs a string, not None")
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFC", text)).strip()


def content_hash(text: str) -> str:
    """Return the SHA-256 hex digest of `text` after normalization.

    Args:
        text: The exact text a model read, or is about to read.

    Returns:
        A 64-character lowercase hex digest.
    """
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()
