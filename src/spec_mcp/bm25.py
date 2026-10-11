"""Pure-python BM25 (Okapi). No numpy, small corpora (thousands of chunks)."""

from __future__ import annotations

import math
import re
from collections import Counter

_TOKEN = re.compile(r"[\w][\w\-\.]*", re.UNICODE)


def tokenize(text: str) -> list[str]:
    toks: list[str] = []
    for raw in _TOKEN.findall(text.lower()):
        tok = raw.strip("._-")
        if not tok:
            continue
        toks.append(tok)
        # also index identifier parts: "act-r1" -> "act", "r1"; "api.dev" -> "api", "dev"
        parts = [p for p in re.split(r"[\-\._]", tok) if p]
        if len(parts) > 1:
            toks.extend(parts)
    return toks


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.n = len(docs)
        self.tf: list[Counter[str]] = [Counter(d) for d in docs]
        self.dl = [len(d) for d in docs]
        self.avgdl = (sum(self.dl) / self.n) if self.n else 0.0
        df: Counter[str] = Counter()
        for c in self.tf:
            df.update(c.keys())
        self.idf = {t: math.log(1 + (self.n - n_t + 0.5) / (n_t + 0.5)) for t, n_t in df.items()}

    def search(self, query: list[str], limit: int = 10) -> list[tuple[int, float]]:
        if not query or not self.n:
            return []
        scores = [0.0] * self.n
        for t in set(query):
            idf = self.idf.get(t)
            if idf is None:
                continue
            for i, c in enumerate(self.tf):
                f = c.get(t)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * self.dl[i] / (self.avgdl or 1))
                scores[i] += idf * f * (self.k1 + 1) / denom
        ranked = [(i, s) for i, s in enumerate(scores) if s > 0]
        ranked.sort(key=lambda x: (-x[1], x[0]))
        return ranked[:limit]
