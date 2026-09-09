"""Ingestion pipeline: route to the right chunker by doc type, embed
child chunks, store parent + child text, hybrid retrieval at query time.
fetch_document_bytes() is the seam where the storage abstraction layer
plugs in — everything else in this module is storage-provider-agnostic.
"""
import hashlib
import uuid

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.ingestion.chunkers import (
    Chunk,
    fixed_size_chunker,
    hierarchical_chunker,
    recursive_character_chunker,
    semantic_chunker,
)
from app.models.llm import Document, DocumentChunk

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
CHUNKER_VERSION = "v1"


async def fetch_document_bytes(document: Document) -> bytes:
    """The single connection point between the storage abstraction layer
    and ingestion. Swapping STORAGE_PROVIDER never requires touching
    anything below this function."""
    from app.storage.factory import get_storage_provider
    from app.storage.keys import build_storage_key_for_task

    provider = get_storage_provider()
    key = build_storage_key_for_task(document, str(document.tenant_id))
    chunks = [chunk async for chunk in provider.download(key)]
    return b"".join(chunks)


def _extract_text(raw_content: bytes, doc_type: str) -> str:
    """Decodes raw bytes into plain text before chunking. PDF/DOCX need
    real extraction (pypdf/python-docx) — wired here as the single seam
    for that, separate from the storage concern above."""
    if doc_type == "pdf":
        try:
            from pypdf import PdfReader
            import io
            reader = PdfReader(io.BytesIO(raw_content))
            return "\n\n".join(page.extract_text() or "" for page in reader.pages)
        except ImportError:
            raise RuntimeError("pypdf is required to ingest PDF documents — add it to pyproject.toml")
    if doc_type == "docx":
        try:
            import docx
            import io
            d = docx.Document(io.BytesIO(raw_content))
            return "\n\n".join(p.text for p in d.paragraphs)
        except ImportError:
            raise RuntimeError("python-docx is required to ingest DOCX documents — add it to pyproject.toml")
    # markdown/transcript/chat_log/long_form_prose/html/txt — treat as UTF-8 text
    return raw_content.decode("utf-8", errors="replace")


def select_chunker_name(doc_type: str) -> str:
    return {
        "markdown": "hierarchical",
        "html": "hierarchical",
        "docx": "hierarchical",
        "transcript": "fixed_size",
        "chat_log": "fixed_size",
        "long_form_prose": "semantic",
    }.get(doc_type, "recursive")


def chunk_document(content: str, doc_type: str, embedder=None) -> list[Chunk]:
    strategy = select_chunker_name(doc_type)
    if strategy == "hierarchical":
        return hierarchical_chunker(content)
    if strategy == "fixed_size":
        return fixed_size_chunker(content)
    if strategy == "semantic":
        if embedder is None:
            return recursive_character_chunker(content)  # graceful fallback if no embedder passed
        return semantic_chunker(content, embedder)
    return recursive_character_chunker(content)


def _vector_to_literal(embedding: list[float]) -> str:
    """Serializes a Python float list into pgvector's text literal
    format ('[0.1,0.2,...]'), cast with `::vector` in SQL. See the note
    in app/core/db/session.py for why this is explicit rather than
    relying on an asyncpg codec registration."""
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def _parse_vector_literal(raw: str) -> list[float]:
    return [float(x) for x in raw.strip("[]").split(",") if x]
    return " ".join(text.split()).lower()


def _content_hash(text: str) -> str:
    return hashlib.sha256(_normalize_for_hash(text).encode()).hexdigest()


async def embed_texts(texts: list[str], model: str = DEFAULT_EMBEDDING_MODEL) -> list[list[float]]:
    from langchain_openai import OpenAIEmbeddings

    from app.config import settings

    embedder = OpenAIEmbeddings(model=model, api_key=settings.OPENAI_API_KEY)
    # Batched in one call (not one call per chunk) — meaningful cost/
    # latency win, and langchain's aembed_documents already batches
    # internally for the provider's own request-size limits.
    return await embedder.aembed_documents(texts)


async def _find_existing_embedding(db: AsyncSession, content_hash: str, tenant_id: uuid.UUID,
                                    embedding_model: str) -> list[float] | None:
    """Dedup check (architecture doc section 3): if an identical chunk
    (by normalized content hash) already has an embedding under the SAME
    model anywhere in this tenant, reuse that vector instead of calling
    the embedding API again. Real savings on repeated boilerplate
    (headers, footers, disclaimers) across many documents in a KB."""
    result = await db.execute(
        sql_text(
            """
            SELECT embedding::text AS embedding_text FROM document_chunks
            WHERE tenant_id = :tenant_id AND content_hash = :content_hash
              AND embedding_model = :embedding_model AND is_current = true
            LIMIT 1
            """
        ),
        {"tenant_id": str(tenant_id), "content_hash": content_hash, "embedding_model": embedding_model},
    )
    row = result.first()
    return _parse_vector_literal(row.embedding_text) if row else None


async def ingest_document(db: AsyncSession, document: Document, raw_content: bytes | None = None) -> int:
    """raw_content is optional — when omitted (the normal path, triggered
    from app.tasks.llm_tasks after a successful storage+scan commit), it
    is fetched from the storage layer via fetch_document_bytes(). Tests
    can still pass raw_content directly to bypass storage entirely."""
    if raw_content is None:
        raw_content = await fetch_document_bytes(document)

    embedding_model = await _resolve_embedding_model(db, document)

    text_content = _extract_text(raw_content, document.doc_type)
    chunks = chunk_document(text_content, document.doc_type)
    if not chunks:
        document.status = "indexed"
        await db.flush()
        return 0

    # Dedup pass: reuse existing vectors for identical content, only
    # call the embedding API for genuinely new chunk text.
    hashes = [_content_hash(c.content) for c in chunks]
    embeddings: list[list[float] | None] = []
    to_embed_indices = []
    to_embed_texts = []
    for i, (chunk, h) in enumerate(zip(chunks, hashes, strict=True)):
        existing = await _find_existing_embedding(db, h, document.tenant_id, embedding_model)
        embeddings.append(existing)
        if existing is None:
            to_embed_indices.append(i)
            to_embed_texts.append(chunk.content)

    if to_embed_texts:
        fresh_embeddings = await embed_texts(to_embed_texts, embedding_model)
        for idx, embedding in zip(to_embed_indices, fresh_embeddings, strict=True):
            embeddings[idx] = embedding

    for chunk, embedding, content_hash in zip(chunks, embeddings, hashes, strict=True):
        chunk_id = uuid.uuid4()
        metadata = {**chunk.metadata, "chunker_version": CHUNKER_VERSION}
        # Raw SQL for the pgvector column since it's not modeled in the
        # SQLAlchemy ORM layer (see app/models/llm.py note).
        await db.execute(
            sql_text(
                """
                INSERT INTO document_chunks
                    (id, tenant_id, document_id, knowledge_base_id, content, content_hash,
                     chunk_metadata, embedding_model, is_current, embedding)
                VALUES (:id, :tenant_id, :document_id, :knowledge_base_id, :content, :content_hash,
                        :metadata, :embedding_model, true, CAST(:embedding AS vector))
                """
            ),
            {
                "id": str(chunk_id),
                "tenant_id": str(document.tenant_id),
                "document_id": str(document.id),
                "knowledge_base_id": str(document.knowledge_base_id) if document.knowledge_base_id else None,
                "content": chunk.content,
                "content_hash": content_hash,
                "metadata": metadata,
                "embedding_model": embedding_model,
                "embedding": _vector_to_literal(embedding),
            },
        )

    if document.knowledge_base_id:
        await db.execute(
            sql_text(
                """
                UPDATE knowledge_bases
                SET document_count = document_count + 1, chunk_count = chunk_count + :n
                WHERE id = :kb_id
                """
            ),
            {"n": len(chunks), "kb_id": str(document.knowledge_base_id)},
        )

    # "indexed" (chunked + embedded), distinct from "ready" (stored and
    # scanned successfully, set by app/tasks/storage_tasks.py before this
    # function is ever called) — two different pipeline stages, two
    # different status values, so a document stuck between them is
    # diagnosable from its status alone.
    document.status = "indexed"
    await db.flush()
    return len(chunks)


async def _resolve_embedding_model(db: AsyncSession, document: Document) -> str:
    """A KB pins its embedding model at creation time (architecture doc
    section 3, Problem 1) — chunks for a document in that KB always use
    the KB's model, never a global default, so introducing a new
    platform-wide default embedding model never silently breaks existing
    KBs. Documents with no KB (not yet common, but the FK is nullable)
    fall back to the platform default."""
    if document.knowledge_base_id:
        from app.models.knowledge import KnowledgeBase
        kb = await db.get(KnowledgeBase, document.knowledge_base_id)
        if kb:
            return kb.embedding_model
    return DEFAULT_EMBEDDING_MODEL


async def hybrid_search(db: AsyncSession, tenant_id: uuid.UUID, query: str, query_embedding: list[float],
                         knowledge_base_ids: list[uuid.UUID] | None = None, limit: int = 8) -> list[dict]:
    """Reciprocal-rank-fused hybrid search: pgvector cosine similarity +
    Postgres full-text search. Pure vector search under-ranks queries
    containing exact terms (IDs, product names) that embeddings don't
    represent well — hybrid search materially improves recall for those.

    knowledge_base_ids, when provided, MUST already be the intersection
    of (a) KBs toggled on for the conversation and (b) KBs the calling
    user can actually access (app.services.kb_service.get_accessible_kb_ids)
    — this function trusts its caller on that; it only additionally
    enforces tenant_id and is_current/superseded-document exclusion,
    which are its own responsibility regardless of caller.
    """
    kb_filter_sql = ""
    params = {"tenant_id": str(tenant_id), "query": query,
              "query_embedding": _vector_to_literal(query_embedding), "limit": limit}
    if knowledge_base_ids is not None:
        if not knowledge_base_ids:
            return []  # explicitly scoped to zero KBs — no results, not "search everything"
        kb_filter_sql = "AND dc.knowledge_base_id = ANY(:kb_ids)"
        params["kb_ids"] = [str(k) for k in knowledge_base_ids]

    rows = await db.execute(
        sql_text(
            f"""
            WITH vector_results AS (
                SELECT dc.id, dc.content, dc.chunk_metadata,
                       row_number() OVER (ORDER BY dc.embedding <=> CAST(:query_embedding AS vector)) AS rank
                FROM document_chunks dc
                JOIN documents d ON d.id = dc.document_id
                WHERE dc.tenant_id = :tenant_id AND dc.is_current = true
                  AND d.status NOT IN ('superseded', 'rejected_virus', 'rejected_invalid_type', 'failed')
                  {kb_filter_sql}
                ORDER BY dc.embedding <=> CAST(:query_embedding AS vector)
                LIMIT 20
            ),
            text_results AS (
                SELECT dc.id, dc.content, dc.chunk_metadata,
                       row_number() OVER (ORDER BY ts_rank_cd(to_tsvector('english', dc.content),
                                          plainto_tsquery('english', :query)) DESC) AS rank
                FROM document_chunks dc
                JOIN documents d ON d.id = dc.document_id
                WHERE dc.tenant_id = :tenant_id AND dc.is_current = true
                  AND d.status NOT IN ('superseded', 'rejected_virus', 'rejected_invalid_type', 'failed')
                  AND to_tsvector('english', dc.content) @@ plainto_tsquery('english', :query)
                  {kb_filter_sql}
                LIMIT 20
            )
            SELECT COALESCE(v.id, t.id) AS id,
                   COALESCE(v.content, t.content) AS content,
                   (1.0 / (60 + COALESCE(v.rank, 1000)) + 1.0 / (60 + COALESCE(t.rank, 1000))) AS score
            FROM vector_results v
            FULL OUTER JOIN text_results t ON v.id = t.id
            ORDER BY score DESC
            LIMIT :limit
            """
        ),
        params,
    )
    return [{"id": r.id, "content": r.content, "score": r.score} for r in rows]
