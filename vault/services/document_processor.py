import json
import logging
import math
import os
import time
from typing import Tuple
import msal
import requests
from django.conf import settings
from django.utils import timezone
from vault.models import Document, DocumentCategory, ProcessingLog, SystemConfiguration

logger = logging.getLogger("vault")

# ==============================================================================
# 1. Microsoft Graph Token & Filing Services
# ==============================================================================

def get_graph_app_token() -> str:
    """Acquires application-only token via Client Credentials for SharePoint filing."""
    if not (settings.MS_CLIENT_ID and settings.MS_CLIENT_SECRET and settings.MS_TENANT_ID):
        raise ValueError("Microsoft Entra ID client credentials not configured in settings/.env.")

    app = msal.ConfidentialClientApplication(
        client_id=settings.MS_CLIENT_ID,
        client_credential=settings.MS_CLIENT_SECRET,
        authority=settings.MS_AUTHORITY,
    )
    result = app.acquire_token_for_client(scopes=settings.GRAPH_SCOPES)
    if "access_token" in result:
        return result["access_token"]

    err = result.get("error_description") or result.get("error") or "Unknown MSAL token error"
    raise PermissionError(f"Failed to acquire Microsoft Graph app token: {err}")


def upload_to_sharepoint(document: Document) -> Tuple[str, str, str]:
    """
    Uploads document to SharePoint via Microsoft Graph API.
    Supports both direct upload (<= 4MB) and UploadSession chunking (> 4MB).
    Returns (item_id, web_url, folder_path).
    """
    token = get_graph_app_token()
    headers = {"Authorization": f"Bearer {token}"}

    site_id = settings.GRAPH_SITE_ID.strip()
    drive_id = settings.GRAPH_DRIVE_ID.strip()
    root_folder = settings.GRAPH_LIBRARY_ROOT_FOLDER

    if not site_id or not drive_id:
        raise ValueError("GRAPH_SITE_ID or GRAPH_DRIVE_ID is missing in settings/.env.")

    # Generate sanitized target folder path
    folder_path = document.clean_sharepoint_path(base_root=root_folder)
    file_name = document.original_filename
    file_path = document.temp_file.path
    file_size = os.path.getsize(file_path)

    # Encode path for Microsoft Graph
    encoded_item_path = f"{folder_path}/{file_name}".replace(" ", "%20")
    base_drive_url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives/{drive_id}"

    # Branch 1: Files <= 4 MB (Direct PUT)
    if file_size <= 4 * 1024 * 1024:
        put_url = f"{base_drive_url}/root:/{encoded_item_path}:/content"
        with open(file_path, "rb") as f:
            resp = requests.put(
                put_url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"},
                data=f,
                timeout=60,
            )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Graph PUT upload failed [{resp.status_code}]: {resp.text}")
        data = resp.json()
        return data["id"], data.get("webUrl", ""), folder_path

    # Branch 2: Files > 4 MB (Create Upload Session & Stream Chunks)
    session_url = f"{base_drive_url}/root:/{encoded_item_path}:/createUploadSession"
    session_payload = {
        "item": {
            "@microsoft.graph.conflictBehavior": "rename",
            "name": file_name,
        }
    }
    s_resp = requests.post(session_url, headers=headers, json=session_payload, timeout=30)
    if s_resp.status_code not in (200, 201):
        raise RuntimeError(f"Failed to create Graph UploadSession [{s_resp.status_code}]: {s_resp.text}")

    upload_url = s_resp.json()["uploadUrl"]
    chunk_size = 320 * 1024 * 10  # 3.2 MB chunks (Graph requires multiples of 320 KiB)
    final_data = None

    with open(file_path, "rb") as f:
        chunk_idx = 0
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            start_byte = chunk_idx * chunk_size
            end_byte = start_byte + len(chunk) - 1
            chunk_headers = {
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start_byte}-{end_byte}/{file_size}",
            }
            c_resp = requests.put(upload_url, headers=chunk_headers, data=chunk, timeout=90)
            if c_resp.status_code not in (200, 201, 202):
                raise RuntimeError(f"Chunk upload failed [{c_resp.status_code}]: {c_resp.text}")
            if c_resp.status_code in (200, 201):
                final_data = c_resp.json()
            chunk_idx += 1

    if not final_data:
        raise RuntimeError("Upload session completed without returning final item payload.")

    return final_data["id"], final_data.get("webUrl", ""), folder_path


def file_reviewed_document(document: Document, actor=None) -> Tuple[bool, str]:
    """
    Safely transfers the verified document to SharePoint and records audit trail.
    Returns (success: bool, error_message: str).
    """
    if not document.has_temp_file:
        msg = "Local scan file missing from disk."
        document.mark(Document.Status.FAILED, error_message=msg)
        return False, msg

    document.log_step(ProcessingLog.Step.FILING, "Filing document to SharePoint...", actor=actor)
    start_time = time.time()

    try:
        item_id, web_url, folder_path = upload_to_sharepoint(document)
        duration_ms = int((time.time() - start_time) * 1000)

        document.sharepoint_item_id = item_id
        document.sharepoint_web_url = web_url
        document.sharepoint_folder_path = folder_path
        document.sharepoint_uploaded_at = timezone.now()
        document.status = Document.Status.FILED
        document.error_message = ""
        document.save()

        document.log_step(
            ProcessingLog.Step.FILED,
            f"Successfully filed to SharePoint: '{folder_path}'",
            actor=actor,
            duration_ms=duration_ms,
            metadata={"web_url": web_url, "item_id": item_id},
        )

        # Safely remove transient file now that SharePoint upload succeeded
        config = SystemConfiguration.get_settings()
        if config.delete_local_on_file:
            document.purge_temp_file()

        return True, ""

    except Exception as exc:
        duration_ms = int((time.time() - start_time) * 1000)
        err_msg = str(exc)
        logger.exception("Failed to file document %s to SharePoint: %s", document.pk, err_msg)
        document.status = Document.Status.FAILED
        document.error_message = err_msg
        document.save()

        document.log_step(
            ProcessingLog.Step.FAILED,
            f"SharePoint upload failed: {err_msg}",
            actor=actor,
            duration_ms=duration_ms,
        )
        return False, err_msg


# ==============================================================================
# 2. Gemini Classification with Exponential Backoff
# ==============================================================================

def _call_gemini_with_retry(document: Document) -> dict:
    """
    Invokes Google Gemini with backoff retry on 503/429 transient errors.
    """
    api_key = settings.GEMINI_API_KEY
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not configured.")

    model_name = settings.GEMINI_MODEL or "gemini-2.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"

    prompt = (
        "Analyze this scanned document. Extract and return JSON with keys:\n"
        "- company_name: string (originating vendor or organization)\n"
        "- document_year: integer\n"
        "- document_date: string (YYYY-MM-DD format if found, otherwise null)\n"
        "- category: string (e.g. Invoice, Tax Return, Legal Agreement, Bank Statement)\n"
        "- summary: string (2-3 sentence overview)\n"
        "- confidence: float between 0.0 and 1.0\n"
        "- entities: key-value dictionary of pertinent fields (invoice_number, total_amount, etc.)"
    )

    payload = {
        "contents": [{"parts": [{"text": f"Document original filename: {document.original_filename}\n{prompt}"}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }

    max_retries = getattr(settings, "GEMINI_MAX_RETRIES", 3)
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=payload, timeout=45)
            if resp.status_code == 200:
                res_json = resp.json()
                content_text = res_json["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(content_text)

            if resp.status_code in (503, 429):
                # Transient overload: wait with exponential backoff (e.g. 2s, 4s, 8s)
                sleep_sec = math.pow(2, attempt)
                logger.warning(
                    "Gemini API %s on doc %s (attempt %d/%d). Retrying in %ds...",
                    resp.status_code, document.pk, attempt, max_retries, sleep_sec
                )
                time.sleep(sleep_sec)
                last_error = f"Gemini {resp.status_code}: {resp.text}"
                continue

            raise RuntimeError(f"Gemini API returned error {resp.status_code}: {resp.text}")

        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_error = str(exc)
            time.sleep(2)

    raise RuntimeError(f"Gemini API unavailable after {max_retries} retries: {last_error}")


def process_document(document_pk: int):
    """
    Main intake processing pipeline. Runs Gemini classification.
    If Gemini fails with 503 or transient errors, it moves the document to NEEDS_REVIEW
    and preserves the local temp file for manual review and SharePoint filing.
    """
    try:
        doc = Document.objects.get(pk=document_pk)
    except Document.DoesNotExist:
        return

    doc.status = Document.Status.PROCESSING
    doc.save(update_fields=["status"])
    doc.log_step(ProcessingLog.Step.CLASSIFYING, "Submitting document to Gemini AI...")

    start_time = time.time()
    try:
        ai_data = _call_gemini_with_retry(doc)
        duration_ms = int((time.time() - start_time) * 1000)

        # Parse extracted data
        doc.company_name = ai_data.get("company_name") or ""
        doc.document_year = ai_data.get("document_year")
        doc.ai_summary = ai_data.get("summary") or ""
        doc.confidence_score = float(ai_data.get("confidence") or 0.8)
        doc.extracted_entities = ai_data.get("entities") or {}
        doc.ai_raw_response = ai_data

        cat_name = ai_data.get("category") or "Uncategorized"
        doc.category = DocumentCategory.get_or_create_discovered(cat_name)

        doc.processed_at = timezone.now()
        config = SystemConfiguration.get_settings()

        # Check if confidence satisfies auto-filing criteria
        if doc.confidence_score >= config.auto_file_threshold:
            doc.status = Document.Status.PROCESSING
            doc.save()
            doc.log_step(
                ProcessingLog.Step.CLASSIFIED,
                f"Classified as '{cat_name}' with confidence {doc.confidence_percent}%. Auto-filing...",
                duration_ms=duration_ms,
            )
            file_reviewed_document(doc)
        else:
            doc.status = Document.Status.NEEDS_REVIEW
            doc.save()
            doc.log_step(
                ProcessingLog.Step.CLASSIFIED,
                f"Classified as '{cat_name}', confidence {doc.confidence_percent}% below auto-file threshold. Sent to Review Queue.",
                duration_ms=duration_ms,
            )

    except Exception as exc:
        duration_ms = int((time.time() - start_time) * 1000)
        logger.error("Gemini classification failed for document %s: %s", doc.pk, exc)

        # Move to NEEDS_REVIEW so the operator can file manually
        doc.status = Document.Status.NEEDS_REVIEW
        doc.error_message = str(exc)
        doc.ai_summary = "AI classification unavailable (model high demand / rate limit). Ready for manual verification."
        doc.save()

        doc.log_step(
            ProcessingLog.Step.FAILED,
            f"AI classification failed: {exc}. Retaining scan for manual filing.",
            duration_ms=duration_ms,
        )