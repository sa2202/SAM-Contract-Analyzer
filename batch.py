"""
Batch analysis: run several contracts through the pipeline in one go and
keep the results together.

WHY THIS EXISTS
---------------
A single-contract tool answers "what does this agreement say". An ELP asks a
different question: "across every agreement we hold with this publisher,
what are we entitled to in total?" Answering that from one-at-a-time output
means running the tool N times and hand-merging N spreadsheets, which is the
manual step the tool was meant to remove.

Two design decisions worth stating:

1. ONE FAILURE MUST NOT SINK THE BATCH. A contract pack is a mixed bag --
   there's usually one password-protected PDF, one unreadable fax scan, one
   file that's actually a scanned envelope. Each document is analysed inside
   its own try/except and a failure is recorded as a result rather than
   raised, so eleven good contracts still produce a summary and the two
   failures are reported alongside it.

2. THE RATE LIMITER IS SHARED. extract_contract_chunked() builds its own
   TokenRateLimiter when it isn't given one, which is right for a single
   document and wrong for twelve: each document would get a fresh 60-second
   budget and collectively blow straight through the per-minute limit. One
   limiter is created here and threaded through every call so the budget is
   respected across the whole batch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from extract import DEFAULT_TPM_BUDGET, ExtractionError, extract_contract_chunked
from parser import ParsedDocument, parse_document
from rate_limiter import TokenRateLimiter
from schema import ContractExtraction


@dataclass
class ContractResult:
    """Outcome for one document in the batch -- succeeded or failed."""
    filename: str
    extraction: ContractExtraction | None = None
    error: str | None = None
    pages: int = 0
    ocr_pages: int = 0
    chunk_count: int = 1
    conflicts: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.extraction is not None

    @property
    def vendor(self) -> str:
        if not self.extraction:
            return "—"
        field_ = self.extraction.vendor_name
        return field_.value if field_ and field_.value else "Unknown vendor"

    @property
    def line_item_count(self) -> int:
        return len(self.extraction.products) if self.extraction else 0


@dataclass
class BatchResult:
    results: list[ContractResult] = field(default_factory=list)

    @property
    def successful(self) -> list[ContractResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[ContractResult]:
        return [r for r in self.results if not r.ok]

    @property
    def total_line_items(self) -> int:
        return sum(r.line_item_count for r in self.successful)

    @property
    def vendors(self) -> list[str]:
        seen = []
        for result in self.successful:
            if result.vendor not in seen:
                seen.append(result.vendor)
        return seen


def analyze_one(
    file_path: Path,
    limiter: TokenRateLimiter,
    progress_callback=None,
) -> ContractResult:
    """
    Parse + extract + score a single document. Never raises: every failure
    mode is converted into a ContractResult carrying a message the user can
    act on, because in a batch a raised exception costs every other file too.
    """
    filename = file_path.name
    try:
        parsed: ParsedDocument = parse_document(file_path)

        if parsed.char_count < 20:
            return ContractResult(
                filename=filename,
                error="No readable text found, even after OCR. The file may be "
                      "empty, corrupted, or a scan OCR couldn't handle.",
            )

        extraction, merge_info = extract_contract_chunked(
            parsed,
            filename,
            limiter=limiter,
            progress_callback=progress_callback,
        )

        return ContractResult(
            filename=filename,
            extraction=extraction,
            pages=len(parsed.pages),
            ocr_pages=parsed.ocr_page_count,
            chunk_count=merge_info.chunk_count,
            conflicts=merge_info.conflicts or {},
        )

    except ValueError as e:
        # Unreadable / unsupported / password-protected file
        return ContractResult(filename=filename, error=str(e))
    except ExtractionError as e:
        return ContractResult(filename=filename, error=f"Extraction failed: {e}")
    except Exception as e:  # noqa: BLE001 -- deliberate catch-all, see docstring
        return ContractResult(
            filename=filename,
            error=f"Unexpected problem ({type(e).__name__}): {e}",
        )


def analyze_batch(
    file_paths: list[Path],
    tpm_budget: int = DEFAULT_TPM_BUDGET,
    on_file_start=None,
    on_file_done=None,
    on_chunk=None,
) -> BatchResult:
    """
    Analyse every document in `file_paths`, sharing one rate-limit budget.

    Callbacks are all optional and exist so a UI can show live progress:
      on_file_start(index, total, filename)
      on_file_done(index, total, ContractResult)
      on_chunk(current, total)   -- forwarded from the chunked extractor
    """
    limiter = TokenRateLimiter(tpm_budget=tpm_budget)
    batch = BatchResult()
    total = len(file_paths)

    for index, path in enumerate(file_paths, start=1):
        if on_file_start:
            on_file_start(index, total, path.name)

        result = analyze_one(path, limiter=limiter, progress_callback=on_chunk)
        batch.results.append(result)

        if on_file_done:
            on_file_done(index, total, result)

    return batch
