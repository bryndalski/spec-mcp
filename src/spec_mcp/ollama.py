"""Optional local-RAG layer: embeddings + answer generation through an Ollama server.

Nothing here is required. Without an `ollama` section in the config every tool in this module
answers {"error": {"category": "ollama_not_configured", ...}} and the BM25 tools behave exactly
as before. Ollama never writes anything: it only retrieves and answers.

Corpora (config `ollama.corpora`): `docs` (heading chunks of visible documents), `flows`
(one item per step and per async step), `requirements` (text + acceptance), `records:<name>`
(one item per record of a configured collection).
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import operator
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

import httpx

from .bm25 import BM25, tokenize
from .errors import SpecError
from .index import SpecIndex
from . import records as rc
from . import verify as vf

try:  # optional accelerator (extra `fast`)
    import numpy as np  # type: ignore[import-not-found]
except ImportError:  # pure-python fallback
    np = None  # type: ignore[assignment]

OLLAMA_DEFAULTS: dict[str, Any] = {
    "base_url": "http://localhost:11434",
    "embed_model": "bge-m3",
    "chat_model": "mistral-small3.2:24b",
    "cache_path": ".spec-embeddings.sqlite",
    "corpora": ["docs", "flows", "requirements"],
    "chunk_max_chars": 1200,
    "top_k": 8,
    "min_score": 0.54,
    "confidence_margin": 0.12,
    "hybrid": {"bm25": 0.35, "embed": 0.65},
    "bm25_saturation": 8.0,
    "timeout_s": 120,
    "keep_alive": "10m",
    "temperature": 0.1,
    "num_ctx": 8192,
    "embed_batch": 32,
    "embed_max_chars": 2400,
    "source_max_chars": 1500,
    "lazy_embed_limit": 200,
    "embed_query_prefix": "",
    "embed_doc_prefix": "",
}

_ALLOWED_KEYS = set(OLLAMA_DEFAULTS)
_CORPUS_RE = re.compile(r"^(docs|flows|requirements|records:[A-Za-z0-9_.\-]+)$")

# Test hook: an httpx transport (e.g. MockTransport) used instead of the network.
_TRANSPORT: httpx.BaseTransport | None = None

SYSTEM_PROMPT = (
    "Odpowiadaj WYŁĄCZNIE na podstawie źródeł [n]. Każde twierdzenie z odwołaniem [n]. "
    "Jeśli źródła nie wystarczają — napisz »brak w dokumentacji« i wskaż czego brakuje. "
    "Nie zgaduj. Odpowiadaj po polsku, zwięźle."
)

_CITE = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+)*)\]")
_NO_ANSWER = re.compile(r"brak\s+w\s+dokumentacji", re.IGNORECASE)


# ---------------------------------------------------------------- configuration


def resolve_config(raw: Any) -> dict[str, Any] | None:
    """Validate the user's `ollama` section and merge it over the defaults.
    Returns None when the section is absent/empty (layer not configured)."""
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, dict):
        raise SpecError("config", "`ollama` must be an object")
    unknown = sorted(set(raw) - _ALLOWED_KEYS)
    if unknown:
        raise SpecError(
            "config", f"ollama: unknown keys {unknown}", allowed=sorted(_ALLOWED_KEYS)
        )
    cfg = json.loads(json.dumps(OLLAMA_DEFAULTS))
    for k, v in raw.items():
        if k == "hybrid":
            if not isinstance(v, dict):
                raise SpecError(
                    "config", "ollama.hybrid must be an object {bm25, embed}"
                )
            bad = sorted(set(v) - {"bm25", "embed"})
            if bad:
                raise SpecError("config", f"ollama.hybrid: unknown keys {bad}")
            cfg["hybrid"] = {**cfg["hybrid"], **v}
        else:
            cfg[k] = v
    for k in (
        "base_url",
        "embed_model",
        "chat_model",
        "cache_path",
        "keep_alive",
        "embed_query_prefix",
        "embed_doc_prefix",
    ):
        if not isinstance(cfg[k], str):
            raise SpecError("config", f"ollama.{k} must be a string")
    for k in ("base_url", "embed_model", "chat_model", "cache_path"):
        if not cfg[k].strip():
            raise SpecError("config", f"ollama.{k} must not be empty")
    if not cfg["base_url"].startswith(("http://", "https://")):
        raise SpecError("config", "ollama.base_url must start with http:// or https://")
    corpora = cfg["corpora"]
    if (
        not isinstance(corpora, list)
        or not corpora
        or not all(isinstance(c, str) and _CORPUS_RE.match(c) for c in corpora)
    ):
        raise SpecError(
            "config",
            "ollama.corpora must be a non-empty list of: docs, flows, requirements, records:<name>",
        )
    for k in (
        "chunk_max_chars",
        "top_k",
        "embed_batch",
        "embed_max_chars",
        "source_max_chars",
        "lazy_embed_limit",
        "num_ctx",
    ):
        if not isinstance(cfg[k], int) or isinstance(cfg[k], bool) or cfg[k] < 1:
            raise SpecError("config", f"ollama.{k} must be a positive integer")
    for k in (
        "min_score",
        "confidence_margin",
        "temperature",
        "timeout_s",
        "bm25_saturation",
    ):
        if (
            not isinstance(cfg[k], (int, float))
            or isinstance(cfg[k], bool)
            or cfg[k] < 0
        ):
            raise SpecError("config", f"ollama.{k} must be a non-negative number")
    h = cfg["hybrid"]
    for k in ("bm25", "embed"):
        if not isinstance(h[k], (int, float)) or isinstance(h[k], bool) or h[k] < 0:
            raise SpecError(
                "config", f"ollama.hybrid.{k} must be a non-negative number"
            )
    if h["bm25"] + h["embed"] <= 0:
        raise SpecError("config", "ollama.hybrid weights must not both be zero")
    if cfg["bm25_saturation"] <= 0:
        raise SpecError("config", "ollama.bm25_saturation must be > 0")
    return cfg


def require(cfg: dict[str, Any]) -> dict[str, Any]:
    o = cfg.get("_ollama")
    if not o:
        raise SpecError(
            "ollama_not_configured",
            "no `ollama` section in the spec config; the local-RAG layer is optional",
            hint='add {"ollama": {"embed_model": "bge-m3", "chat_model": "qwen2.5:7b-instruct"}} to .spec-mcp.json',
        )
    return o


# ---------------------------------------------------------------- Ollama HTTP client


class OllamaClient:
    def __init__(self, o: dict[str, Any]):
        self.o = o
        self.http = httpx.Client(
            base_url=o["base_url"].rstrip("/"),
            timeout=float(o["timeout_s"]),
            transport=_TRANSPORT,
        )

    def close(self) -> None:
        self.http.close()

    def _unavailable(self, e: Exception) -> SpecError:
        return SpecError(
            "ollama_unavailable",
            f"cannot reach Ollama at {self.o['base_url']}: {type(e).__name__}: {e}",
            retryable=True,
            hint="start Ollama (open -a Ollama) and retry",
        )

    def _post(self, path: str, body: dict[str, Any], model: str) -> dict[str, Any]:
        try:
            r = self.http.post(path, json=body)
        except httpx.HTTPError as e:
            raise self._unavailable(e) from e
        if r.status_code == 404 or (
            r.status_code >= 400 and "not found" in r.text.lower()
        ):
            raise SpecError(
                "model_missing",
                f"Ollama has no model {model!r}",
                hint=f"ollama pull {model}",
                status=r.status_code,
            )
        if r.status_code >= 400:
            raise SpecError(
                "ollama_unavailable",
                f"Ollama {path} returned HTTP {r.status_code}: {r.text[:200]}",
                retryable=r.status_code >= 500,
                hint="start Ollama (open -a Ollama) and retry",
                status=r.status_code,
            )
        try:
            return r.json()
        except ValueError as e:
            raise SpecError(
                "ollama_unavailable",
                f"Ollama {path} returned invalid JSON",
                retryable=True,
            ) from e

    def tags(self) -> list[str]:
        try:
            r = self.http.get("/api/tags")
        except httpx.HTTPError as e:
            raise self._unavailable(e) from e
        if r.status_code >= 400:
            raise SpecError(
                "ollama_unavailable",
                f"Ollama /api/tags returned HTTP {r.status_code}",
                retryable=True,
                hint="start Ollama (open -a Ollama) and retry",
            )
        return [str(m.get("name")) for m in (r.json().get("models") or [])]

    def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        model = model or self.o["embed_model"]
        out = self._post(
            "/api/embed",
            {"model": model, "input": texts, "keep_alive": self.o["keep_alive"]},
            model,
        )
        vecs = out.get("embeddings")
        if not isinstance(vecs, list) or len(vecs) != len(texts):
            raise SpecError(
                "ollama_unavailable",
                "Ollama /api/embed returned an unexpected shape",
                retryable=True,
            )
        return [_normalize(v) for v in vecs]

    def chat(self, messages: list[dict[str, str]], model: str | None = None) -> str:
        model = model or self.o["chat_model"]
        body = {
            "model": model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.o["keep_alive"],
            "options": {
                "temperature": self.o["temperature"],
                "num_ctx": self.o["num_ctx"],
            },
        }
        out = self._post("/api/chat", body, model)
        msg = out.get("message") or {}
        return str(msg.get("content") or "").strip()


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


# ---------------------------------------------------------------- vector store (sqlite)


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS chunks(key TEXT PRIMARY KEY, corpus TEXT NOT NULL, hash TEXT NOT NULL, dim INTEGER NOT NULL, vec BLOB NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL)"
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def meta(self, k: str) -> str | None:
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else None

    def set_meta(self, k: str, v: str) -> None:
        self.db.execute(
            "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, v),
        )

    def hashes(self) -> dict[str, str]:
        return {k: h for k, h in self.db.execute("SELECT key, hash FROM chunks")}

    def count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])

    def put(self, key: str, corpus: str, h: str, vec: list[float]) -> None:
        blob = array.array("f", vec).tobytes()
        self.db.execute(
            "INSERT INTO chunks(key,corpus,hash,dim,vec) VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET corpus=excluded.corpus, hash=excluded.hash, dim=excluded.dim, vec=excluded.vec",
            (key, corpus, h, len(vec), blob),
        )

    def delete(self, keys: Iterable[str]) -> int:
        keys = list(keys)
        for i in range(0, len(keys), 500):
            part = keys[i : i + 500]
            self.db.execute(
                f"DELETE FROM chunks WHERE key IN ({','.join('?' * len(part))})", part
            )
        return len(keys)

    def clear(self) -> None:
        self.db.execute("DELETE FROM chunks")

    def commit(self) -> None:
        self.db.commit()

    def vectors(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, dim, blob in self.db.execute("SELECT key, dim, vec FROM chunks"):
            a = array.array("f")
            a.frombytes(blob)
            out[key] = a
        return out


# ---------------------------------------------------------------- corpus items


def _h(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _evidence_of_text(text: str) -> list[dict[str, Any]]:
    return [e for e in vf._ref_entries(text)]


def _anchor_entries(anchors: Any) -> list[dict[str, Any]]:
    out = []
    for a in anchors or []:
        if isinstance(a, dict) and a.get("file"):
            out.append(
                {
                    "file": a["file"],
                    "symbol": a.get("symbol"),
                    "line": a.get("line"),
                    "line_end": a.get("line"),
                }
            )
    return out


def _flat(v: Any) -> str:
    if isinstance(v, (list, tuple)):
        return ", ".join(_flat(x) for x in v)
    if isinstance(v, dict):
        return ", ".join(f"{k}={_flat(x)}" for k, x in v.items())
    return str(v)


def build_items(idx: SpecIndex, o: dict[str, Any]) -> list[dict[str, Any]]:
    """Retrievable items of every configured corpus. `embeddable` marks the ones that get a
    vector (documents that are not hidden by status); the rest stay BM25-only."""
    items: list[dict[str, Any]] = []
    corpora = o["corpora"]
    prefix = o["embed_doc_prefix"]
    if "docs" in corpora:
        status_map = idx.doc_status_map()
        hidden = idx.hidden_statuses()
        for c in idx.chunks:
            st = status_map.get(c["file"])
            is_hidden = st in hidden
            body = f"{c.get('path') or c['heading']}\n{c['text']}".strip()
            items.append(
                {
                    "key": f"docs:{c['file']}:{c['line']}:{c.get('part', 0)}",
                    "corpus": "docs",
                    "kind": "doc",
                    "file": c["file"],
                    "heading": c["heading"],
                    "path": c.get("path") or c["heading"],
                    "line": c["line"],
                    "line_end": c.get("line_end"),
                    "status": st,
                    "hidden": is_hidden,
                    "embeddable": not is_hidden,
                    "text": c["text"],
                    "embed_text": prefix + body,
                    "evidence": [],
                }
            )
    if "flows" in corpora:
        for f in idx.flows.values():
            head = f"Flow {f['id']} — {f['title']} (role: {', '.join(f.get('roles') or []) or '-'})"
            for s in f.get("steps") or []:
                text = f"{head}\nKrok {s.get('n')}: rola {s.get('role') or '-'}, ekran {s.get('screen') or '-'}. {s.get('action') or ''}\nDowód: {s.get('evidence') or '-'}"
                ev = _anchor_entries(s.get("anchors")) or _evidence_of_text(
                    str(s.get("evidence") or "")
                )
                items.append(
                    {
                        "key": f"flow:{f['id']}:step:{s.get('n')}",
                        "corpus": "flows",
                        "kind": "flow_step",
                        "file": f["file"],
                        "heading": f"{f['id']} krok {s.get('n')}",
                        "path": f"{f['title']} > krok {s.get('n')}",
                        "line": None,
                        "line_end": None,
                        "status": None,
                        "hidden": False,
                        "embeddable": True,
                        "text": text,
                        "embed_text": prefix + text,
                        "evidence": ev,
                    }
                )
            for a in f.get("async") or []:
                text = f"{head}\nKrok asynchroniczny {a.get('n')} ({a.get('runtime')}): {a.get('action') or ''}\nDowód: {a.get('evidence') or '-'}"
                items.append(
                    {
                        "key": f"flow:{f['id']}:async:{a.get('n')}",
                        "corpus": "flows",
                        "kind": "flow_async",
                        "file": f["file"],
                        "heading": f"{f['id']} async {a.get('n')}",
                        "path": f"{f['title']} > async {a.get('n')} ({a.get('runtime')})",
                        "line": None,
                        "line_end": None,
                        "status": None,
                        "hidden": False,
                        "embeddable": True,
                        "text": text,
                        "embed_text": prefix + text,
                        "evidence": _evidence_of_text(str(a.get("evidence") or "")),
                    }
                )
    if "requirements" in corpora:
        for r in idx.req_all:
            acc = "; ".join(r.get("acceptance") or [])
            text = (
                f"{r['id']} [{r.get('domain')}] {r.get('title') or ''}\n{r['text']}"
                + (f"\nAkceptacja: {acc}" if acc else "")
                + f"\nStatus: {r.get('status') or 'brak'}"
            )
            ev = _anchor_entries(r.get("anchors")) or _evidence_of_text(r["text"])
            items.append(
                {
                    "key": f"req:{r['id']}:{r['file']}",
                    "corpus": "requirements",
                    "kind": "requirement",
                    "file": r["file"],
                    "heading": r["id"],
                    "path": f"{r.get('domain')} > {r['id']}",
                    "line": r.get("line"),
                    "line_end": None,
                    "status": r.get("status"),
                    "hidden": False,
                    "embeddable": True,
                    "text": text,
                    "embed_text": prefix + text,
                    "evidence": ev,
                }
            )
    for corpus in corpora:
        if not corpus.startswith("records:"):
            continue
        name = corpus.split(":", 1)[1]
        coll = rc.collections(idx.cfg).get(name)
        if coll is None:
            continue
        rows, _problems = coll.load()
        for row in rows:
            rid = str(row.get(coll.id_field) or row.get("_index"))
            fields = "\n".join(
                f"{k}: {_flat(v)}"
                for k, v in row.items()
                if k not in ("_file", "_index") and v not in (None, "", [], {})
            )
            text = f"Rekord {name} {rid}\n{fields}"
            items.append(
                {
                    "key": f"rec:{name}:{rid}:{row.get('_file')}",
                    "corpus": corpus,
                    "kind": "record",
                    "file": row.get("_file"),
                    "heading": rid,
                    "path": f"{name} > {rid}",
                    "line": None,
                    "line_end": None,
                    "status": str(row["status"]) if row.get("status") else None,
                    "hidden": False,
                    "embeddable": True,
                    "text": text,
                    "embed_text": prefix + text,
                    "evidence": [],
                }
            )
    for it in items:
        it["hash"] = _h(it["embed_text"]) if it["embeddable"] else ""
    return items


# ---------------------------------------------------------------- engine


def _dot(a: Any, b: Any) -> float:
    return sum(map(operator.mul, a, b))


class Engine:
    """Per-process state: items, BM25 over them, vectors, the sqlite store."""

    def __init__(self, idx: SpecIndex, o: dict[str, Any]):
        self.idx = idx
        self.o = o
        self.root = idx.root
        self.store_path = idx.resolve(o["cache_path"])
        self._items: list[dict[str, Any]] | None = None
        self._items_fp: str | None = None
        self._bm25: BM25 | None = None
        self._vectors: dict[str, Any] | None = None
        self._vec_stamp: tuple[float, int] | None = None
        self._matrix: Any = None
        self._matrix_keys: list[str] | None = None
        self.stale_index = False

    # ------------------------------------------------------------ items / store

    def _records_fp(self) -> str:
        h = hashlib.sha1()
        for corpus in self.o["corpora"]:
            if not corpus.startswith("records:"):
                continue
            coll = rc.collections(self.idx.cfg).get(corpus.split(":", 1)[1])
            if coll is None:
                continue
            for f in coll.files():
                try:
                    st = f.stat()
                    h.update(f"{f}|{st.st_mtime_ns}|{st.st_size}".encode())
                except OSError:
                    pass
        return h.hexdigest()

    def items(self) -> list[dict[str, Any]]:
        fp = f"{self.idx.fingerprint}|{self._records_fp()}"
        if self._items is None or self._items_fp != fp:
            self._items = build_items(self.idx, self.o)
            self._items_fp = fp
            self._bm25 = None
            self._matrix = None
        return self._items

    def bm25(self) -> BM25:
        items = self.items()
        if self._bm25 is None:
            self._bm25 = BM25([tokenize(f"{it['path']}\n{it['text']}") for it in items])
        return self._bm25

    def open_store(self) -> Store:
        return Store(self.store_path)

    def _pending(self, store: Store) -> tuple[list[dict[str, Any]], list[str], bool]:
        """(items needing a vector, orphan keys, model_changed)."""
        items = [it for it in self.items() if it["embeddable"]]
        have = store.hashes()
        model_changed = (
            store.count() > 0 and store.meta("embed_model") != self.o["embed_model"]
        )
        if model_changed:
            have = {}
        todo = [it for it in items if have.get(it["key"]) != it["hash"]]
        live = {it["key"] for it in items}
        orphans = [k for k in have if k not in live]
        return todo, orphans, model_changed

    # ------------------------------------------------------------ indexing

    def reindex(self, scope: str = "all", force: bool = False) -> dict[str, Any]:
        t0 = time.time()
        client = OllamaClient(self.o)
        store = self.open_store()
        try:
            if force:
                store.clear()
            todo, orphans, model_changed = self._pending(store)
            if model_changed:
                store.clear()
                todo, orphans, _ = self._pending(store)
            if scope != "all":
                todo = [
                    it
                    for it in todo
                    if it["corpus"] == scope or it["corpus"].startswith(scope + ":")
                ]
            embedded = 0
            by_corpus: dict[str, dict[str, int]] = {}
            all_items = [it for it in self.items() if it["embeddable"]]
            for it in all_items:
                by_corpus.setdefault(
                    it["corpus"], {"embedded": 0, "reused": 0, "total": 0}
                )["total"] += 1
            batch = int(self.o["embed_batch"])
            maxc = int(self.o["embed_max_chars"])
            for i in range(0, len(todo), batch):
                part = todo[i : i + batch]
                vecs = client.embed([it["embed_text"][:maxc] for it in part])
                for it, v in zip(part, vecs):
                    store.put(it["key"], it["corpus"], it["hash"], v)
                    by_corpus[it["corpus"]]["embedded"] += 1
                embedded += len(part)
                store.commit()
            removed = store.delete(orphans) if scope == "all" else 0
            store.set_meta("embed_model", self.o["embed_model"])
            store.set_meta("updated_at", str(time.time()))
            store.commit()
            total = store.count()
            for c in by_corpus.values():
                c["reused"] = c["total"] - c["embedded"]
            self._vectors = None
            self._matrix = None
            self.stale_index = False
            return {
                "embedded": embedded,
                "reused": len(all_items) - embedded
                if scope == "all"
                else sum(c["reused"] for c in by_corpus.values()),
                "removed": removed,
                "total_chunks": total,
                "embeddable_items": len(all_items),
                "bm25_only_items": len(self.items()) - len(all_items),
                "duration_s": round(time.time() - t0, 2),
                "by_corpus": by_corpus,
                "embed_model": self.o["embed_model"],
                "model_changed": model_changed,
            }
        finally:
            store.close()
            client.close()

    def _ensure_fresh(self, client: OllamaClient | None) -> dict[str, Any]:
        """Lazy refresh before a query. Never blocks on a large backlog."""
        info: dict[str, Any] = {"index_stale": False, "pending": 0}
        store = self.open_store()
        try:
            todo, orphans, model_changed = self._pending(store)
            info["pending"] = len(todo)
            if not todo and not orphans and not model_changed:
                return info
            if (
                model_changed
                or len(todo) > int(self.o["lazy_embed_limit"])
                or client is None
            ):
                info["index_stale"] = True
                info["hint"] = (
                    "run docs_reindex to bring the embedding index up to date"
                )
                return info
            batch = int(self.o["embed_batch"])
            maxc = int(self.o["embed_max_chars"])
            for i in range(0, len(todo), batch):
                part = todo[i : i + batch]
                vecs = client.embed([it["embed_text"][:maxc] for it in part])
                for it, v in zip(part, vecs):
                    store.put(it["key"], it["corpus"], it["hash"], v)
            if orphans:
                store.delete(orphans)
            store.set_meta("embed_model", self.o["embed_model"])
            store.set_meta("updated_at", str(time.time()))
            store.commit()
            self._vectors = None
            self._matrix = None
            info["refreshed"] = len(todo)
            info["pending"] = 0
            return info
        finally:
            store.close()

    # ------------------------------------------------------------ retrieval

    def _load_vectors(self) -> dict[str, Any]:
        try:
            st = self.store_path.stat()
            stamp = (st.st_mtime, st.st_size)
        except OSError:
            return {}
        if self._vectors is None or self._vec_stamp != stamp:
            store = self.open_store()
            try:
                self._vectors = store.vectors()
            finally:
                store.close()
            self._vec_stamp = stamp
            self._matrix = None
        return self._vectors

    def _cosines(self, qvec: list[float]) -> dict[str, float]:
        vecs = self._load_vectors()
        if not vecs:
            return {}
        if np is not None:
            if self._matrix is None:
                self._matrix_keys = list(vecs)
                self._matrix = np.vstack(
                    [
                        np.frombuffer(vecs[k].tobytes(), dtype=np.float32)
                        for k in self._matrix_keys
                    ]
                )
            q = np.asarray(qvec, dtype=np.float32)
            if self._matrix.shape[1] != q.shape[0]:
                return {}
            sims = self._matrix @ q
            return {k: float(s) for k, s in zip(self._matrix_keys or [], sims)}
        out: dict[str, float] = {}
        dim = len(qvec)
        for k, v in vecs.items():
            if len(v) == dim:
                out[k] = _dot(v, qvec)
        return out

    def rank(
        self,
        query: str,
        qvec: list[float] | None,
        corpus: str | None,
        include_stale: bool,
        limit: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        items = self.items()
        toks = tokenize(query)
        bm_scores: dict[int, float] = {}
        if toks:
            for i, s in self.bm25().search(toks, limit=max(1, len(items))):
                bm_scores[i] = s
        cos = self._cosines(qvec) if qvec is not None else {}
        wb, we = float(self.o["hybrid"]["bm25"]), float(self.o["hybrid"]["embed"])
        total_w = wb + we
        sat = float(self.o["bm25_saturation"])
        scored: list[tuple[float, int, float, float]] = []
        pool = 0
        for i, it in enumerate(items):
            if it["hidden"] and not include_stale:
                continue
            if corpus and not (
                it["corpus"] == corpus or it["corpus"].startswith(corpus + ":")
            ):
                continue
            pool += 1
            b = bm_scores.get(i, 0.0)
            b_norm = b / (b + sat) if b > 0 else 0.0
            e = max(0.0, cos.get(it["key"], 0.0))
            score = (wb * b_norm + we * e) / total_w
            if score > 0:
                scored.append((score, i, b, e))
        scored.sort(key=lambda t: (-t[0], t[1]))
        hits = []
        for score, i, b, e in scored[: max(1, limit)]:
            it = items[i]
            hits.append(
                {
                    **it,
                    "score": round(score, 4),
                    "score_bm25": round(b, 3),
                    "score_embed": round(e, 4),
                }
            )
        meta = {
            "searched_chunks": pool,
            "embedded_chunks": len(self._load_vectors()),
            "total_items": len(items),
            "hybrid": {"bm25": wb, "embed": we},
        }
        return hits, meta

    # ------------------------------------------------------------ public ops

    def _query_vec(self, client: OllamaClient, query: str) -> list[float]:
        return client.embed([self.o["embed_query_prefix"] + query])[0]

    def semantic_search(
        self, query: str, limit: int, corpus: str | None, include_stale: bool
    ) -> dict[str, Any]:
        client = OllamaClient(self.o)
        try:
            fresh = self._ensure_fresh(client)
            qvec = self._query_vec(client, query)
        finally:
            client.close()
        hits, meta = self.rank(
            query, qvec, corpus, include_stale, min(max(limit, 1), 100)
        )
        status_hidden = sum(1 for it in self.items() if it["hidden"])
        return {
            "query": query,
            "hits": [_public_hit(h, with_snippet=True) for h in hits],
            **meta,
            "hidden_chunks": 0 if include_stale else status_hidden,
            "stale_index": fresh["index_stale"],
            **({"index_hint": fresh["hint"]} if fresh.get("hint") else {}),
            "pending_embeddings": fresh["pending"],
        }

    def ask(
        self,
        question: str,
        top_k: int | None,
        corpus: str | None,
        include_stale: bool,
        strict: bool,
        model: str | None,
    ) -> dict[str, Any]:
        t0 = time.time()
        k = int(top_k or self.o["top_k"])
        client = OllamaClient(self.o)
        try:
            fresh = self._ensure_fresh(client)
            qvec = self._query_vec(client, question)
            hits, meta = self.rank(question, qvec, corpus, include_stale, k)
            best = hits[0]["score"] if hits else 0.0
            min_score = float(self.o["min_score"])
            retrieval = {
                k2: v
                for k2, v in meta.items()
                if k2 in ("searched_chunks", "embedded_chunks", "hybrid")
            }
            base = {
                "question": question,
                "retrieval": retrieval,
                "stale_index": fresh["index_stale"],
                "min_score": min_score,
                "best_score": best,
            }
            if strict and (not hits or best < min_score):
                return {
                    "error": {
                        "category": "insufficient_evidence",
                        "message": f"best retrieval score {best} is below min_score {min_score}; the generator was not called",
                        "retryable": False,
                        "hint": "rephrase the question, pass strict=false to force an answer, or the documentation does not cover it",
                    },
                    **base,
                    "nearest": [_public_hit(h, with_snippet=True) for h in hits[:5]],
                    "latency_ms": int((time.time() - t0) * 1000),
                }
            used_model = model or self.o["chat_model"]
            sources = _number_sources(hits)
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _user_prompt(
                        question, hits, int(self.o["source_max_chars"])
                    ),
                },
            ]
            answer = (
                client.chat(messages, used_model) if hits else "brak w dokumentacji"
            )
        finally:
            client.close()
        cites = parse_citations(answer)
        cited = sorted({n for grp in cites for n in grp})
        valid = all(1 <= n <= len(sources) for n in cited)
        no_answer = bool(_NO_ANSWER.search(answer))
        uncited = uncited_sentences(answer)
        grounded = (
            bool(hits)
            and valid
            and (bool(cited) or no_answer)
            and len(uncited) <= max(1, len(_sentences(answer)) // 4)
        )
        above = sum(1 for h in hits if h["score"] >= min_score)
        margin = float(self.o["confidence_margin"])
        if not grounded or no_answer:
            confidence = "low"
        elif best >= min_score + margin and above >= 2:
            confidence = "high"
        elif best >= min_score:
            confidence = "medium"
        else:
            confidence = "low"
        resolver = vf.Resolver(
            self.root, list(self.idx.cfg["docs"].get("evidence_roots") or [])
        )
        for s, h in zip(sources, hits):
            ev = _check_evidence(resolver, h.get("evidence") or [])
            s.update(ev)
        return {
            **base,
            "answer": answer,
            "grounded": grounded,
            "confidence": confidence,
            "sources": sources,
            "cited": cited,
            "citations_valid": valid,
            "uncited_sentences": uncited,
            "no_answer": no_answer,
            "model": used_model,
            "latency_ms": int((time.time() - t0) * 1000),
            **({"index_hint": fresh["hint"]} if fresh.get("hint") else {}),
        }

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "configured": True,
            "base_url": self.o["base_url"],
            "embed_model": self.o["embed_model"],
            "chat_model": self.o["chat_model"],
            "corpora": self.o["corpora"],
            "min_score": self.o["min_score"],
            "hybrid": self.o["hybrid"],
            "numpy": np is not None,
        }
        store_exists = self.store_path.is_file()
        out["index"] = {
            "path": _rel_or_abs(self.store_path, self.root),
            "exists": store_exists,
        }
        if store_exists:
            store = self.open_store()
            try:
                out["index"].update(
                    chunks=store.count(),
                    embed_model=store.meta("embed_model"),
                    age_seconds=(
                        round(time.time() - float(store.meta("updated_at") or 0), 1)
                        if store.meta("updated_at")
                        else None
                    ),
                    pending=len(self._pending(store)[0]),
                )
            finally:
                store.close()
        client = OllamaClient(self.o)
        try:
            names = client.tags()
            out["available"] = True
            out["models_present"] = {
                "embed_model": _has_model(names, self.o["embed_model"]),
                "chat_model": _has_model(names, self.o["chat_model"]),
            }
            out["installed_models"] = len(names)
        except SpecError as e:
            out["available"] = False
            out["error"] = e.to_dict()["error"]
            out["models_present"] = {"embed_model": None, "chat_model": None}
        finally:
            client.close()
        return out


def _rel_or_abs(p: Path, root: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return str(p)


def _has_model(names: list[str], want: str) -> bool:
    if want in names:
        return True
    if ":" not in want:
        return f"{want}:latest" in names
    return False


# ---------------------------------------------------------------- answer helpers


def _public_hit(h: dict[str, Any], with_snippet: bool = False) -> dict[str, Any]:
    out = {
        "corpus": h["corpus"],
        "kind": h["kind"],
        "file": h["file"],
        "heading": h["heading"],
        "path": h["path"],
        "line": h["line"],
        "line_end": h["line_end"],
        "status": h["status"],
        "score": h["score"],
        "score_bm25": h["score_bm25"],
        "score_embed": h["score_embed"],
    }
    if with_snippet:
        t = h["text"]
        out["snippet"] = t[:240] + ("…" if len(t) > 240 else "")
    return out


def _number_sources(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "n": i,
            "kind": h["kind"],
            "corpus": h["corpus"],
            "path": h["file"],
            "heading": h["path"] or h["heading"],
            "line_start": h["line"],
            "line_end": h["line_end"],
            "status": h["status"],
            "score": h["score"],
        }
        for i, h in enumerate(hits, start=1)
    ]


def _user_prompt(question: str, hits: list[dict[str, Any]], max_chars: int) -> str:
    blocks = []
    for i, h in enumerate(hits, start=1):
        where = f"{h['file']}" + (
            f", linie {h['line']}-{h['line_end']}"
            if h["line"] and h["line_end"]
            else (f", linia {h['line']}" if h["line"] else "")
        )
        text = h["text"] if len(h["text"]) <= max_chars else h["text"][:max_chars] + "…"
        blocks.append(
            f"[{i}] ({h['kind']}, {where}, nagłówek: {h['path'] or h['heading']}, status: {h['status'] or '-'})\n{text}"
        )
    return f"Pytanie: {question}\n\nŹródła:\n\n" + "\n\n".join(blocks)


def parse_citations(answer: str) -> list[list[int]]:
    return [
        [int(x) for x in re.split(r"[,;]\s*", m.group(1))]
        for m in _CITE.finditer(answer)
    ]


def _sentences(answer: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", answer)
    return [p.strip() for p in parts if p.strip()]


def uncited_sentences(answer: str) -> list[str]:
    out = []
    for s in _sentences(answer):
        core = re.sub(r"^[\-\*\d\.\)\s]+", "", s)
        if len(core) < 25 or _NO_ANSWER.search(s) or _CITE.search(s):
            continue
        out.append(s[:160])
    return out


def _check_evidence(
    resolver: vf.Resolver, entries: list[dict[str, Any]]
) -> dict[str, Any]:
    if not entries:
        return {"evidence_ok": None, "evidence_checked": 0}
    statuses: dict[str, int] = {}
    for e in entries[:5]:
        st = vf.check_entry(resolver, e)["status"]
        statuses[st] = statuses.get(st, 0) + 1
    ok = all(k == "ok" for k in statuses)
    return {
        "evidence_ok": ok,
        "evidence_checked": sum(statuses.values()),
        "evidence_statuses": statuses,
    }
