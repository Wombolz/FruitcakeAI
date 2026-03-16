"""
FruitcakeAI v5 — Hybrid BM25 + vector retriever with RRF fusion.
Ported from v4 LlamaIndexService, extracted into its own module.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

_FUSION_MODE_ALIASES: Dict[str, List[str]] = {
    # Legacy label first for backward-compat, then newer llama-index label.
    "rrf": ["rrf", "reciprocal_rerank"],
    "reciprocal_rerank": ["reciprocal_rerank", "rrf"],
}


def _candidate_fusion_modes(configured_mode: str | None) -> List[str]:
    base = (configured_mode or "rrf").strip().lower()
    candidates = list(_FUSION_MODE_ALIASES.get(base, [base]))
    if "reciprocal_rerank" not in candidates:
        candidates.append("reciprocal_rerank")
    if "rrf" not in candidates:
        candidates.append("rrf")
    # Deduplicate while preserving order
    out: List[str] = []
    seen = set()
    for mode in candidates:
        if mode and mode not in seen:
            seen.add(mode)
            out.append(mode)
    return out


def build_hybrid_retriever(
    index: Any,
    config: Dict[str, Any],
    *,
    bm25_nodes: Optional[List[Any]] = None,
) -> Tuple[Any, List[Any]]:
    """
    Build the retrieval pipeline from a LlamaIndex VectorStoreIndex.

    Returns (retriever, postprocessors).

    Degrades gracefully:
    - If llama-index-retrievers-bm25 is missing → vector-only
    - If QueryFusionRetriever is missing → vector-only
    - If SentenceTransformerRerank is missing → no reranking
    """
    from llama_index.core.retrievers import VectorIndexRetriever

    ret_cfg = config.get("retrieval", {})
    vector_top_k = int(ret_cfg.get("vector_top_k", 40))
    bm25_top_k = int(ret_cfg.get("bm25_top_k", 40))
    configured_fusion_mode = str(ret_cfg.get("fusion", "rrf"))

    vector_retriever = VectorIndexRetriever(
        index=index, similarity_top_k=vector_top_k
    )

    # ── BM25 retriever (optional) ─────────────────────────────────────────────
    # BM25 requires at least one document to build its corpus index.
    # At startup with an empty DB this legitimately fails — skip it cleanly
    # and let the service rebuild the retriever after the first ingest.
    bm25_retriever = None
    try:
        from llama_index.retrievers.bm25 import BM25Retriever

        if bm25_nodes:
            bm25_retriever = BM25Retriever.from_defaults(
                nodes=bm25_nodes,
                similarity_top_k=bm25_top_k,
            )
            log.info("BM25 retriever initialized from persisted chunk nodes", node_count=len(bm25_nodes))
        else:
            docstore = index.docstore
            raw_docs = getattr(docstore, "_docs", None)
            if isinstance(raw_docs, dict):
                has_docs = len(raw_docs) > 0
            else:
                has_docs = bool(raw_docs)

            if not has_docs:
                log.info("BM25 skipped at startup (no documents yet) — will activate after first ingest")
            else:
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    bm25_retriever = BM25Retriever.from_defaults(
                        docstore=docstore,
                        similarity_top_k=bm25_top_k,
                    )
                log.info("BM25 retriever initialized from index docstore")
    except ValueError as e:
        # Seen when a docstore object exists but has zero effective corpus rows.
        if "empty sequence" in str(e).lower():
            log.info("BM25 skipped at startup (empty corpus) — using vector-only until ingest")
        else:
            log.warning("BM25 not available, using vector-only: %s", e)
    except Exception as e:
        log.warning("BM25 not available, using vector-only: %s", e)

    # ── Hybrid fusion (RRF) ───────────────────────────────────────────────────
    retriever: Any = vector_retriever
    if bm25_retriever:
        try:
            from llama_index.core.retrievers import QueryFusionRetriever

            fusion_retriever = None
            mode_candidates = _candidate_fusion_modes(configured_fusion_mode)
            for mode in mode_candidates:
                try:
                    fusion_retriever = QueryFusionRetriever(
                        retrievers=[vector_retriever, bm25_retriever],
                        similarity_top_k=vector_top_k,
                        num_queries=1,
                        mode=mode,
                    )
                    log.info("Hybrid retriever initialized", fusion_mode=mode)
                    break
                except ValueError as e:
                    # LlamaIndex mode naming has changed across versions.
                    if "invalid fusion mode" in str(e).lower():
                        continue
                    raise
            if fusion_retriever is None:
                log.warning(
                    "No supported fusion mode accepted by QueryFusionRetriever; using vector-only",
                    configured_mode=configured_fusion_mode,
                    tried_modes=mode_candidates,
                )
            else:
                retriever = fusion_retriever
        except Exception as e:
            log.warning("QueryFusionRetriever not available, falling back to vector: %s", e)

    # ── Reranker (disabled by default — heavy model) ──────────────────────────
    postprocessors: List[Any] = []
    if config.get("rerank_enabled", False):
        rerank_top_n = int(ret_cfg.get("rerank_top_n", 10))
        rerank_model = ret_cfg.get(
            "rerank_model", "cross-encoder/ms-marco-MiniLM-L-6-v2"
        )
        try:
            from llama_index.core.postprocessor import SentenceTransformerRerank

            postprocessors.append(
                SentenceTransformerRerank(top_n=rerank_top_n, model=rerank_model)
            )
            log.info("Reranker enabled: %s", rerank_model)
        except Exception as e:
            log.warning("SentenceTransformerRerank not available: %s", e)

    return retriever, postprocessors
