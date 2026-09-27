"""
Orchestrates a Document through: classification (Gemini) -> confidence
routing -> SharePoint filing (Graph) -> metadata write-back, logging
every step to ProcessingLog so the pipeline is auditable end to end.
"""
import logging
import time

from django.conf import settings
from django.utils import timezone

from ..models import Document, DocumentCategory, ProcessingLog, SystemConfiguration
from . import gemini_service, graph_service

logger = logging.getLogger("vault")


def _log(document: Document, step: str, message: str = "", actor=None, duration_ms=None, metadata=None):
    return document.log_step(
        step=step,
        message=message,
        actor=actor,
        duration_ms=duration_ms,
        metadata=metadata or {},
    )


def _resolve_category(name: str, description: str = "") -> DocumentCategory:
    category = DocumentCategory.get_or_create_discovered(name, description)
    category.update_document_count()
    return category


def _build_folder_path(document: Document) -> str:
    """
    Builds the SharePoint folder path from SystemConfiguration or settings.
    Example: 'Acme Ltd/2025/Vendor Invoices'
    """
    parts = []
    folder_structure = getattr(
        settings, "FOLDER_STRUCTURE", ["company_name", "year", "document_type"]
    )

    for field in folder_structure:
        if field == "company_name":
            parts.append(graph_service.sanitize_path_segment(document.company_name or "Unknown Company"))
        elif field == "year":
            parts.append(str(document.document_year) if document.document_year else "Undated")
        elif field == "document_type":
            parts.append(
                graph_service.sanitize_path_segment(
                    document.category.name if document.category else "Unclassified"
                )
            )

    return "/".join(parts) if parts else "Unsorted"


def process_document(document_id: int):
    """
    Runs the full pipeline for one document.
    1. Classifies via Gemini.
    2. Checks confidence threshold against SystemConfiguration / settings.
    3. Auto-files to SharePoint if confident; otherwise routes to NEEDS_REVIEW.
    """
    document = Document.objects.select_related("category").get(pk=document_id)

    try:
        document.mark(Document.Status.PROCESSING)
        _log(document, ProcessingLog.Step.CLASSIFYING, "Submitting document to Gemini AI...")

        start_classify = time.time()
        result = gemini_service.classify_document(document.temp_file.path)
        classify_duration_ms = int((time.time() - start_classify) * 1000)

        category = _resolve_category(result["document_type"], result.get("category_description", ""))

        document.category = category
        document.company_name = result["company_name"]
        document.document_year = result["document_year"]
        document.document_date = result.get("document_date")
        document.confidence_score = result["confidence"]
        document.ai_summary = result["summary"]
        document.extracted_text_excerpt = result["extracted_text"][:5000]
        document.extracted_entities = result.get("key_entities", {})
        document.ai_raw_response = result["raw_response"]
        document.processed_at = timezone.now()
        document.save()

        _log(
            document,
            ProcessingLog.Step.CLASSIFIED,
            f"Classified as '{category.name}' (Confidence: {int(result['confidence'] * 100)}%).",
            duration_ms=classify_duration_ms,
            metadata={"confidence": result["confidence"]},
        )

        # Confidence and required field checks
        threshold = getattr(
            settings,
            "CLASSIFICATION_CONFIDENCE_THRESHOLD",
            SystemConfiguration.get_settings().auto_file_threshold,
        )
        low_confidence = result["confidence"] < threshold
        missing_fields = not result["company_name"] or not result["document_year"]

        if low_confidence or missing_fields:
            reason = "low confidence score" if low_confidence else "missing key company or year fields"
            document.mark(Document.Status.NEEDS_REVIEW)
            _log(
                document,
                ProcessingLog.Step.CLASSIFIED,
                f"Flagged for human verification ({reason}).",
            )
            return document

        # Meets confidence threshold: proceed to auto-filing
        _file_to_sharepoint(document)
        return document

    except Exception as exc:
        logger.exception("Processing pipeline failed for document %s", document_id)
        document.mark(Document.Status.FAILED, error_message=str(exc))
        _log(document, ProcessingLog.Step.FAILED, f"Pipeline Error: {str(exc)}")
        return document


def file_reviewed_document(document: Document, actor=None):
    """Called after human verification in the review queue to file to SharePoint."""
    _log(
        document,
        ProcessingLog.Step.REVIEWED,
        f"Metadata approved by {actor.username if actor else 'operator'}.",
        actor=actor,
    )
    try:
        _file_to_sharepoint(document, actor=actor)
    except Exception as exc:
        logger.exception("Filing failed after review for document %s", document.pk)
        document.mark(Document.Status.FAILED, error_message=str(exc))
        _log(document, ProcessingLog.Step.FAILED, f"SharePoint filing failed: {str(exc)}", actor=actor)
    return document


def _file_to_sharepoint(document: Document, actor=None):
    """Transfers file bytes to SharePoint via Graph API and purges local copy."""
    _log(document, ProcessingLog.Step.FILING, "Uploading file to SharePoint document library...", actor=actor)
    folder_path = _build_folder_path(document)

    start_file = time.time()
    with document.temp_file.open("rb") as fh:
        content = fh.read()

    upload_result = graph_service.upload_file(
        folder_path=folder_path,
        filename=document.original_filename,
        content=content,
        content_type=document.content_type,
    )

    # Write custom metadata back to SharePoint listItem columns
    graph_service.set_item_metadata(
        upload_result["item_id"],
        {
            "DocumentType": document.category.name if document.category else "",
            "CompanyName": document.company_name,
            "DocumentYear": document.document_year,
            "AIConfidence": document.confidence_score,
        },
    )

    filing_duration_ms = int((time.time() - start_file) * 1000)

    document.mark(
        Document.Status.FILED,
        sharepoint_folder_path=folder_path,
        sharepoint_item_id=upload_result["item_id"],
        sharepoint_web_url=upload_result["web_url"],
        sharepoint_uploaded_at=timezone.now(),
        error_message="",
    )

    _log(
        document,
        ProcessingLog.Step.FILED,
        f"Successfully filed to SharePoint path: /{folder_path}",
        actor=actor,
        duration_ms=filing_duration_ms,
        metadata={"item_id": upload_result["item_id"], "web_url": upload_result["web_url"]},
    )

    # Safely purge transient file from disk
    config = SystemConfiguration.get_settings()
    if getattr(config, "delete_local_on_file", True):
        document.purge_temp_file()