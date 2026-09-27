"""
Reads a scanned document (PDF, image, or text) and, in a single Gemini call,
performs OCR, open-ended classification, and structured field
extraction — no separate OCR engine or fixed category list needed.
"""
from datetime import datetime
import json
import logging
import os

from django.conf import settings
from google import genai
from google.genai import types

logger = logging.getLogger("vault")

MAX_PAGES = 6  # cap per-document page count sent to the model

_PROMPT = """You are an elite enterprise document classification engine.
Analyze the provided document content and return ONLY a valid JSON object (no markdown, no backticks, no explanatory commentary) with these exact keys:

{
  "document_type": "string",          // short, human-readable category inferred from the content (e.g., "Vendor Invoice", "Commercial Lease Agreement", "Board Minutes", "Certificate of Insurance", "Tax Form W-9"). Do not restrict yourself to a predefined list.
  "category_description": "string",   // 1 concise sentence describing what this category represents.
  "company_name": "string",           // primary company, organization, or vendor the document is fundamentally about. Use formal name as printed. Empty string if genuinely absent.
  "document_date": "YYYY-MM-DD or null", // formal document date if present, or null.
  "document_year": 2025,              // integer year of the document, or null if unidentifiable.
  "confidence": 0.95,                 // number from 0.0 to 1.0 reflecting overall classification clarity.
  "summary": "string",                // 1-2 sentence executive summary.
  "extracted_text": "string",         // key extracted text excerpt.
  "key_entities": {                   // open-ended key-value pairs (e.g., invoice_number, total_amount, currency, due_date, contract_parties).
    "key": "value"
  }
}

Be conservative with 'confidence' — use a lower score (< 0.75) if the document is blurry, ambiguous, or lacks key counterparty details.
"""


class ClassificationError(Exception):
    pass


def _client() -> genai.Client:
    api_key = getattr(settings, "GEMINI_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise ClassificationError("GEMINI_API_KEY is not configured in settings or environment.")
    return genai.Client(api_key=api_key)


def _file_to_parts(file_path: str) -> list:
    """Converts a PDF, image, or text file into Gemini content parts."""
    ext = os.path.splitext(file_path)[1].lower()

    # Plain text / CSV / JSON files
    if ext in [".txt", ".csv", ".json", ".log"]:
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(50000)
        return [f"Document Content ({os.path.basename(file_path)}):\n{text}"]

    # PDF documents
    if ext == ".pdf":
        try:
            import pymupdf
            images = []
            doc = pymupdf.open(file_path)
            try:
                for page_index in range(min(len(doc), MAX_PAGES)):
                    page = doc.load_page(page_index)
                    pix = page.get_pixmap(dpi=150)
                    images.append(types.Part.from_bytes(data=pix.tobytes("png"), mime_type="image/png"))
            finally:
                doc.close()
            return images
        except ImportError:
            # Direct PDF byte streaming if pymupdf is not available
            with open(file_path, "rb") as fh:
                return [types.Part.from_bytes(data=fh.read(), mime_type="application/pdf")]

    # Common image formats
    mime_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
    }
    mime_type = mime_map.get(ext, "application/octet-stream")
    with open(file_path, "rb") as fh:
        return [types.Part.from_bytes(data=fh.read(), mime_type=mime_type)]


def classify_document(file_path: str) -> dict:
    """
    Classifies a document via Gemini 2.5 Flash / Pro and returns structured metadata.
    """
    client = _client()

    try:
        parts = _file_to_parts(file_path)
    except Exception as exc:
        raise ClassificationError(f"Could not prepare document payload: {exc}") from exc

    if not parts:
        raise ClassificationError("Document contains no readable content.")

    contents = [_PROMPT] + parts
    model_name = getattr(settings, "GEMINI_MODEL", "gemini-2.5-flash")

    try:
        response = client.models.generate_content(
            model=model_name,
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2,
            ),
        )
    except Exception as exc:
        raise ClassificationError(f"Gemini API request failed: {exc}") from exc

    raw_text = getattr(response, "text", "") or ""
    try:
        # Strip potential markdown code fences if model returned them
        clean_json = raw_text.strip()
        if clean_json.startswith("```"):
            clean_json = clean_json.split("\n", 1)[1]
            if clean_json.endswith("```"):
                clean_json = clean_json.rsplit("\n", 1)[0]
        parsed = json.loads(clean_json)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Gemini returned invalid JSON: %s", raw_text[:500])
        raise ClassificationError("The model's response could not be parsed as JSON.") from exc

    # Parse formal date if provided
    doc_date = None
    raw_date = parsed.get("document_date")
    if raw_date:
        try:
            doc_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            doc_date = None

    return {
        "document_type": (parsed.get("document_type") or "Unclassified").strip().title(),
        "category_description": (parsed.get("category_description") or "").strip(),
        "company_name": (parsed.get("company_name") or "").strip(),
        "document_date": doc_date,
        "document_year": parsed.get("document_year"),
        "confidence": float(parsed.get("confidence", 0.0)),
        "summary": (parsed.get("summary") or "").strip(),
        "extracted_text": (parsed.get("extracted_text") or "").strip(),
        "key_entities": parsed.get("key_entities", {}),
        "raw_response": parsed,
    }