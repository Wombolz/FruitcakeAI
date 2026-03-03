"""
FruitcakeAI v5 — RAG Service
LlamaIndex + pgvector, lazy-initialized singleton.
Ported and cleaned up from v4 LlamaIndexService.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
import structlog
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

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        """Initialize all LlamaIndex components. Called once at app startup."""
        if self._loaded:
            return

        try:
            self._load_config()
            # Heavy model loading runs in a thread so it doesn't block the event loop
            await asyncio.get_running_loop().run_in_executor(None, self._init_sync)
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
        self._embed_model = HuggingFaceEmbedding(model_name=model_name)
        Settings.embed_model = self._embed_model
        log.info("Embedding model loaded", model=model_name)

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
        self._retriever, self._node_postprocessors = build_hybrid_retriever(
            self._index, self._config
        )

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

            if filter_list:
                meta_filters = MetadataFilters(
                    filters=filter_list, condition=FilterCondition.OR
                )
                # Apply to VectorIndexRetriever directly or via its inner retrievers
                for r in [self._retriever] + list(
                    getattr(self._retriever, "_retrievers", [])
                ):
                    if hasattr(r, "_filters"):
                        r._filters = meta_filters

            loop = asyncio.get_running_loop()
            nodes = await loop.run_in_executor(
                None, lambda: self._retriever.retrieve(query_str)
            )

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
        await asyncio.get_running_loop().run_in_executor(None, self._rebuild_retriever)

        return len(nodes)

    def _rebuild_retriever(self) -> None:
        """Rebuild hybrid retriever — called after each ingest so BM25 corpus stays current."""
        from app.rag.retriever import build_hybrid_retriever

        self._retriever, self._node_postprocessors = build_hybrid_retriever(
            self._index, self._config
        )

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
        }
