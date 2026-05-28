"""
selmakit/memory.py

SQLite FTS5 memory index with optional Ollama vector search and temporal decay.
Provides SqliteMemory — the selmakit wrapper that produces pydantic-ai tools.

Ported from memory_index.py (project-level) to make selmakit standalone.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import time
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

logger = logging.getLogger(__name__)

_CHUNK_SIZE           = 500
_HYBRID_VECTOR_WEIGHT = 0.5
_HYBRID_TEXT_WEIGHT   = 0.5
_DECAY_WEIGHT         = 0.3


# ════════════════════════════════════════════════════════════
# DATA
# ════════════════════════════════════════════════════════════

class SearchResult(BaseModel):
    path: str
    content: str
    score: float


# ════════════════════════════════════════════════════════════
# EMBEDDING PROVIDER
# ════════════════════════════════════════════════════════════

class EmbeddingProvider:
    def __init__(self, model: str, base_url: str):
        self._model = model
        self._base_url = base_url.rstrip("/")

    def embed(self, text: str) -> list[float] | None:
        url = f"{self._base_url}/embeddings"
        payload = json.dumps({"model": self._model, "input": text}).encode()
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())["data"][0]["embedding"]
        except Exception as e:
            logger.warning("Embedding failed | model=%s error=%s", self._model, e)
            return None


# ════════════════════════════════════════════════════════════
# MEMORY INDEX
# ════════════════════════════════════════════════════════════

class MemoryIndex:
    """
    SQLite FTS5 (+ optional vector) index for all memory files in a workspace.

    Memory files:  <workspace>/MEMORY.md  and  <workspace>/memory/*.md
    Database:      <workspace>/../memory.db
    """

    def __init__(
        self,
        workspace_dir: str | Path,
        *,
        vector_search: bool = False,
        embed_model: str = "nomic-embed-text",
        embed_base_url: str = "http://localhost:11434/v1",
        temporal_decay: bool = False,
        temporal_decay_rate: float = 0.01,
    ):
        self._workspace = Path(workspace_dir).resolve()
        db_path = self._workspace.parent / "memory.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._vector_search = vector_search
        self._embedder = (
            EmbeddingProvider(model=embed_model, base_url=embed_base_url)
            if vector_search else None
        )
        self._temporal_decay = temporal_decay
        self._decay_rate = temporal_decay_rate

    # ── Schema ───────────────────────────────────────────────

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY, hash TEXT NOT NULL, mtime REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
                USING fts5(path UNINDEXED, content, tokenize='unicode61')
            """)
            if self._vector_search:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS chunks_vec (
                        path TEXT NOT NULL, chunk_idx INTEGER NOT NULL,
                        embedding BLOB NOT NULL, PRIMARY KEY (path, chunk_idx)
                    )
                """)

    # ── Sync ─────────────────────────────────────────────────

    def sync(self) -> int:
        self.ensure_schema()
        disk_files = self._find_memory_files()
        disk_paths = {self._rel(p) for p in disk_files}
        reindexed = 0

        with self._connect() as conn:
            stored_paths = {row[0] for row in conn.execute("SELECT path FROM files")}
            for removed in stored_paths - disk_paths:
                conn.execute("DELETE FROM chunks_fts WHERE path = ?", (removed,))
                if self._vector_search:
                    conn.execute("DELETE FROM chunks_vec WHERE path = ?", (removed,))
                conn.execute("DELETE FROM files WHERE path = ?", (removed,))

            for abs_path in disk_files:
                rel = self._rel(abs_path)
                new_hash = self._hash(abs_path)
                row = conn.execute("SELECT hash FROM files WHERE path = ?", (rel,)).fetchone()
                if row and row[0] == new_hash:
                    continue

                content = abs_path.read_text(encoding="utf-8", errors="replace")
                chunks = _chunk_text(content)

                conn.execute("DELETE FROM chunks_fts WHERE path = ?", (rel,))
                conn.executemany(
                    "INSERT INTO chunks_fts(path, content) VALUES (?, ?)",
                    [(rel, ch) for ch in chunks],
                )

                if self._vector_search and self._embedder:
                    conn.execute("DELETE FROM chunks_vec WHERE path = ?", (rel,))
                    vec_rows = [
                        (rel, idx, json.dumps(vec).encode())
                        for idx, ch in enumerate(chunks)
                        if (vec := self._embedder.embed(ch)) is not None
                    ]
                    if vec_rows:
                        conn.executemany(
                            "INSERT INTO chunks_vec(path, chunk_idx, embedding) VALUES (?, ?, ?)",
                            vec_rows,
                        )

                conn.execute(
                    "INSERT OR REPLACE INTO files(path, hash, mtime) VALUES (?, ?, ?)",
                    (rel, new_hash, abs_path.stat().st_mtime),
                )
                reindexed += 1
                logger.info("Memory index: re-indexed | path=%s chunks=%d", rel, len(chunks))

        return reindexed

    # ── Search ───────────────────────────────────────────────

    def search(self, query: str, max_results: int = 10, min_score: float | None = None) -> list[SearchResult]:
        self.ensure_schema()
        fts_query = _build_fts_query(query)
        if not fts_query:
            return []
        mtime_by_path = self._load_mtimes() if self._temporal_decay else {}
        if self._vector_search and self._embedder:
            return self._hybrid_search(query, fts_query, max_results, min_score, mtime_by_path)
        return self._fts_search(fts_query, max_results, min_score, mtime_by_path)

    def _fts_search(self, fts_query, max_results, min_score, mtime_by_path) -> list[SearchResult]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT path, content, bm25(chunks_fts) AS score FROM chunks_fts "
                    "WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?",
                    (fts_query, max_results * 3 if self._temporal_decay else max_results),
                ).fetchall()
        except sqlite3.OperationalError as e:
            logger.warning("Memory search failed | query=%r error=%s", fts_query, e)
            return []

        results = []
        for row in rows:
            score = self._apply_decay(_normalise_bm25(row["score"]), row["path"], mtime_by_path)
            if min_score is not None and score < min_score:
                continue
            results.append(SearchResult(path=row["path"], content=row["content"], score=score))

        if self._temporal_decay:
            results.sort(key=lambda r: r.score, reverse=True)
            results = results[:max_results]
        return results

    def _hybrid_search(self, query, fts_query, max_results, min_score, mtime_by_path) -> list[SearchResult]:
        assert self._embedder is not None
        candidate_limit = max(max_results * 3, 30)
        try:
            with self._connect() as conn:
                fts_rows = conn.execute(
                    "SELECT path, content, bm25(chunks_fts) AS score FROM chunks_fts "
                    "WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?",
                    (fts_query, candidate_limit),
                ).fetchall()
        except sqlite3.OperationalError as e:
            logger.warning("Hybrid FTS stage failed | error=%s", e)
            return []

        if not fts_rows:
            return []

        query_vec = self._embedder.embed(query)
        if query_vec is None:
            return self._fts_search(fts_query, max_results, min_score, mtime_by_path)

        candidate_paths = list({row["path"] for row in fts_rows})
        stored: dict[tuple[str, int], list[float]] = {}
        try:
            with self._connect() as conn:
                placeholders = ",".join("?" * len(candidate_paths))
                vec_rows = conn.execute(
                    f"SELECT path, chunk_idx, embedding FROM chunks_vec WHERE path IN ({placeholders})",
                    candidate_paths,
                ).fetchall()
            for vr in vec_rows:
                stored[(vr["path"], vr["chunk_idx"])] = json.loads(vr["embedding"])
        except Exception as e:
            logger.warning("Loading embeddings failed | error=%s", e)

        path_counters: dict[str, int] = {}
        results = []
        for row in fts_rows:
            path = row["path"]
            bm25_norm = _normalise_bm25(row["score"])
            idx = path_counters.get(path, 0)
            path_counters[path] = idx + 1
            chunk_vec = stored.get((path, idx))
            if chunk_vec is not None:
                cos = _cosine_sim(query_vec, chunk_vec)
                base_score = _HYBRID_VECTOR_WEIGHT * cos + _HYBRID_TEXT_WEIGHT * bm25_norm
            else:
                base_score = bm25_norm
            score = self._apply_decay(base_score, path, mtime_by_path)
            if min_score is not None and score < min_score:
                continue
            results.append(SearchResult(path=path, content=row["content"], score=score))

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:max_results]

    # ── Write ────────────────────────────────────────────────

    def write(self, content: str) -> None:
        """Append a new entry to today's daily memory file."""
        memory_dir = self._workspace / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        today = date.today().isoformat()
        path = memory_dir / f"{today}.md"
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n\n{content.strip()}")

    # ── Internals ────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _find_memory_files(self) -> list[Path]:
        files: list[Path] = []
        root_mem = self._workspace / "MEMORY.md"
        if root_mem.exists():
            files.append(root_mem)
        mem_dir = self._workspace / "memory"
        if mem_dir.is_dir():
            files.extend(sorted(mem_dir.glob("*.md")))
        return files

    def _rel(self, path: Path) -> str:
        return str(path.relative_to(self._workspace))

    @staticmethod
    def _hash(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _load_mtimes(self) -> dict[str, float]:
        try:
            with self._connect() as conn:
                rows = conn.execute("SELECT path, mtime FROM files").fetchall()
            return {row["path"]: row["mtime"] for row in rows}
        except Exception:
            return {}

    def _apply_decay(self, score: float, path: str, mtime_by_path: dict[str, float]) -> float:
        if not self._temporal_decay:
            return score
        mtime = mtime_by_path.get(path)
        if mtime is None:
            return score
        age_days = max(0.0, (time.time() - mtime) / 86400.0)
        decay = math.exp(-self._decay_rate * age_days)
        return (1.0 - _DECAY_WEIGHT) * score + _DECAY_WEIGHT * decay


# ════════════════════════════════════════════════════════════
# TEXT HELPERS
# ════════════════════════════════════════════════════════════

def _chunk_text(text: str) -> list[str]:
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return []
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for para in paragraphs:
        if current and current_len + len(para) > _CHUNK_SIZE:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(para)
        current_len += len(para) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _build_fts_query(query: str) -> str:
    words = re.findall(r'\w+', query, re.UNICODE)
    if not words:
        return ""
    return " AND ".join(f'"{w}"' for w in words)


def _normalise_bm25(raw_score: float) -> float:
    return -raw_score / (1.0 + (-raw_score))


def _cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (norm_a * norm_b)))


# ════════════════════════════════════════════════════════════
# SQLITEMEMORY — pydantic-ai integration
# ════════════════════════════════════════════════════════════

_MEMORY_INSTRUCTIONS = (
    "Long-term memory is available across sessions:\n"
    "- Call `memory_search(query)` before answering questions where prior context "
    "(past decisions, user preferences, stored facts) might exist.\n"
    "- Call `memory_write(content)` to persist facts, decisions, or notes worth "
    "remembering across sessions. Keep entries concise and specific."
)


@dataclass
class SqliteMemory(AbstractCapability[Any]):
    """
    Long-term memory capability for selmakit agents.

    Registers `memory_search` and `memory_write` as function tools via
    `get_toolset()` and contributes usage instructions via `get_instructions()`.

    Pass to Agent:
        agent = Agent(
            state_dir=".selmakit",
            memory=SqliteMemory(workspace_dir=".selmakit/workspace"),
        )
    """

    workspace_dir: str = ""
    vector_search: bool = False
    embed_model: str = "nomic-embed-text"
    embed_base_url: str = "http://localhost:11434/v1"
    temporal_decay: bool = False
    temporal_decay_rate: float = 0.01

    def __post_init__(self) -> None:
        if not self.workspace_dir:
            raise ValueError("SqliteMemory.workspace_dir is required")
        self._index = MemoryIndex(
            self.workspace_dir,
            vector_search=self.vector_search,
            embed_model=self.embed_model,
            embed_base_url=self.embed_base_url,
            temporal_decay=self.temporal_decay,
            temporal_decay_rate=self.temporal_decay_rate,
        )

    def get_toolset(self) -> AgentToolset[Any] | None:
        index = self._index

        async def memory_search(query: str) -> str:
            """Search long-term memory for facts, past decisions, or notes relevant to the query."""
            index.sync()
            results = index.search(query, max_results=5)
            if not results:
                return "No relevant memories found."
            return "\n\n---\n\n".join(
                f"[{r.path}] (score: {r.score:.2f})\n{r.content}" for r in results
            )

        async def memory_write(content: str) -> str:
            """Store a new fact, note, or decision in long-term memory."""
            index.write(content)
            index.sync()
            return "Memory saved."

        return FunctionToolset(tools=[memory_search, memory_write])

    def get_instructions(self):
        return _MEMORY_INSTRUCTIONS
