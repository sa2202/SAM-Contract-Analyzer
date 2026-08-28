"""
Splits a large parsed document into overlapping chunks so nothing gets
silently dropped the way truncation does.

Strategy: pack whole pages together up to a per-chunk character budget, and
carry the LAST page of each chunk forward as the first page of the next
chunk. Clauses that span a page break are the main risk with page-based
splitting, so the overlap gives the model a second chance to see anything
that started right at a boundary. Extraction is naturally somewhat
duplicate-tolerant here (the merge step below dedupes), so slight overlap
is cheap insurance, not a precision problem.

For single-"page" documents (DOCX has no real pagination -- see parser.py),
falls back to splitting on paragraph breaks instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from parser import ParsedDocument, PageResult

# Target size per chunk, in characters (not tokens -- see extract.py's
# rate limiter for the token-level budget this feeds into).
DEFAULT_CHUNK_CHARS = 9000


@dataclass
class DocumentChunk:
    index: int                # 0-based position among chunks
    total_chunks: int
    text: str
    pages_covered: list[int]  # page numbers included (for UI/debug display)


def _paragraph_chunks(text: str, max_chars: int, overlap_chars: int = 600) -> list[str]:
    """Fallback splitter for a single giant page (e.g. a long DOCX) with no
    page markers to split on. Splits on paragraph breaks, carrying a small
    tail of the previous chunk forward for continuity."""
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]

    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if current and len(current) + len(para) > max_chars:
            chunks.append(current)
            # carry a tail of the previous chunk into the next, for context
            tail = current[-overlap_chars:]
            current = tail + "\n\n" + para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        chunks.append(current)

    return chunks or [text]


def _split_oversized_block(block: str, max_chars: int, overlap_chars: int) -> list[str]:
    """
    Split one page that is larger than the entire chunk budget.

    Tries paragraph boundaries first so clauses stay intact; falls back to a
    hard character cut only when a single paragraph is itself oversized,
    because at that point any split is arbitrary and an arbitrary split still
    beats hanging. Consecutive pieces overlap so a clause straddling a cut is
    seen whole at least once.
    """
    # Paragraph split first so clauses stay intact where possible. Note this
    # calls _paragraph_chunks directly rather than recursing into
    # chunk_document -- the hard cut below is the terminating case, so there
    # is no path that can loop.
    pieces = _paragraph_chunks(block, max_chars, overlap_chars)

    final: list[str] = []
    for piece in pieces:
        if len(piece) <= max_chars:
            final.append(piece)
            continue
        # A single paragraph over budget: any split point is arbitrary, so cut
        # mechanically with overlap. Arbitrary beats truncating or hanging.
        step = max(max_chars - overlap_chars, max_chars // 2)
        for start in range(0, len(piece), step):
            final.append(piece[start:start + max_chars])
    return final


def chunk_document(doc: ParsedDocument, max_chars: int = DEFAULT_CHUNK_CHARS, overlap_chars: int = 800) -> list[DocumentChunk]:
    """
    Returns a list of DocumentChunk covering the ENTIRE document -- no
    content is dropped. If the whole document already fits in one chunk,
    returns a single-element list (callers can treat 1-chunk and N-chunk
    documents identically).

    Overlap is a bounded CHARACTER tail carried from the end of one chunk
    into the start of the next (not a whole extra page) -- carrying a full
    page as "overlap" is wasteful when pages are large relative to the
    chunk budget, since it can end up eating half of every chunk just to
    re-send content the model already saw.
    """
    if doc.char_count <= max_chars:
        pages = [p.page_number for p in doc.pages]
        text = doc.full_text
        return [DocumentChunk(index=0, total_chunks=1, text=text, pages_covered=pages)]

    # Multi-page document (PDF): pack whole pages until the budget would be
    # exceeded, then start the next chunk with a small text-tail of the
    # previous chunk (for boundary context) followed by the page that
    # didn't fit.
    if len(doc.pages) > 1:
        page_blocks = [
            (p.page_number, f"\n\n--- Page {p.page_number} ---\n{p.text}")
            for p in doc.pages
        ]

        raw_chunks: list[tuple[str, list[int]]] = []
        buffer = ""
        buffer_pages: list[int] = []

        i = 0
        while i < len(page_blocks):
            page_num, block = page_blocks[i]
            prospective = buffer + block

            # A SINGLE page bigger than the whole budget must be split, not
            # deferred. Without this branch the loop below flushes the buffer,
            # refills it with the overlap tail, retries the same oversized
            # page, finds it still doesn't fit, and repeats forever -- the app
            # hangs with no error and no output. Rare but real: one dense OCR'd
            # page, or a PDF whose "pages" are actually long continuous sheets.
            if len(block) > max_chars:
                if buffer:
                    raw_chunks.append((buffer, buffer_pages.copy()))
                    buffer, buffer_pages = "", []
                for piece in _split_oversized_block(block, max_chars, overlap_chars):
                    raw_chunks.append((piece, [page_num]))
                i += 1
                continue

            if buffer and len(prospective) > max_chars:
                raw_chunks.append((buffer, buffer_pages.copy()))
                # start next chunk with a bounded tail of the previous chunk,
                # tagged so the model knows it's boundary context, not new content
                tail = buffer[-overlap_chars:]
                buffer = f"[...continued from previous section...]\n{tail}"
                buffer_pages = buffer_pages[-1:]  # the tail belongs to the last page
                continue  # retry placing this same page into the fresh buffer

            buffer = prospective
            buffer_pages.append(page_num)
            i += 1

        if buffer:
            raw_chunks.append((buffer, buffer_pages.copy()))

        total = len(raw_chunks)
        return [
            DocumentChunk(index=i, total_chunks=total, text=text.strip(), pages_covered=pages)
            for i, (text, pages) in enumerate(raw_chunks)
        ]

    # Single "page" document (DOCX, an image, a CSV): paragraph-level split.
    #
    # _paragraph_chunks alone is NOT sufficient here. It splits on blank
    # lines, so text with no paragraph breaks -- OCR output from one dense
    # page, a DOCX saved as a single block, a CSV -- comes back as one
    # oversized chunk. That chunk is then silently cut down by
    # prompts.MAX_DOCUMENT_CHARS, which is precisely the invisible truncation
    # this module exists to prevent. Enforcing the budget here closes it.
    single_page_num = doc.pages[0].page_number if doc.pages else 1
    text_chunks = []
    for piece in _paragraph_chunks(doc.full_text, max_chars):
        if len(piece) <= max_chars:
            text_chunks.append(piece)
        else:
            text_chunks.extend(_split_oversized_block(piece, max_chars, overlap_chars))
    total = len(text_chunks)
    return [
        DocumentChunk(index=i, total_chunks=total, text=chunk, pages_covered=[single_page_num])
        for i, chunk in enumerate(text_chunks)
    ]
