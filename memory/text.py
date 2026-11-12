"""Small text utilities shared by the memory layers.

No embeddings, no external service: a deterministic token set plus Jaccard
similarity is enough to find "a task like this one" and keeps the project free
of heavy dependencies.  The interface is intentionally the same shape a vector
store would offer, so swapping in embeddings later only touches this module.
"""

from __future__ import annotations

import re

_WORD = re.compile(r"[a-zA-Z0-9_]{2,}")
_CJK = re.compile(r"[\u4e00-\u9fff]")

STOPWORDS = {
    "the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "is", "are",
    "be", "with", "by", "at", "it", "this", "that", "from", "as", "into",
    "then", "please", "使用", "打开", "一个", "这个", "然后", "并且", "进行",
    "the", "my", "me", "i", "you", "we",
}


def tokenize(text: str) -> set[str]:
    """Return a comparable token set for English and Chinese text."""
    if not text:
        return set()
    lowered = text.lower()
    tokens = {word for word in _WORD.findall(lowered) if word not in STOPWORDS}
    # Chinese has no spaces: use single characters plus character bigrams.
    cjk = _CJK.findall(lowered)
    tokens.update(cjk)
    tokens.update("".join(pair) for pair in zip(cjk, cjk[1:]))
    return tokens


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    union = len(left | right)
    return intersection / union if union else 0.0


def similarity(left: str, right: str) -> float:
    return jaccard(tokenize(left), tokenize(right))


def keyword_score(query: str, text: str) -> float:
    """Recall-oriented score used for memory search."""
    query_tokens = tokenize(query)
    if not query_tokens:
        return 0.0
    text_tokens = tokenize(text)
    if not text_tokens:
        return 0.0
    return len(query_tokens & text_tokens) / len(query_tokens)


def short(text: str, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."
