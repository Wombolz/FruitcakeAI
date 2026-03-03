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


async def read_and_chunk(
    file_path: Path,
    metadata: Dict[str, Any],
    config: Dict[str, Any],
) -> List[Any]:
    """
    Read a file and split it into LlamaIndex TextNodes.

    Supported formats: PDF, DOCX, TXT (via LlamaIndex SimpleDirectoryReader).
    Chunking: SentenceSplitter with configurable size + overlap.

    Returns a list of TextNodes with metadata attached, ready for ainsert_nodes().
    """
    chunk_cfg = config.get("chunking", {})
    chunk_size = int(chunk_cfg.get("chunk_size", 900))
    chunk_overlap = int(chunk_cfg.get("chunk_overlap", 120))

    loop = asyncio.get_running_loop()

    def _load_and_chunk() -> List[Any]:
        from llama_index.core import SimpleDirectoryReader
        from llama_index.core.node_parser import SentenceSplitter

        # Load — SimpleDirectoryReader handles PDF, DOCX, TXT, HTML, etc.
        reader = SimpleDirectoryReader(input_files=[str(file_path)])
        documents = reader.load_data()

        # Attach user metadata and set ref_doc_id for later deletion
        doc_id = str(metadata.get("document_id", uuid.uuid4()))
        for doc in documents:
            doc.metadata.update(metadata)
            doc.doc_id = doc_id

        # Chunk
        splitter = SentenceSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        nodes = splitter.get_nodes_from_documents(documents)
        return nodes

    return await loop.run_in_executor(None, _load_and_chunk)
