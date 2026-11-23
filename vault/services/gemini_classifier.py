import io
import json
import logging
import os
import random
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime
from typing import Any, List, Optional

from django.conf import settings
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

logger = logging.getLogger("vault")

MAX_RENDERED_PAGES = 6
MAX_TEXT_CHARS = 30000


class ExtractedEntity(BaseModel):
    key: str = Field(description="Normalized snake_case entity name, e.g. invoice_number, total_amount, po_number")
    value: str = Field(description="Extracted value as a concise string")


class DocumentClassificationSchema(BaseModel):
    document_type: str = Field(
        description="Short, human-readable category inferred from content (e.g. 'Vendor Invoice', 'Tax Form W-9', 'Lease Agreement')."
    )
    category_description: str = Field(
        description="One concise sentence describing what documents belong in this category."
    )
    company_name: str = Field(
        description="Primary counterparty, vendor, or organization the document is about. Empty string if genuinely absent."
    )
    document_title: str = Field(
        description="Concise descriptive title suitable for a document library (e.g. 'Acme Corp Q3 Invoice #1042')."
    )
    document_date: Optional[str] = Field(
        default=None,
        description="Formal document date in YYYY-MM-DD format if clearly present, otherwise null.",
    )
    document_year: Optional[int] = Field(
        default=None,
        description="4-digit integer year of the document if identifiable, otherwise null.",
    )
    document_language: str = Field(
        default="en",
        description="ISO 639-1 language code of the document (e.g. 'en', 'fr', 'de').",
    )
    confidence: float = Field(
        description="Conservative confidence score between 0.0 and 1.0 reflecting classification & metadata certainty."
    )
    summary: str = Field(
        description="1-2 sentence executive summary of the document."
    )
    extracted_text: str = Field(
        description="Key verbatim text excerpt (header, parties, key figures, dates) up to 1500 chars."
    )
    key_entities: List[ExtractedEntity] = Field(
        default_factory=list,
        description="Pertinent structured key-value pairs (invoice_number, total_amount, currency, due_date, etc.).",
    )


_PROMPT = """You are an enterprise document classification and extraction engine.
Analyze the provided document pages and/or text carefully and extract structured metadata.

Guidelines:
- Infer 'document_type' organically from the actual content (do not restrict to a fixed list; avoid overly generic names like 'Document' when a specific type is clear).
- Extract the formal 'company_name' (vendor, issuer, or primary counterparty). Do not invent a company name if none exists.
- Extract 'document_date' strictly as YYYY-MM-DD when clearly visible, or null. Ensure 'document_year' matches if a date is present.
- Be conservative with 'confidence': score below 0.75 if the scan is blurry, cut off, handwritten/illegible, or missing company/category clarity.
"""


class ClassificationError(Exception):
    pass


def _get_client() -> genai.Client:
    api_key = getattr(settings, "GEMINI_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise ClassificationError("GEMINI_API_KEY is not configured in settings or environment.")
    return genai.Client(api_key=api_key)


def _read_source_bytes(file_source: Any) -> bytes:
    """Reads bytes cleanly from a Django FieldFile, file-like object, or local path."""
    if isinstance(file_source, (bytes, bytearray)):
        return bytes(file_source)
    if isinstance(file_source, str):
        with open(file_source, "rb") as fh:
            return fh.read()
    # Django FieldFile / Storage file
    file_source.open("rb")
    try:
        return file_source.read()
    finally:
        file_source.close()


def _extract_docx_text(raw_bytes: bytes) -> str:
    """Extracts text and tables from a .docx archive without hard dependency failure."""
    try:
        import docx  # python-docx
        doc = docx.Document(io.BytesIO(raw_bytes))
        chunks = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_text:
                    chunks.append(row_text)
        return "\n".join(chunks)[:MAX_TEXT_CHARS]
    except ImportError:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            xml_content = zf.read("word/document.xml")
        root = ET.fromstring(xml_content)
        texts = [elem.text for elem in root.iter() if elem.tag.endswith("}t") and elem.text]
        return " ".join(texts)[:MAX_TEXT_CHARS]


def _build_content_parts(raw_bytes: bytes, filename: str) -> tuple[list, Optional[int]]:
    """
    Converts raw document bytes into Gemini content parts + total page count.
    For long PDFs, renders the first 5 pages + the final signature/closing page,
    and attaches extracted text from remaining pages.
    """
    ext = os.path.splitext(filename)[1].lower()

    if ext in (".txt", ".csv", ".json", ".log"):
        text = raw_bytes.decode("utf-8", errors="replace")[:MAX_TEXT_CHARS]
        if not text.strip():
            raise ClassificationError("Text file is empty.")
        return [f"Document Filename: {filename}\n\n{text}"], 1

    if ext == ".docx":
        text = _extract_docx_text(raw_bytes)
        if not text.strip():
            raise ClassificationError("DOCX file contains no readable text.")
        return [f"Document Filename: {filename}\n\n{text}"], None

    if ext == ".pdf":
        try:
            import pymupdf
            doc = pymupdf.open(stream=raw_bytes, filetype="pdf")
            try:
                total_pages = len(doc)
                if total_pages == 0:
                    raise ClassificationError("PDF contains 0 pages.")

                if total_pages <= MAX_RENDERED_PAGES:
                    page_indices = list(range(total_pages))
                else:
                    # First 5 pages + final page (often holds signatures/totals/dates)
                    page_indices = list(range(MAX_RENDERED_PAGES - 1)) + [total_pages - 1]

                parts: list = []
                extra_text_chunks: list[str] = []

                for idx in range(total_pages):
                    page = doc.load_page(idx)
                    if idx in page_indices:
                        pix = page.get_pixmap(dpi=150)
                        parts.append(types.Part.from_bytes(data=pix.tobytes("png"), mime_type="image/png"))
                    else:
                        txt = (page.get_text("text") or "").strip()
                        if txt:
                            extra_text_chunks.append(f"--- Page {idx + 1} ---\n{txt}")

                if extra_text_chunks:
                    combined = "\n\n".join(extra_text_chunks)[:MAX_TEXT_CHARS]
                    parts.append(f"Extracted text from middle pages ({total_pages} pages total):\n{combined}")

                return parts, total_pages
            finally:
                doc.close()
        except ImportError:
            # Fallback: pass PDF bytes natively to Gemini
            return [types.Part.from_bytes(data=raw_bytes, mime_type="application/pdf")], None

    mime_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
    }
    mime_type = mime_map.get(ext)
    if not mime_type:
        raise ClassificationError(f"Unsupported file extension '{ext}' for AI inspection.")

    return [types.Part.from_bytes(data=raw_bytes, mime_type=mime_type)], 1


def _parse_date(raw_date: Optional[str]) -> Optional[date]:
    if not raw_date:
        return None
    val = str(raw_date).strip()
    if val.lower() in ("null", "none", ""):
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(val, fmt).date()
        except ValueError:
            continue
    return None


def _is_retryable_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(code in msg for code in ("429", "503", "500", "504", "resource_exhausted", "unavailable", "timeout"))


def classify_document(
    file_source: Any,
    filename: str = "document.pdf",
    model_name: Optional[str] = None,
    max_retries: int = 3,
) -> dict:
    """
    Classifies a document via Gemini and returns validated structured metadata.
    `file_source` may be a Django FieldFile, local path string, or raw bytes.
    """
    client = _get_client()
    chosen_model = (model_name or getattr(settings, "GEMINI_MODEL", "") or "gemini-3.8-flash").strip()

    try:
        raw_bytes = _read_source_bytes(file_source)
        if not raw_bytes:
            raise ClassificationError("Document file is empty (0 bytes).")
        parts, page_count = _build_content_parts(raw_bytes, filename)
    except ClassificationError:
        raise
    except Exception as exc:
        raise ClassificationError(f"Could not prepare document payload: {exc}") from exc

    contents = [_PROMPT, f"Original Filename: {filename}"] + parts

    response = None
    last_exc: Optional[Exception] = None
    for attempt in range(1, max(1, max_retries) + 1):
        try:
            response = client.models.generate_content(
                model=chosen_model,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=DocumentClassificationSchema,
                    temperature=0.1,
                ),
            )
            break
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries and _is_retryable_error(exc):
                delay = (2 ** attempt) + random.uniform(0.2, 1.0)
                logger.warning(
                    "Gemini transient error (attempt %s/%s, retrying in %.1fs): %s",
                    attempt,
                    max_retries,
                    delay,
                    exc,
                )
                time.sleep(delay)
                continue
            raise ClassificationError(f"Gemini API request failed: {exc}") from exc

    if response is None:
        raise ClassificationError(f"Gemini API failed after {max_retries} attempts: {last_exc}")

    parsed_model = getattr(response, "parsed", None)
    if isinstance(parsed_model, DocumentClassificationSchema):
        parsed = parsed_model.model_dump()
    else:
        raw_text = (getattr(response, "text", "") or "").strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            parsed = json.loads(raw_text)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("Gemini returned invalid JSON: %s", raw_text[:500])
            raise ClassificationError("Gemini returned unparseable JSON output.") from exc

    doc_date = _parse_date(parsed.get("document_date"))
    raw_year = parsed.get("document_year")
    try:
        doc_year = int(raw_year) if raw_year is not None else (doc_date.year if doc_date else None)
        if doc_year is not None and not (1900 <= doc_year <= 2100):
            doc_year = None
    except (ValueError, TypeError):
        doc_year = doc_date.year if doc_date else None

    try:
        confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.0))))
    except (ValueError, TypeError):
        confidence = 0.0

    # Convert list[ExtractedEntity] -> dict
    entities_dict = {}
    raw_entities = parsed.get("key_entities") or []
    if isinstance(raw_entities, list):
        for item in raw_entities:
            if isinstance(item, dict) and item.get("key"):
                entities_dict[str(item["key"]).strip()] = str(item.get("value", "")).strip()
    elif isinstance(raw_entities, dict):
        entities_dict = {str(k): str(v) for k, v in raw_entities.items()}

    usage = getattr(response, "usage_metadata", None)
    input_tokens = getattr(usage, "prompt_token_count", None) if usage else None
    output_tokens = getattr(usage, "candidates_token_count", None) if usage else None

    return {
        "document_type": (parsed.get("document_type") or "Uncategorized").strip(),
        "category_description": (parsed.get("category_description") or "").strip(),
        "company_name": (parsed.get("company_name") or "").strip()[:255],
        "document_title": (parsed.get("document_title") or "").strip()[:255],
        "document_date": doc_date,
        "document_year": doc_year,
        "document_language": (parsed.get("document_language") or "en").strip()[:16],
        "confidence": confidence,
        "summary": (parsed.get("summary") or "").strip(),
        "extracted_text": (parsed.get("extracted_text") or "").strip()[:4000],
        "key_entities": entities_dict,
        "page_count": page_count,
        "model_used": chosen_model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "raw_response": parsed,
    }