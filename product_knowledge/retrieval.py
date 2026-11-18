"""Pick the few chunks of a brand's website/document text that answer a question.

Why BM25 and not embeddings: a brand's whole corpus is small (25 pages, a
few product records and documents — a few hundred chunks at most), BA
questions are short and name the product and the fact ("caffeine", "vegan",
"OG Luxe"), and keyword scoring is exact on exactly those words. It runs in
milliseconds in-process, needs no second paid API call per question, no
vector storage or backfill, and it is deterministic — so a test can say
which chunk wins. ``utils.ai_text`` has no embeddings helper today; if recall
on paraphrased questions turns out weak, embeddings can be added behind
``top_chunks`` without touching the prompt.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from .crawler import display_url

# ~800 tokens at ~4 chars/token.
CHUNK_CHARS = 3200
CHUNK_OVERLAP_CHARS = 300
TOP_K = 4
MAX_PER_SOURCE = 2
_K1 = 1.4
_B = 0.75

_STOP = set(
    """a an and are as at be but by can do does for from has have how i if in is it its
    me my of on or our so than that the their them then there these they this to
    us was we what when where which who why will with you your yours about any all
    much many does did get got just please tell know there's what's whats""".split()
)


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", (text or "").lower())
    return [_stem(w) for w in words if w not in _STOP and (len(w) > 1 or w.isdigit())]


def chunk_text(
    text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP_CHARS
) -> list[str]:
    """Split on line boundaries into ~size-char chunks with a little overlap."""
    text = (text or "").strip()
    if len(text) <= size:
        return [text] if text else []
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in text.split("\n"):
        while len(line) > size:  # one enormous line: hard-split it
            head, line = line[:size], line[size - overlap :]
            if current:
                chunks.append("\n".join(current))
                current, length = [], 0
            chunks.append(head)
        if length + len(line) + 1 > size and current:
            chunks.append("\n".join(current))
            tail = chunks[-1][-overlap:]
            current, length = [tail], len(tail)
        current.append(line)
        length += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return [c.strip() for c in chunks if c.strip()]


def source_label(source) -> str:
    """What the BA sees under an answer: the page address or document title."""
    if source.is_website:
        return f"From {display_url(source.url or source.ref)}"
    title = (source.title or "").strip() or source.get_kind_display()
    return f"From {title}"[:160]


@dataclass
class Chunk:
    source_id: int
    label: str
    text: str
    score: float = 0.0


def top_chunks(sources, question: str, k: int = TOP_K) -> list[Chunk]:
    """The best ``k`` chunks for ``question`` (BM25 over the brand's sources).

    ``sources`` are KnowledgeSource rows already filtered to one tenant and
    to non-excluded, readable rows. A question with no matching words gets
    the brand's homepage/about text as general context rather than nothing.
    """
    chunks: list[Chunk] = []
    docs: list[list[str]] = []
    for src in sources:
        label = source_label(src)
        # Title + address words count for every chunk of a source, so
        # "OG Luxe" finds the OG Luxe page even if the body says "it".
        head = tokenize(f"{src.title} {display_url(src.url) if src.url else ''}")
        for piece in chunk_text(src.text):
            chunks.append(Chunk(source_id=src.id, label=label, text=piece))
            docs.append(head + tokenize(piece))
    if not chunks:
        return []
    q_terms = list(dict.fromkeys(tokenize(question)))
    n = len(docs)
    avg = sum(len(d) for d in docs) / n or 1.0
    df: Counter = Counter()
    for d in docs:
        df.update(set(d))
    for chunk, d in zip(chunks, docs):
        tf = Counter(d)
        score = 0.0
        for term in q_terms:
            f = tf.get(term, 0)
            if not f:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * f * (_K1 + 1) / (f + _K1 * (1 - _B + _B * len(d) / avg))
        chunk.score = score
    ranked = sorted(
        (c for c in chunks if c.score > 0),
        key=lambda c: (-c.score, c.source_id, c.text[:40]),
    )
    picked: list[Chunk] = []
    per_source: Counter = Counter()
    for c in ranked:
        if per_source[c.source_id] >= MAX_PER_SOURCE:
            continue
        picked.append(c)
        per_source[c.source_id] += 1
        if len(picked) >= k:
            break
    if picked:
        return picked
    # Nothing matched: give the model the brand's own overview to work from.
    overview = sorted(
        (s for s in sources if s.is_website and s.text),
        key=lambda s: (len(display_url(s.url or s.ref)), s.id),
    )
    if overview:
        first = chunk_text(overview[0].text)[:1]
        return (
            [
                Chunk(
                    source_id=overview[0].id,
                    label=source_label(overview[0]),
                    text=first[0],
                )
            ]
            if first
            else []
        )
    return []
