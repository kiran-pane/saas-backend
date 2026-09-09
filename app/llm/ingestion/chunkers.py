"""Six chunking strategies, routed by document type. See blueprint section
4.5 for the full rationale. The recommended default combines structural
splitting with hierarchical parent/child chunks: small chunks are indexed
for precise retrieval matching, but the larger parent section is returned
to the generation step for full context — this combination consistently
beats any single fixed-size splitter on both retrieval precision and
answer groundedness.
"""
from dataclasses import dataclass, field


@dataclass
class Chunk:
    content: str
    metadata: dict = field(default_factory=dict)
    parent_content: str | None = None  # populated by the hierarchical chunker


def fixed_size_chunker(text: str, chunk_size: int = 800, overlap: int = 100) -> list[Chunk]:
    """1. Fixed-size with overlap. Baseline strategy; best for uniform,
    unstructured content like raw logs or chat transcripts where structure
    doesn't help and a simple sliding window is good enough."""
    chunks = []
    step = chunk_size - overlap
    for i in range(0, len(text), step):
        piece = text[i : i + chunk_size]
        if piece.strip():
            chunks.append(Chunk(content=piece))
    return chunks


def recursive_character_chunker(text: str, chunk_size: int = 600, overlap: int = 80) -> list[Chunk]:
    """2. Recursive character splitting. Tries paragraph -> sentence ->
    word boundaries recursively until a piece fits chunk_size. The
    general-purpose default for prose without strong headings."""
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=overlap)
    return [Chunk(content=c) for c in splitter.split_text(text)]


def semantic_chunker(text: str, embedder, breakpoint_percentile: int = 90) -> list[Chunk]:
    """3. Semantic chunking. Embeds consecutive sentences and splits where
    similarity drops (topic shift). Best for long-form articles/research
    docs where topics shift but there's no reliable heading structure to
    split on. Costs extra embedding calls at ingest time."""
    from langchain_experimental.text_splitter import SemanticChunker

    splitter = SemanticChunker(embeddings=embedder, breakpoint_threshold_type="percentile",
                                breakpoint_threshold_amount=breakpoint_percentile)
    return [Chunk(content=c) for c in splitter.split_text(text)]


def structure_aware_chunker(markdown_text: str) -> list[tuple[str, str]]:
    """4. Structure-aware / Markdown-header splitting. Splits on real
    document structure (headers, sections). Best for manuals, wikis,
    contracts — anything with genuine headings. Returns (header_path,
    section_text) pairs; feed section_text into the hierarchical chunker
    below to get the final indexed chunks."""
    from langchain_text_splitters import MarkdownHeaderTextSplitter

    headers_to_split_on = [("#", "h1"), ("##", "h2"), ("###", "h3")]
    splitter = MarkdownHeaderTextSplitter(headers_to_split_on=headers_to_split_on)
    docs = splitter.split_text(markdown_text)
    return [(" > ".join(d.metadata.values()), d.page_content) for d in docs]


def sentence_window_chunker(text: str, window: int = 2) -> list[Chunk]:
    """5. Sentence-window retrieval. Indexes single sentences but returns
    N surrounding sentences at query time. Best for high-precision Q&A
    over dense technical text where matching needs to be very specific
    but answers need surrounding context to make sense."""
    import re

    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    chunks = []
    for i, sentence in enumerate(sentences):
        if not sentence.strip():
            continue
        lo, hi = max(0, i - window), min(len(sentences), i + window + 1)
        chunks.append(Chunk(content=sentence, metadata={"window_context": " ".join(sentences[lo:hi])}))
    return chunks


def hierarchical_chunker(markdown_text: str, child_chunk_size: int = 500,
                          child_overlap: int = 60) -> list[Chunk]:
    """6. Hierarchical / parent-document chunking (the recommended
    default). Splits into sections via structure_aware_chunker, then
    further splits each section into small child chunks for precise
    embedding-based matching — while keeping the full parent section text
    attached to each child so generation gets full context, not a
    fragment."""
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    sections = structure_aware_chunker(markdown_text)
    child_splitter = RecursiveCharacterTextSplitter(chunk_size=child_chunk_size, chunk_overlap=child_overlap)

    chunks = []
    for header_path, section_text in sections:
        for child_text in child_splitter.split_text(section_text):
            chunks.append(
                Chunk(content=child_text, metadata={"header_path": header_path},
                      parent_content=section_text)
            )
    return chunks
