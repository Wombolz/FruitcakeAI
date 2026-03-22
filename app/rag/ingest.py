"""
FruitcakeAI v5 — Document ingestion pipeline.
Reads a file, splits into chunks, and returns LlamaIndex TextNodes
ready to be inserted into the vector store.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, Dict, List

from app.rag.extractor import DocumentExtractor


def _load_documents(file_path: Path) -> List[Any]:
    from llama_index.core import Document

    extractor = DocumentExtractor()
    _, text = extractor.extract(file_path)
    return [Document(text=text)]


async def chunk_and_index(
    text: str,
    metadata: Dict[str, Any],
    config: Dict[str, Any],
) -> List[Any]:
    """
    Split pre-extracted text into LlamaIndex TextNodes ready for ainsert_nodes().
    """
    chunk_cfg = config.get("chunking", {})
    chunk_size = int(chunk_cfg.get("chunk_size", 900))
    chunk_overlap = int(chunk_cfg.get("chunk_overlap", 120))

    loop = asyncio.get_running_loop()

    def _chunk() -> List[Any]:
        from llama_index.core import Document
        from llama_index.core.node_parser import SentenceSplitter

        doc_id = str(metadata.get("document_id", uuid.uuid4()))
        document = Document(text=text, metadata=dict(metadata))
        document.doc_id = doc_id
        splitter = SentenceSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        return splitter.get_nodes_from_documents([document])

    return await loop.run_in_executor(None, _chunk)


async def read_and_chunk(
    file_path: Path,
    metadata: Dict[str, Any],
    config: Dict[str, Any],
) -> List[Any]:
    """
    Read a file and split it into LlamaIndex TextNodes.

    Supported formats: PDF, DOCX, TXT (via LlamaIndex SimpleDirectoryReader).
    Returns a list of TextNodes with metadata attached, ready for ainsert_nodes().
    """
    loop = asyncio.get_running_loop()
    extractor = DocumentExtractor()

    def _extract_text() -> str:
        _, extracted_text = extractor.extract(file_path)
        return extracted_text

    extracted_text = await loop.run_in_executor(None, _extract_text)
    return await chunk_and_index(extracted_text, metadata, config)
