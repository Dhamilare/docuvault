import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Tuple

from django.conf import settings
from django.db import close_old_connections
from django.db.models import F
from django.utils import timezone

from vault.models import Document, DocumentCategory, ProcessingLog, SystemConfiguration
from vault.services import gemini_classifier, sharepoint

logger = logging.getLogger("vault")

_executor = ThreadPoolExecutor(max_workers=getattr(settings, "VAULT_WORKER_THREADS", 4))


def _run_in_worker(document_pk: int) -> None:
    """Wrapper ensuring DB connections are cleanly closed after background thread execution."""
    try:
        close_old_connections()
        process_document(document_pk)
    except Exception:
        logger.exception("Unhandled exception in background worker for Document %s", document_pk)
    finally:
        close_old_connections()


def enqueue_document_processing(document_pk: int) -> str:
    """
    Dispatches document processing asynchronously.
    Uses Celery if `CELERY_BROKER_URL` and `VAULT_USE_CELERY=True` are configured;
    otherwise dispatches cleanly to the managed ThreadPoolExecutor.
    """
    if getattr(settings, "VAULT_USE_CELERY", False):
        try:
            from celery import current_app
            task = current_app.send_task("vault.tasks.process_document_task", args=[document_pk])
            Document.objects.filter(pk=document_pk).update(task_id=task.id)
            return task.id
        except Exception as exc:
            logger.warning("Celery dispatch failed (%s); falling back to local worker pool.", exc)

    _executor.submit(_run_in_worker, document_pk)
    return "threadpool"


def file_reviewed_document(document: Document, actor=None) -> Tuple[bool, str]:
    """
    Uploads the document to SharePoint, syncs ECM list-item columns, updates
    telemetry, and purges the transient file if configured.
    Returns (success: bool, error_message: str).
    """
    if not document.has_temp_file:
        msg = "Transient scan file is missing from storage."
        document.mark(Document.Status.FAILED, error_message=msg)
        document.log_step(ProcessingLog.Step.FAILED, msg, actor=actor)
        return False, msg

    config = SystemConfiguration.get_settings()
    folder_path = document.clean_sharepoint_path(
        base_root=config.sharepoint_root_folder,
        template=config.folder_template,
    )
    target_filename = document.get_sanitized_filename()

    document.log_step(
        ProcessingLog.Step.FILING,
        f"Uploading '{target_filename}' to SharePoint folder '{folder_path}'...",
        actor=actor,
    )
    start_time = time.time()

    try:
        upload_result = sharepoint.upload_document(
            document=document,
            folder_path=folder_path,
            filename=target_filename,
            conflict_behavior=config.sharepoint_conflict_behavior,
        )

        # Update SharePoint upload fields immediately before metadata step
        document.sharepoint_site_id = upload_result["site_id"]
        document.sharepoint_drive_id = upload_result["drive_id"]
        document.sharepoint_item_id = upload_result["item_id"]
        document.sharepoint_etag = upload_result.get("etag", "")
        document.sharepoint_web_url = upload_result.get("web_url", "")
        document.sharepoint_folder_path = upload_result["folder_path"]
        document.filed_filename = upload_result.get("name", target_filename)
        document.sharepoint_uploaded_at = timezone.now()

        # Sync SharePoint ECM metadata columns
        synced_cols = {}
        meta_err = ""
        if config.sync_sharepoint_columns:
            synced_cols, list_item_id, meta_ok, meta_err = sharepoint.sync_item_metadata(
                site_id=upload_result["site_id"],
                drive_id=upload_result["drive_id"],
                item_id=upload_result["item_id"],
                document=document,
            )
            document.sharepoint_list_item_id = list_item_id
            document.sharepoint_metadata_payload = synced_cols
            document.sharepoint_metadata_status = (
                Document.MetadataSyncStatus.SYNCED if meta_ok else Document.MetadataSyncStatus.FAILED
            )
        else:
            document.sharepoint_metadata_status = Document.MetadataSyncStatus.NOT_APPLICABLE

        duration_ms = int((time.time() - start_time) * 1000)

        document.mark(
            Document.Status.FILED,
            sharepoint_site_id=document.sharepoint_site_id,
            sharepoint_drive_id=document.sharepoint_drive_id,
            sharepoint_item_id=document.sharepoint_item_id,
            sharepoint_list_item_id=document.sharepoint_list_item_id,
            sharepoint_etag=document.sharepoint_etag,
            sharepoint_web_url=document.sharepoint_web_url,
            sharepoint_folder_path=document.sharepoint_folder_path,
            filed_filename=document.filed_filename,
            sharepoint_uploaded_at=document.sharepoint_uploaded_at,
            sharepoint_metadata_status=document.sharepoint_metadata_status,
            sharepoint_metadata_payload=document.sharepoint_metadata_payload,
            error_message="",
        )

        msg = f"Filed to SharePoint: '{document.sharepoint_folder_path}/{document.filed_filename}'"
        if synced_cols:
            msg += f" ({len(synced_cols)} ECM columns synced)"

        document.log_step(
            ProcessingLog.Step.FILED,
            msg,
            actor=actor,
            duration_ms=duration_ms,
            metadata={
                "web_url": document.sharepoint_web_url,
                "item_id": document.sharepoint_item_id,
                "columns": synced_cols,
            },
        )

        if config.sync_sharepoint_columns and document.sharepoint_metadata_status == Document.MetadataSyncStatus.FAILED:
            document.log_step(
                ProcessingLog.Step.METADATA_FAILED,
                f"File uploaded, but SharePoint column sync failed: {meta_err}",
                actor=actor,
            )

        if config.delete_local_on_file:
            if document.purge_temp_file():
                document.log_step(ProcessingLog.Step.PURGED, "Transient scan file purged after verified filing.", actor=actor)

        return True, ""

    except Exception as exc:
        duration_ms = int((time.time() - start_time) * 1000)
        err_msg = str(exc)
        logger.exception("Failed to file Document %s to SharePoint: %s", document.pk, err_msg)
        document.mark(Document.Status.FAILED, error_message=err_msg)
        document.log_step(
            ProcessingLog.Step.FAILED,
            f"SharePoint filing failed: {err_msg}",
            actor=actor,
            duration_ms=duration_ms,
        )
        return False, err_msg


def retry_metadata_sync(document: Document, actor=None) -> Tuple[bool, str]:
    """Retries syncing SharePoint ECM columns for an already-filed document."""
    if document.status != Document.Status.FILED or not document.sharepoint_item_id:
        return False, "Document has not been filed to SharePoint yet."

    site_id = document.sharepoint_site_id or getattr(settings, "GRAPH_SITE_ID", "").strip()
    drive_id = document.sharepoint_drive_id or getattr(settings, "GRAPH_DRIVE_ID", "").strip()
    if not site_id or not drive_id:
        return False, "Missing SharePoint site_id or drive_id."

    synced_cols, list_item_id, ok, err = sharepoint.sync_item_metadata(
        site_id=site_id,
        drive_id=drive_id,
        item_id=document.sharepoint_item_id,
        document=document,
    )
    document.sharepoint_list_item_id = list_item_id or document.sharepoint_list_item_id
    document.sharepoint_metadata_payload = synced_cols
    document.sharepoint_metadata_status = (
        Document.MetadataSyncStatus.SYNCED if ok else Document.MetadataSyncStatus.FAILED
    )
    document.save(
        update_fields=[
            "sharepoint_list_item_id",
            "sharepoint_metadata_payload",
            "sharepoint_metadata_status",
            "updated_at",
        ]
    )

    if ok:
        document.log_step(
            ProcessingLog.Step.METADATA_SYNCED,
            f"Synced {len(synced_cols)} SharePoint ECM columns.",
            actor=actor,
            metadata={"columns": synced_cols},
        )
        return True, ""

    document.log_step(ProcessingLog.Step.METADATA_FAILED, err, actor=actor)
    return False, err


def process_document(document_pk: int, force: bool = False) -> None:
    """
    Claims a document atomically, reads its content via Gemini, creates/resolves its
    category, and either auto-files to SharePoint or routes to Needs Review.
    """
    claimable_statuses = [
        Document.Status.PENDING,
        Document.Status.NEEDS_REVIEW,
        Document.Status.FAILED,
    ]
    if force:
        claimable_statuses.append(Document.Status.PROCESSING)

    claimed = Document.objects.filter(
        pk=document_pk,
        status__in=claimable_statuses,
    ).update(
        status=Document.Status.PROCESSING,
        locked_at=timezone.now(),
        attempt_count=F("attempt_count") + 1,
        error_message="",
        updated_at=timezone.now(),
    )
    if not claimed:
        logger.info("Document %s could not be claimed (already processing or filed).", document_pk)
        return

    doc = Document.objects.get(pk=document_pk)
    if not doc.has_temp_file:
        msg = "Transient scan file missing from storage; cannot classify."
        doc.mark(Document.Status.FAILED, error_message=msg, locked_at=None)
        doc.log_step(ProcessingLog.Step.FAILED, msg)
        return

    config = SystemConfiguration.get_settings()
    doc.log_step(
        ProcessingLog.Step.CLASSIFYING,
        f"Reading document content via {config.gemini_model} (attempt #{doc.attempt_count})...",
    )

    start_time = time.time()
    try:
        result = gemini_classifier.classify_document(
            file_source=doc.temp_file,
            filename=doc.original_filename,
            model_name=config.gemini_model,
            max_retries=config.max_retry_attempts,
        )
        duration_ms = int((time.time() - start_time) * 1000)

        category = DocumentCategory.get_or_create_discovered(
            raw_name=result["document_type"],
            description=result["category_description"],
        )

        # Preserve operator edits if manually corrected prior to re-run
        company_name = doc.company_name if (doc.is_manually_edited and doc.company_name) else result["company_name"]

        doc.mark(
            Document.Status.PROCESSING,
            category=category,
            company_name=company_name,
            document_title=result["document_title"],
            document_date=result["document_date"],
            document_year=result["document_year"],
            document_language=result["document_language"],
            ai_summary=result["summary"],
            extracted_text_excerpt=result["extracted_text"],
            confidence_score=result["confidence"],
            extracted_entities=result["key_entities"],
            ai_raw_response=result["raw_response"],
            page_count=result["page_count"],
            ai_model_used=result["model_used"],
            ai_input_tokens=result["input_tokens"],
            ai_output_tokens=result["output_tokens"],
            processed_at=timezone.now(),
            locked_at=None,
        )
        category.update_document_count()

        if doc.confidence_score >= config.auto_file_threshold:
            doc.log_step(
                ProcessingLog.Step.CLASSIFIED,
                f"Classified as '{category.name}' ({doc.confidence_percent}% >= {int(config.auto_file_threshold * 100)}%). Auto-filing to SharePoint...",
                duration_ms=duration_ms,
                metadata={
                    "model": doc.ai_model_used,
                    "input_tokens": doc.ai_input_tokens,
                    "output_tokens": doc.ai_output_tokens,
                },
            )
            file_reviewed_document(doc)
        else:
            doc.mark(Document.Status.NEEDS_REVIEW)
            doc.log_step(
                ProcessingLog.Step.CLASSIFIED,
                f"Classified as '{category.name}' ({doc.confidence_percent}% below {int(config.auto_file_threshold * 100)}% threshold). Routed to Review Queue.",
                duration_ms=duration_ms,
                metadata={
                    "model": doc.ai_model_used,
                    "input_tokens": doc.ai_input_tokens,
                    "output_tokens": doc.ai_output_tokens,
                },
            )

    except Exception as exc:
        duration_ms = int((time.time() - start_time) * 1000)
        logger.error("Gemini classification failed for Document %s: %s", doc.pk, exc, exc_info=True)
        doc.mark(
            Document.Status.NEEDS_REVIEW,
            error_message=str(exc),
            ai_summary=doc.ai_summary or "AI classification failed. Ready for manual review.",
            locked_at=None,
        )
        doc.log_step(
            ProcessingLog.Step.FAILED,
            f"AI classification error: {exc}. Local scan retained for manual verification.",
            duration_ms=duration_ms,
        )