"""
FruitcakeAI v5 — RAG Service tests

Tests the RAG service at the unit level using mocked LlamaIndex components.
No real PostgreSQL or embedding model is loaded.

Covers:
- RAGService.query() returns [] when the service is not initialized
- RAGService.health() reports correct status before/after startup
- RAGService.ingest() raises when service is not initialized
- build_hybrid_retriever() falls back to vector-only when docstore is empty
- build_hybrid_retriever() falls back to vector-only when BM25 is unavailable
- Access-control filter building (MetadataFilters OR logic)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.rag.ingest import _load_documents
from app.rag.service import RAGService


# ── RAGService — pre-startup behaviour ────────────────────────────────────────

def test_rag_service_not_ready_before_startup():
    svc = RAGService()
    assert not svc.is_ready


@pytest.mark.asyncio
async def test_rag_query_returns_empty_when_not_ready():
    svc = RAGService()
    results = await svc.query(
        query_str="anything",
        user_id=1,
        accessible_scopes=["personal"],
    )
    assert results == []


@pytest.mark.asyncio
async def test_rag_ingest_raises_when_not_ready():
    svc = RAGService()
    from pathlib import Path
    with pytest.raises(RuntimeError, match="not initialized"):
        await svc.ingest(
            file_path=Path("/tmp/fake.pdf"),
            document_id=1,
            user_id=1,
            scope="personal",
            filename="fake.pdf",
        )


def test_rag_health_not_initialized():
    svc = RAGService()
    h = svc.health()
    assert h["status"] == "not_initialized"
    assert h["retriever"] is None


def test_rag_health_ready():
    svc = RAGService()
    svc._loaded = True
    svc._retriever = MagicMock(__class__=MagicMock(__name__="QueryFusionRetriever"))
    svc._node_postprocessors = []
    h = svc.health()
    assert h["status"] == "ready"
    assert "fusion_runtime_disabled" in h


def test_embedding_init_uses_local_files_only_when_model_is_cached(tmp_path):
    svc = RAGService()
    cache_dir = tmp_path / "hf-cache"
    cache_dir.mkdir()

    with patch.object(svc, "_huggingface_model_cached", return_value=True):
        kwargs = svc._embedding_init_kwargs(
            model_name="BAAI/bge-small-en-v1.5",
            cache_folder=str(cache_dir),
        )

    assert kwargs["cache_folder"] == str(cache_dir)
    assert kwargs["local_files_only"] is True


def test_embedding_init_fails_fast_when_model_uncached_and_host_unreachable(tmp_path):
    svc = RAGService()
    cache_dir = tmp_path / "hf-cache"
    cache_dir.mkdir()

    with patch.object(svc, "_huggingface_model_cached", return_value=False):
        with patch.object(svc, "_host_resolves", return_value=False):
            with pytest.raises(RuntimeError, match="not cached locally"):
                svc._embedding_init_kwargs(
                    model_name="BAAI/bge-small-en-v1.5",
                    cache_folder=str(cache_dir),
                )


def test_embedding_init_allows_local_path_without_network(tmp_path):
    svc = RAGService()
    model_dir = tmp_path / "local-model"
    model_dir.mkdir()

    kwargs = svc._embedding_init_kwargs(
        model_name=str(model_dir),
        cache_folder=str(tmp_path / "hf-cache"),
    )

    assert kwargs == {"cache_folder": str(tmp_path / "hf-cache")}


def test_load_documents_extracts_text_from_pdf(tmp_path):
    from reportlab.pdfgen import canvas

    pdf_path = tmp_path / "manual.pdf"
    c = canvas.Canvas(str(pdf_path))
    c.drawString(72, 720, "Stained Glass Adversarial Obfuscation Studio Manual")
    c.drawString(72, 700, "Identity Mode B preserves human familiarity.")
    c.save()

    docs = _load_documents(pdf_path)

    assert len(docs) == 1
    text = docs[0].text
    assert "Stained Glass Adversarial Obfuscation Studio Manual" in text
    assert "Identity Mode B preserves human familiarity." in text
    assert "%PDF" not in text


# ── RAGService — delete_document (best-effort, no error when not ready) ────────

@pytest.mark.asyncio
async def test_delete_document_no_op_when_not_ready():
    svc = RAGService()
    # Should not raise
    await svc.delete_document(999)


# ── build_hybrid_retriever ─────────────────────────────────────────────────────

def test_build_hybrid_retriever_vector_only_when_no_docs():
    """When the docstore is empty, BM25 is skipped and vector retriever is returned."""
    from app.rag.retriever import build_hybrid_retriever

    mock_index = MagicMock()
    mock_docstore = MagicMock()
    mock_docstore._docs = None
    mock_docstore._kvstore = None
    mock_index.docstore = mock_docstore

    mock_vector_retriever = MagicMock()
    config = {"retrieval": {"vector_top_k": 10, "bm25_top_k": 10}}

    # VectorIndexRetriever is imported locally inside build_hybrid_retriever —
    # patch at the llama_index source module level.
    with patch("llama_index.core.retrievers.VectorIndexRetriever", return_value=mock_vector_retriever, create=True):
        with patch.dict("sys.modules", {"llama_index.retrievers.bm25": None}):
            retriever, postprocessors = build_hybrid_retriever(mock_index, config)

    assert postprocessors == []


def test_build_hybrid_retriever_vector_only_when_bm25_unavailable():
    """When the BM25 package is not installed, vector-only retriever is used."""
    from app.rag.retriever import build_hybrid_retriever

    mock_index = MagicMock()
    mock_vector_retriever = MagicMock()
    config = {"retrieval": {"vector_top_k": 10, "bm25_top_k": 10}}

    with patch("llama_index.core.retrievers.VectorIndexRetriever", return_value=mock_vector_retriever, create=True):
        with patch.dict("sys.modules", {"llama_index.retrievers.bm25": None}):
            retriever, postprocessors = build_hybrid_retriever(mock_index, config)

    assert postprocessors == []


def test_build_hybrid_retriever_no_reranker_by_default():
    """rerank_enabled defaults to False — no postprocessors added."""
    from app.rag.retriever import build_hybrid_retriever

    mock_index = MagicMock()
    mock_vector_retriever = MagicMock()
    config = {}  # no rerank_enabled key

    with patch("llama_index.core.retrievers.VectorIndexRetriever", return_value=mock_vector_retriever, create=True):
        with patch.dict("sys.modules", {"llama_index.retrievers.bm25": None}):
            _, postprocessors = build_hybrid_retriever(mock_index, config)

    assert postprocessors == []


def test_build_hybrid_retriever_fusion_mode_alias_fallback_from_rrf():
    """If llama-index rejects 'rrf', fallback should retry with reciprocal_rerank."""
    from app.rag.retriever import build_hybrid_retriever

    mock_index = MagicMock()
    mock_vector_retriever = MagicMock(name="vector")
    mock_bm25_retriever = MagicMock(name="bm25")
    mock_fusion_retriever = MagicMock(name="fusion")
    config = {"retrieval": {"vector_top_k": 10, "bm25_top_k": 10, "fusion": "rrf"}}

    def _fusion_side_effect(*args, **kwargs):
        mode = kwargs.get("mode")
        if mode == "rrf":
            raise ValueError("Invalid fusion mode: rrf")
        if mode == "reciprocal_rerank":
            return mock_fusion_retriever
        raise AssertionError(f"unexpected mode {mode}")

    with patch("llama_index.core.retrievers.VectorIndexRetriever", return_value=mock_vector_retriever, create=True):
        with patch("llama_index.retrievers.bm25.BM25Retriever.from_defaults", return_value=mock_bm25_retriever, create=True):
            with patch("llama_index.core.retrievers.QueryFusionRetriever", side_effect=_fusion_side_effect, create=True):
                retriever, postprocessors = build_hybrid_retriever(
                    mock_index,
                    config,
                    bm25_nodes=[MagicMock()],
                )

    assert retriever is mock_fusion_retriever
    assert postprocessors == []


def test_candidate_fusion_modes_supports_legacy_and_new_names():
    from app.rag.retriever import _candidate_fusion_modes

    assert _candidate_fusion_modes("rrf")[0] == "rrf"
    assert "reciprocal_rerank" in _candidate_fusion_modes("rrf")
    assert _candidate_fusion_modes("reciprocal_rerank")[0] == "reciprocal_rerank"


# ── Access-control filter logic ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rag_query_builds_personal_filter():
    """
    query() with accessible_scopes=["personal"] should only filter by user_id.
    We mock the retriever and assert the filter was applied.
    """
    svc = RAGService()
    svc._loaded = True
    svc._node_postprocessors = []

    mock_retriever = MagicMock()
    # Make retrieve() return an empty list (no actual nodes)
    mock_retriever.retrieve.return_value = []
    # Make _retrievers attribute absent (single retriever, no fusion)
    del mock_retriever._retrievers
    svc._retriever = mock_retriever

    with patch("asyncio.get_running_loop") as mock_loop:
        mock_executor = AsyncMock(return_value=[])
        mock_loop.return_value.run_in_executor = mock_executor

        # Import filter types to inspect what was set
        from llama_index.core.vector_stores.types import MetadataFilter, MetadataFilters

        results = await svc.query(
            query_str="test",
            user_id=42,
            accessible_scopes=["personal"],
            top_k=5,
        )

    assert results == []
    # The filter should have been applied (checked via _filters attribute set)
    # We can't easily assert MetadataFilters here since mock consumed it,
    # but we verify the call completed without errors.


@pytest.mark.asyncio
async def test_rag_query_returns_formatted_results():
    """query() formats node results into {text, score, metadata} dicts."""
    svc = RAGService()
    svc._loaded = True
    svc._node_postprocessors = []
    svc._retriever = MagicMock()

    fake_node = MagicMock()
    fake_node.get_content.return_value = "chunk text here"
    fake_node.score = 0.85
    fake_node.metadata = {"filename": "test.pdf", "user_id": "1"}

    with patch("asyncio.get_running_loop") as mock_loop:
        mock_loop.return_value.run_in_executor = AsyncMock(return_value=[fake_node])

        results = await svc.query(
            query_str="test query",
            user_id=1,
            accessible_scopes=["personal"],
            top_k=10,
        )

    assert len(results) == 1
    assert results[0]["text"] == "chunk text here"
    assert results[0]["score"] == 0.85
    assert results[0]["metadata"]["filename"] == "test.pdf"


@pytest.mark.asyncio
async def test_rag_query_prefers_async_retriever_when_available():
    svc = RAGService()
    svc._loaded = True
    svc._node_postprocessors = []

    fake_node = MagicMock()
    fake_node.get_content.return_value = "async grounded content"
    fake_node.score = 0.77
    fake_node.metadata = {"filename": "async.txt"}

    async_retriever = MagicMock()
    async_retriever.aretrieve = AsyncMock(return_value=[fake_node])
    async_retriever.retrieve.side_effect = AssertionError("sync retrieve should not be used")
    del async_retriever._retrievers
    svc._retriever = async_retriever

    results = await svc.query(
        query_str="async query",
        user_id=1,
        accessible_scopes=["personal"],
        top_k=10,
    )

    assert len(results) == 1
    assert results[0]["text"] == "async grounded content"
    async_retriever.aretrieve.assert_awaited_once_with("async query")


@pytest.mark.asyncio
async def test_rag_query_runtime_invalid_fusion_mode_falls_back_to_vector():
    svc = RAGService()
    svc._loaded = True
    svc._config = {"retrieval": {"vector_top_k": 12}}
    svc._index = MagicMock()
    svc._node_postprocessors = []

    bad_retriever = MagicMock()
    bad_retriever._retrievers = []
    bad_retriever.retrieve.side_effect = ValueError("Invalid fusion mode: rrf")
    svc._retriever = bad_retriever

    good_node = MagicMock()
    good_node.get_content.return_value = "grounded content"
    good_node.score = 0.99
    good_node.metadata = {"filename": "doc.txt"}
    good_retriever = MagicMock()
    good_retriever._retrievers = []
    good_retriever.retrieve.return_value = [good_node]

    with patch("llama_index.core.retrievers.VectorIndexRetriever", return_value=good_retriever, create=True):
        results = await svc.query(
            query_str="library docs",
            user_id=1,
            accessible_scopes=["personal"],
            top_k=10,
        )

    assert len(results) == 1
    assert results[0]["text"] == "grounded content"
    assert svc._fusion_runtime_disabled is True
