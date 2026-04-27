"""
Knowledge base + RAG retriever for the PawPal+ Care Advisor.

Loads markdown documents from ``knowledge/``, splits each into paragraph-sized
chunks, and indexes them with a pure-Python TF-IDF + cosine retriever. No
external dependencies (no scikit-learn, no embeddings API), so the system
works offline and reproducibly.

Public surface:

* ``KnowledgeBase.from_dir(path)`` — load and index a directory of ``.md`` files
* ``KnowledgeBase.retrieve(query, k=3)`` — return the top-k ``RetrievalResult``s
* ``RetrievalResult.score`` — cosine similarity in [0, 1], used by the
  agent's confidence calculation and by guardrails to refuse low-confidence
  answers.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# A small English stoplist. Keeping this short and domain-aware (we
# intentionally do *not* drop "dog", "cat", "vet", "feed" etc.) is enough
# to make TF-IDF scoring meaningful for short user queries like
# "my dog won't eat".
_STOPWORDS: set[str] = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has",
    "have", "he", "in", "is", "it", "its", "of", "on", "or", "she", "that",
    "the", "this", "to", "was", "were", "with", "i", "my", "me", "you",
    "your", "we", "our", "they", "them", "their", "what", "how", "why",
    "do", "does", "did", "should", "would", "could", "can", "will", "if",
    "then", "than", "but", "so", "not", "no", "yes", "any", "all",
}

_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z\-']+")


# Lightweight topic aliases per source file — used by KnowledgeBase.retrieve()
# to disambiguate when the same proper noun appears in multiple documents.
# Kept tiny on purpose: this is *not* a synonym engine, just enough hints to
# steer obvious queries to the right doc.
_FILENAME_ALIASES: dict[str, set[str]] = {
    "feeding.md":      {"feed", "feeding", "food", "meal", "diet", "appetite", "hungry", "eat", "eating"},
    "walking.md":      {"walk", "walking", "exercise", "run", "running", "leash"},
    "medication.md":   {"medication", "medicine", "pill", "dose", "antibiotic", "insulin", "prescription"},
    "grooming.md":     {"groom", "grooming", "brush", "brushing", "bath", "bathing", "nail", "coat", "fur", "shed", "shedding"},
    "emergency.md":    {"emergency", "poison", "toxic", "seizure", "bleeding", "collapse", "choking"},
    "behavior.md":     {"bark", "barking", "anxious", "anxiety", "aggressive", "destructive", "chewing", "lethargic", "lethargy"},
    "general_care.md": {"routine", "schedule", "daily", "stimulation", "enrichment", "boredom"},
}


def _tokenize(text: str) -> list[str]:
    """Lowercase, drop punctuation/numbers, drop stopwords. Keeps domain words."""
    return [t for t in (m.group(0).lower() for m in _TOKEN_RE.finditer(text))
            if t not in _STOPWORDS and len(t) > 2]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Chunk:
    """A single retrievable passage."""
    source: str           # filename, e.g. "feeding.md"
    section: str          # the markdown heading the chunk lives under
    text: str             # the full chunk text
    tokens: list[str] = field(default_factory=list)


@dataclass
class RetrievalResult:
    """One hit from a retrieval call. ``score`` is cosine similarity in [0, 1]."""
    chunk: Chunk
    score: float

    def cite(self) -> str:
        """Return a compact citation like ``feeding.md > Dogs``."""
        return f"{self.chunk.source} > {self.chunk.section}"


# ---------------------------------------------------------------------------
# KnowledgeBase
# ---------------------------------------------------------------------------

class KnowledgeBase:
    """In-memory TF-IDF index over a directory of markdown documents."""

    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        self._df: Counter[str] = Counter()
        self._idf: dict[str, float] = {}
        self._chunk_vectors: list[dict[str, float]] = []
        self._build_index()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_dir(cls, path: str | Path) -> "KnowledgeBase":
        """
        Walk ``path`` for ``*.md`` files, split each on ``##`` headings, and
        index the resulting chunks. Raises ``FileNotFoundError`` if the
        directory is missing or empty.
        """
        root = Path(path)
        if not root.is_dir():
            raise FileNotFoundError(f"Knowledge directory not found: {root}")
        chunks: list[Chunk] = []
        for md in sorted(root.glob("*.md")):
            chunks.extend(_split_markdown(md))
        if not chunks:
            raise FileNotFoundError(f"No .md files found in {root}")
        return cls(chunks)

    def _build_index(self) -> None:
        """Compute IDF over all chunks and a TF-IDF vector per chunk."""
        for c in self.chunks:
            c.tokens = _tokenize(c.text)
            for term in set(c.tokens):
                self._df[term] += 1

        n = len(self.chunks)
        # Smoothed IDF so unseen terms don't blow up.
        self._idf = {term: math.log((n + 1) / (df + 1)) + 1
                     for term, df in self._df.items()}

        for c in self.chunks:
            self._chunk_vectors.append(self._vectorize(c.tokens))

    # ------------------------------------------------------------------
    # Vector ops
    # ------------------------------------------------------------------

    def _vectorize(self, tokens: list[str]) -> dict[str, float]:
        if not tokens:
            return {}
        tf = Counter(tokens)
        vec = {term: (count / len(tokens)) * self._idf.get(term, 0.0)
               for term, count in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values()))
        if norm > 0:
            for k in vec:
                vec[k] /= norm
        return vec

    @staticmethod
    def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
        if not a or not b:
            return 0.0
        # Iterate over the smaller dict for speed.
        if len(a) > len(b):
            a, b = b, a
        return sum(v * b.get(term, 0.0) for term, v in a.items())

    # ------------------------------------------------------------------
    # Public retrieval
    # ------------------------------------------------------------------

    def retrieve(self, query: str, k: int = 3,
                 min_score: float = 0.05) -> list[RetrievalResult]:
        """
        Return the top-``k`` chunks for ``query``, filtered to those with
        cosine similarity >= ``min_score``.

        Final score is ``cosine(query, chunk_body) + 0.15 * heading_bonus``,
        where ``heading_bonus`` is the fraction of query tokens that appear
        in the chunk's section heading or source filename. This biases
        retrieval toward chunks whose *topic* matches the query — important
        when a single keyword (e.g. "Shiba Inu") appears in multiple docs
        but the user clearly asked about one of them ("How often should I
        BRUSH my Shiba Inu?" should hit grooming.md, not walking.md).

        ``min_score`` exists so a wildly off-topic query returns an empty
        list, which the agent treats as "I don't know" — a guardrail against
        hallucinating outside the knowledge base.
        """
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []
        q_token_set = set(q_tokens)
        q_vec = self._vectorize(q_tokens)

        scored: list[tuple[float, int]] = []
        for idx, vec in enumerate(self._chunk_vectors):
            chunk = self.chunks[idx]
            base = self._cosine(q_vec, vec)

            # Heading bonus: how many query tokens appear in the section name.
            heading_tokens = set(_tokenize(chunk.section))
            heading_overlap = len(q_token_set & heading_tokens)
            heading_bonus = (heading_overlap / len(q_token_set)) if q_token_set else 0.0

            # Filename bonus: covers the case where the query's intent verb
            # (e.g. "brush") matches the document topic (grooming.md). We
            # store an alias map here rather than per-document metadata so
            # the index stays a single in-memory structure.
            filename_alias = _FILENAME_ALIASES.get(chunk.source, set())
            filename_bonus = 1.0 if (q_token_set & filename_alias) else 0.0

            final = base + 0.15 * heading_bonus + 0.10 * filename_bonus
            scored.append((final, idx))

        scored.sort(reverse=True)

        results: list[RetrievalResult] = []
        for score, idx in scored[:k]:
            if score < min_score:
                continue
            results.append(RetrievalResult(chunk=self.chunks[idx], score=float(score)))
        return results

    # ------------------------------------------------------------------
    # Helpers used by the agent
    # ------------------------------------------------------------------

    def render_context(self, results: list[RetrievalResult],
                       max_chars: int = 1800) -> str:
        """
        Format retrieval hits into a single string the LLM can consume,
        with inline citations and a hard character cap so prompts stay small.
        """
        if not results:
            return ""
        parts: list[str] = []
        used = 0
        for r in results:
            header = f"[Source: {r.cite()} | relevance {r.score:.2f}]"
            body = r.chunk.text.strip()
            block = f"{header}\n{body}"
            if used + len(block) > max_chars and parts:
                break
            parts.append(block)
            used += len(block)
        return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Markdown splitting
# ---------------------------------------------------------------------------

def _split_markdown(path: Path) -> list[Chunk]:
    """
    Split a markdown file on ``##`` headings. Each section becomes one chunk.
    The H1 (if present) is included as the first chunk's section name.

    We deliberately do not chunk smaller than ``##`` sections — the docs
    were authored with retrieval in mind, and finer splits hurt recall on
    short user queries.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    # Track the document title (first ``# `` heading) for nicer citations.
    doc_title = ""
    for line in lines:
        if line.startswith("# "):
            doc_title = line[2:].strip()
            break

    # Split body on ``##`` headings.
    sections: list[tuple[str, str]] = []
    current_heading = doc_title or path.stem
    buffer: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if buffer:
                sections.append((current_heading, "\n".join(buffer).strip()))
            current_heading = line[3:].strip()
            buffer = []
        else:
            if not line.startswith("# "):  # drop the H1 line itself
                buffer.append(line)
    if buffer:
        sections.append((current_heading, "\n".join(buffer).strip()))

    return [Chunk(source=path.name, section=heading, text=body)
            for heading, body in sections if body]
