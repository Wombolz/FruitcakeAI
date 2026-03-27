"""
FruitcakeAI v5 — RAG Service
LlamaIndex + pgvector, lazy-initialized singleton.
Ported and cleaned up from v4 LlamaIndexService.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import socket
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
import structlog
from sqlalchemy import text
from sqlalchemy.engine import make_url

log = structlog.get_logger(__name__)

# Module-level singleton — initialized once at app startup
_service: Optional["RAGService"] = None


def get_rag_service() -> "RAGService":
    global _service
    if _service is None:
        _service = RAGService()
    return _service


class RAGService:
    """
    LlamaIndex RAG service with hybrid retrieval (vector + BM25 + RRF).

    Documents are stored in a LlamaIndex-managed table (`llamaindex_nodes`)
    separate from our own `documents` metadata table.
    """

    def __init__(self) -> None:
        self._loaded = False
        self._index = None
        self._retriever = None
        self._embed_model = None
        self._node_postprocessors: List[Any] = []
        self._config: Dict[str, Any] = {}
        self._bm25_node_count: int = 0
        self._bm25_source_table: Optional[str] = None
        self._fusion_runtime_disabled: bool = False

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        """Initialize all LlamaIndex components. Called once at app startup."""
        if self._loaded:
            return

        try:
            self._load_config()
            # Heavy model loading runs in a thread so it doesn't block the event loop
            await asyncio.get_running_loop().run_in_executor(None, self._init_sync)
            await self._rebuild_retriever_async()
            self._loaded = True
            log.info("RAG service ready")
        except Exception as e:
            log.error("RAG service failed to start", error=str(e), exc_info=True)
            self._loaded = False

    def _load_config(self) -> None:
        config_path = Path("config/rag_config.yaml")
        if config_path.exists():
            self._config = yaml.safe_load(config_path.read_text()) or {}

    def _init_sync(self) -> None:
        """
        Blocking initialization — embedding model + pgvector setup.
        Run inside run_in_executor so startup doesn't stall the event loop.
        """
        from llama_index.core import Settings, VectorStoreIndex, StorageContext
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding
        from llama_index.vector_stores.postgres import PGVectorStore
        from app.config import settings as app_settings
        from app.rag.retriever import build_hybrid_retriever

        # Tell LlamaIndex not to use its own LLM — we drive generation via LiteLLM
        Settings.llm = None

        # ── Embedding model ───────────────────────────────────────────────────
        model_name = (
            self._config.get("embedding", {}).get("model_name")
            or app_settings.embedding_model
        )
        cache_folder = self._embedding_cache_folder(app_settings)
        embedding_kwargs = self._embedding_init_kwargs(
            model_name=model_name,
            cache_folder=cache_folder,
        )
        self._embed_model = HuggingFaceEmbedding(model_name=model_name, **embedding_kwargs)
        Settings.embed_model = self._embed_model
        log.info(
            "Embedding model loaded",
            model=model_name,
            cache_folder=cache_folder,
            local_files_only=bool(embedding_kwargs.get("local_files_only")),
        )

        # ── pgvector store ────────────────────────────────────────────────────
        # from_params() avoids the connection-string parsing bugs in v4
        db_url = make_url(app_settings.database_url)
        vs_cfg = self._config.get("vector_store", {})

        vector_store = PGVectorStore.from_params(
            host=db_url.host or "localhost",
            port=db_url.port or 5432,
            user=db_url.username or "fruitcake",
            password=str(db_url.password or ""),
            database=db_url.database or "fruitcake_v5",
            table_name=vs_cfg.get("table_name", "llamaindex_nodes"),
            embed_dim=int(vs_cfg.get("embed_dim", app_settings.embedding_dimension)),
        )

        # ── Index + retriever ─────────────────────────────────────────────────
        storage_ctx = StorageContext.from_defaults(vector_store=vector_store)
        self._index = VectorStoreIndex.from_vector_store(
            vector_store, storage_context=storage_ctx
        )
        # Hybrid retriever is finalized asynchronously in startup()
        # so BM25 can be built from persisted chunk rows.
        self._retriever, self._node_postprocessors = build_hybrid_retriever(self._index, self._config)

    @property
    def is_ready(self) -> bool:
        return self._loaded

    # ── Query ─────────────────────────────────────────────────────────────────

    async def query(
        self,
        query_str: str,
        user_id: int,
        accessible_scopes: List[str],
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Retrieve relevant chunks with user-scoped access control.

        accessible_scopes: ["personal"] = own docs only,
                           ["personal", "family", "shared"] = everything visible
        """
        if not self._loaded:
            log.warning("RAG not ready — returning empty results")
            return []

        try:
            from llama_index.core.vector_stores.types import (
                MetadataFilter,
                MetadataFilters,
                FilterCondition,
            )

            # Build OR filter so users only see documents they own or that are shared
            filter_list: List[MetadataFilter] = []
            if "personal" in accessible_scopes:
                filter_list.append(MetadataFilter(key="user_id", value=str(user_id)))
            for scope in ("family", "shared"):
                if scope in accessible_scopes:
                    filter_list.append(MetadataFilter(key="scope", value=scope))

            meta_filters = None
            if filter_list:
                meta_filters = MetadataFilters(
                    filters=filter_list, condition=FilterCondition.OR
                )
                self._apply_metadata_filters(meta_filters)

            try:
                nodes = await self._retrieve_nodes(query_str)
            except ValueError as e:
                if "invalid fusion mode" not in str(e).lower():
                    raise
                log.warning("RAG fusion mode failed at retrieve-time; switching to vector-only fallback", error=str(e))
                self._swap_to_vector_only_retriever()
                if meta_filters is not None:
                    self._apply_metadata_filters(meta_filters)
                nodes = await self._retrieve_nodes(query_str)

            for pp in self._node_postprocessors:
                nodes = pp.postprocess_nodes(nodes, query_str=query_str)

            return [
                {
                    "text": node.get_content(),
                    "score": round(node.score or 0.0, 4),
                    "metadata": node.metadata or {},
                }
                for node in nodes[:top_k]
            ]

        except Exception as e:
            log.error("RAG query failed", query=query_str, error=str(e), exc_info=True)
            return []

    async def _retrieve_nodes(self, query_str: str) -> List[Any]:
        aretrieve = getattr(self._retriever, "aretrieve", None)
        if callable(aretrieve):
            maybe_result = aretrieve(query_str)
            if inspect.isawaitable(maybe_result):
                return await maybe_result

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self._retriever.retrieve(query_str)
        )

    def _embedding_cache_folder(self, app_settings: Any) -> str:
        configured = (
            self._config.get("embedding", {}).get("cache_folder")
            or getattr(app_settings, "embedding_cache_dir", "")
            or "./storage/model_cache/huggingface"
        )
        cache_folder = str(Path(configured).expanduser())
        Path(cache_folder).mkdir(parents=True, exist_ok=True)
        return cache_folder

    def _embedding_init_kwargs(self, *, model_name: str, cache_folder: str) -> Dict[str, Any]:
        if self._is_local_embedding_path(model_name):
            return {"cache_folder": cache_folder}
        if self._huggingface_model_cached(model_name, cache_folder=cache_folder):
            return {"cache_folder": cache_folder, "local_files_only": True}
        if not self._host_resolves("huggingface.co"):
            raise RuntimeError(
                "Embedding model is not cached locally and huggingface.co is unreachable. "
                f"Set EMBEDDING_MODEL to a local path or pre-warm the cache at {cache_folder}."
            )
        return {"cache_folder": cache_folder}

    def _is_local_embedding_path(self, model_name: str) -> bool:
        if not model_name:
            return False
        if model_name.startswith((".", "/", "~")):
            return True
        return Path(model_name).expanduser().exists()

    def _huggingface_model_cached(self, model_name: str, *, cache_folder: str) -> bool:
        try:
            from huggingface_hub import try_to_load_from_cache
            from huggingface_hub.file_download import _CACHED_NO_EXIST
        except Exception:
            return False
        for filename in ("modules.json", "config.json", "sentence_bert_config.json"):
            cached = try_to_load_from_cache(
                repo_id=model_name,
                filename=filename,
                cache_dir=cache_folder,
            )
            if cached and cached is not _CACHED_NO_EXIST:
                return True
        return False

    def _host_resolves(self, hostname: str) -> bool:
        try:
            socket.getaddrinfo(hostname, 443, proto=socket.IPPROTO_TCP)
            return True
        except OSError:
            return False

    def _apply_metadata_filters(self, meta_filters: Any) -> None:
        for r in [self._retriever] + list(getattr(self._retriever, "_retrievers", [])):
            if hasattr(r, "_filters"):
                r._filters = meta_filters

    def _swap_to_vector_only_retriever(self) -> None:
        from llama_index.core.retrievers import VectorIndexRetriever

        ret_cfg = self._config.get("retrieval", {})
        vector_top_k = int(ret_cfg.get("vector_top_k", 40))
        self._retriever = VectorIndexRetriever(index=self._index, similarity_top_k=vector_top_k)
        self._fusion_runtime_disabled = True
        log.info("RAG retriever switched to vector-only fallback", vector_top_k=vector_top_k)

    # ── Ingest ────────────────────────────────────────────────────────────────

    async def ingest(
        self,
        file_path: Path,
        document_id: int,
        user_id: int,
        scope: str,
        filename: str,
    ) -> int:
        """Chunk, embed, and store a document. Returns the node count."""
        if not self._loaded:
            raise RuntimeError("RAG service is not initialized")

        from app.rag.ingest import read_and_chunk

        nodes = await read_and_chunk(
            file_path=file_path,
            metadata={
                "document_id": str(document_id),
                "user_id": str(user_id),
                "scope": scope,
                "filename": filename,
            },
            config=self._config,
        )

        await self._index.ainsert_nodes(nodes)
        log.info("Document ingested", document_id=document_id, nodes=len(nodes))

        # Rebuild the retriever so BM25 now picks up the newly indexed documents
        await self._rebuild_retriever_async()

        return len(nodes)

    async def ingest_text(
        self,
        *,
        text: str,
        document_id: int,
        user_id: int,
        scope: str,
        filename: str,
    ) -> int:
        """Chunk, embed, and store pre-extracted text. Returns the node count."""
        if not self._loaded:
            raise RuntimeError("RAG service is not initialized")

        from app.rag.ingest import chunk_and_index

        nodes = await chunk_and_index(
            text=text,
            metadata={
                "document_id": str(document_id),
                "user_id": str(user_id),
                "scope": scope,
                "filename": filename,
            },
            config=self._config,
        )

        await self._index.ainsert_nodes(nodes)
        log.info("Document text ingested", document_id=document_id, nodes=len(nodes))
        await self._rebuild_retriever_async()
        return len(nodes)

    async def _rebuild_retriever_async(self) -> None:
        """Rebuild hybrid retriever using persisted chunk rows for BM25 corpus."""
        from app.rag.retriever import build_hybrid_retriever

        bm25_nodes = await self._load_bm25_nodes()
        loop = asyncio.get_running_loop()
        self._retriever, self._node_postprocessors = await loop.run_in_executor(
            None,
            lambda: build_hybrid_retriever(
                self._index,
                self._config,
                bm25_nodes=bm25_nodes,
            ),
        )
        self._bm25_node_count = len(bm25_nodes)

    async def _load_bm25_nodes(self, limit: int = 8000) -> List[Any]:
        """
        Build BM25 corpus nodes from persisted chunk rows in Postgres.
        This avoids dependence on index.docstore internals.
        """
        from llama_index.core.schema import TextNode
        from app.db.session import AsyncSessionLocal

        table = await self._resolve_chunk_table()
        if not table:
            self._bm25_source_table = None
            return []

        rows: List[Any] = []
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                text(
                    f"""
                    SELECT node_id, text, metadata_
                    FROM {table}
                    WHERE text IS NOT NULL
                      AND LENGTH(TRIM(text)) > 0
                    ORDER BY id DESC
                    LIMIT :limit
                    """
                ),
                {"limit": int(limit)},
            )
            rows = result.all()

        nodes: List[Any] = []
        for idx, row in enumerate(rows, start=1):
            node_id = row[0] or f"bm25-node-{idx}"
            content = row[1] or ""
            metadata = row[2] if isinstance(row[2], dict) else {}
            nodes.append(TextNode(id_=str(node_id), text=content, extra_info=metadata))

        self._bm25_source_table = table
        return nodes

    async def _resolve_chunk_table(self) -> Optional[str]:
        """
        Resolve the active vector chunk table name.
        Supports both configured table and PGVector's data_ prefixed table.
        """
        from app.db.session import AsyncSessionLocal

        configured = str(self._config.get("vector_store", {}).get("table_name", "document_chunks"))
        candidates = [configured, f"data_{configured}"]

        safe_candidates = []
        for name in candidates:
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                safe_candidates.append(name)
        if not safe_candidates:
            return None

        best_name: Optional[str] = None
        best_count = -1
        async with AsyncSessionLocal() as db:
            for name in safe_candidates:
                try:
                    count = (
                        await db.execute(text(f"SELECT COUNT(*) FROM {name}"))
                    ).scalar_one()
                except Exception:
                    continue
                if int(count) > best_count:
                    best_count = int(count)
                    best_name = name

        if best_count <= 0:
            return None
        return best_name

    # ── Delete ────────────────────────────────────────────────────────────────

    async def delete_document(self, document_id: int) -> None:
        """Remove all nodes for a document from the vector store."""
        if not self._loaded or not self._index:
            return
        try:
            await self._index.adelete_ref_doc(
                str(document_id), delete_from_docstore=True
            )
        except Exception as e:
            log.warning("Vector store delete failed", document_id=document_id, error=str(e))

    # ── Health ────────────────────────────────────────────────────────────────

    def health(self) -> Dict[str, Any]:
        return {
            "status": "ready" if self._loaded else "not_initialized",
            "retriever": type(self._retriever).__name__ if self._retriever else None,
            "postprocessors": len(self._node_postprocessors),
            "bm25_nodes": self._bm25_node_count,
            "bm25_source_table": self._bm25_source_table,
            "fusion_runtime_disabled": self._fusion_runtime_disabled,
        }
