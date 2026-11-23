import logging
import re
import threading
import time
import unicodedata
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

import msal
import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger("vault")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024  # 4 MB Graph simple PUT cutoff
_UPLOAD_CHUNK_SIZE = 320 * 1024 * 16    # 5.12 MB (must be multiple of 320 KiB per Microsoft Graph spec)
_TOKEN_CACHE_KEY = "vault:graph_app_access_token"
_token_lock = threading.Lock()


class GraphError(Exception):
    pass


def _encode_graph_path(path: str) -> str:
    """Percent-encodes a SharePoint relative path safely while preserving '/' separators."""
    cleaned = "/".join(seg for seg in path.split("/") if seg)
    return quote(cleaned, safe="/")


def get_app_token(force_refresh: bool = False) -> str:
    """
    Thread-safe, cache-backed acquisition of an application-only Microsoft Graph token.
    """
    if not (
        getattr(settings, "MS_CLIENT_ID", "")
        and getattr(settings, "MS_CLIENT_SECRET", "")
        and getattr(settings, "MS_AUTHORITY", "")
    ):
        raise GraphError("Microsoft Entra ID client credentials are not configured in settings.")

    if not force_refresh:
        cached = cache.get(_TOKEN_CACHE_KEY)
        if cached:
            return cached

    with _TOKEN_lock if False else _token_lock:
        if not force_refresh:
            cached = cache.get(_TOKEN_CACHE_KEY)
            if cached:
                return cached

        app = msal.ConfidentialClientApplication(
            client_id=settings.MS_CLIENT_ID,
            client_credential=settings.MS_CLIENT_SECRET,
            authority=settings.MS_AUTHORITY,
        )
        result = app.acquire_token_for_client(scopes=settings.GRAPH_SCOPES)
        if "access_token" not in result:
            err = result.get("error_description") or result.get("error") or "Unknown MSAL error"
            raise GraphError(f"Failed to acquire Graph app token: {err}")

        token = result["access_token"]
        expires_in = max(60, int(result.get("expires_in", 3600)) - 120)
        cache.set(_TOKEN_CACHE_KEY, token, timeout=expires_in)
        return token


def _graph_request(method: str, url: str, max_retries: int = 3, **kwargs) -> requests.Response:
    """
    Executes a Microsoft Graph API call with automatic token refresh on 401
    and Retry-After / exponential backoff on 429, 502, 503, 504.
    """
    timeout = kwargs.pop("timeout", 30)
    custom_headers = kwargs.pop("headers", {})
    force_refresh = False

    for attempt in range(1, max_retries + 1):
        token = get_app_token(force_refresh=force_refresh)
        force_refresh = False
        headers = {"Authorization": f"Bearer {token}", **custom_headers}

        resp = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)

        if resp.status_code == 401 and attempt < max_retries:
            force_refresh = True
            continue

        if resp.status_code in (429, 502, 503, 504) and attempt < max_retries:
            retry_after = resp.headers.get("Retry-After")
            delay = float(retry_after) if (retry_after and retry_after.isdigit()) else (2 ** attempt)
            logger.warning("Graph throttling/transient %s on %s; sleeping %.1fs", resp.status_code, url, delay)
            time.sleep(min(delay, 30.0))
            continue

        return resp

    return resp


def sanitize_path_segment(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or "")).strip()
    value = re.sub(r'[\~\"\#\%\&\*\:\<\>\?\/\\\{\|\}]', "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:120] or "Unclassified"


def ensure_folder_path(site_id: str, drive_id: str, folder_path: str) -> str:
    """
    Ensures every folder segment in `folder_path` exists inside the SharePoint drive.
    Returns the item ID of the deepest folder.
    """
    segments = [s for s in folder_path.strip("/").split("/") if s]
    parent_ref = "root"
    current_path = ""

    for segment in segments:
        current_path = f"{current_path}/{segment}" if current_path else segment
        encoded = _encode_graph_path(current_path)
        check_url = f"{GRAPH_BASE}/sites/{site_id}/drives/{drive_id}/root:/{encoded}"
        resp = _graph_request("GET", check_url, timeout=15)

        if resp.status_code == 200:
            parent_ref = resp.json()["id"]
            continue

        if resp.status_code != 404:
            raise GraphError(f"Graph error checking folder '{current_path}' [{resp.status_code}]: {resp.text}")

        create_url = f"{GRAPH_BASE}/sites/{site_id}/drives/{drive_id}/items/{parent_ref}/children"
        create_resp = _graph_request(
            "POST",
            create_url,
            headers={"Content-Type": "application/json"},
            json={
                "name": segment,
                "folder": {},
                "@microsoft.graph.conflictBehavior": "replace",
            },
            timeout=15,
        )
        # Handle race condition if another worker created the folder simultaneously
        if create_resp.status_code == 409:
            retry_resp = _graph_request("GET", check_url, timeout=15)
            if retry_resp.status_code == 200:
                parent_ref = retry_resp.json()["id"]
                continue

        if create_resp.status_code not in (200, 201):
            raise GraphError(
                f"Graph error creating folder '{segment}' [{create_resp.status_code}]: {create_resp.text}"
            )
        parent_ref = create_resp.json()["id"]

    return parent_ref


def upload_document(
    document,
    folder_path: str,
    filename: str,
    conflict_behavior: str = "rename",
) -> Dict[str, Any]:
    """
    Streams `document.temp_file` directly to SharePoint via Microsoft Graph API
    (supporting both local disk and cloud object storage) using simple PUT (<= 4MB)
    or chunked UploadSession (> 4MB).
    """
    site_id = getattr(settings, "GRAPH_SITE_ID", "").strip()
    drive_id = getattr(settings, "GRAPH_DRIVE_ID", "").strip()
    if not site_id or not drive_id:
        raise GraphError("GRAPH_SITE_ID or GRAPH_DRIVE_ID is not configured in settings.")

    ensure_folder_path(site_id, drive_id, folder_path)

    full_path = f"{folder_path.strip('/')}/{filename}".strip("/")
    encoded_full_path = _encode_graph_path(full_path)
    base_drive_url = f"{GRAPH_BASE}/sites/{site_id}/drives/{drive_id}"

    file_field = document.temp_file
    file_size = file_field.size

    if file_size <= _SIMPLE_UPLOAD_LIMIT:
        put_url = (
            f"{base_drive_url}/root:/{encoded_full_path}:/content"
            f"?@microsoft.graph.conflictBehavior={conflict_behavior}"
        )
        file_field.open("rb")
        try:
            data = file_field.read()
        finally:
            file_field.close()

        resp = _graph_request(
            "PUT",
            put_url,
            headers={"Content-Type": document.content_type or "application/octet-stream"},
            data=data,
            timeout=90,
        )
        if resp.status_code not in (200, 201):
            raise GraphError(f"Graph PUT upload failed [{resp.status_code}]: {resp.text}")
        item = resp.json()
    else:
        item = _upload_large_stream(
            base_drive_url=base_drive_url,
            encoded_full_path=encoded_full_path,
            filename=filename,
            file_field=file_field,
            file_size=file_size,
            conflict_behavior=conflict_behavior,
        )

    return {
        "site_id": site_id,
        "drive_id": drive_id,
        "item_id": item["id"],
        "etag": item.get("eTag", ""),
        "web_url": item.get("webUrl", ""),
        "name": item.get("name", filename),
        "folder_path": folder_path,
    }


def _upload_large_stream(
    base_drive_url: str,
    encoded_full_path: str,
    filename: str,
    file_field,
    file_size: int,
    conflict_behavior: str,
) -> dict:
    """Streams files > 4 MB using a Microsoft Graph UploadSession in 320 KiB-aligned chunks."""
    session_url = f"{base_drive_url}/root:/{encoded_full_path}:/createUploadSession"
    session_payload = {
        "item": {
            "@microsoft.graph.conflictBehavior": conflict_behavior,
            "name": filename,
        }
    }
    s_resp = _graph_request(
        "POST",
        session_url,
        headers={"Content-Type": "application/json"},
        json=session_payload,
        timeout=30,
    )
    if s_resp.status_code not in (200, 201):
        raise GraphError(f"Could not create Graph UploadSession [{s_resp.status_code}]: {s_resp.text}")

    upload_url = s_resp.json()["uploadUrl"]
    final_item = None

    file_field.open("rb")
    try:
        offset = 0
        while offset < file_size:
            chunk = file_field.read(_UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            end_byte = offset + len(chunk) - 1
            chunk_headers = {
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {offset}-{end_byte}/{file_size}",
            }

            # UploadSession URLs are pre-authenticated; retry transient chunk failures
            c_resp = None
            for attempt in range(1, 4):
                c_resp = requests.put(upload_url, headers=chunk_headers, data=chunk, timeout=120)
                if c_resp.status_code in (200, 201, 202):
                    break
                if c_resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise GraphError(f"Chunked upload failed at byte {offset} [{c_resp.status_code}]: {c_resp.text}")

            if c_resp.status_code in (200, 201):
                final_item = c_resp.json()
            offset += len(chunk)
    finally:
        file_field.close()

    if not final_item:
        raise GraphError("Graph UploadSession completed without returning the final DriveItem.")
    return final_item


def sync_item_metadata(
    site_id: str,
    drive_id: str,
    item_id: str,
    document,
) -> Tuple[Dict[str, Any], str, bool, str]:
    """
    Updates SharePoint ListItem custom columns on the uploaded DriveItem.
    Inspects available fields & list columns to avoid invalid-column or type errors.

    Returns: (synced_payload, list_item_id, success, error_message)
    """
    list_item_url = f"{GRAPH_BASE}/sites/{site_id}/drives/{drive_id}/items/{item_id}/listItem?$expand=fields"
    try:
        li_resp = _graph_request("GET", list_item_url, timeout=15)
        if li_resp.status_code != 200:
            err = f"Could not fetch SharePoint listItem [{li_resp.status_code}]: {li_resp.text}"
            logger.warning(err)
            return {}, "", False, err

        li_data = li_resp.json()
        list_item_id = str(li_data.get("id", ""))
        available_fields = li_data.get("fields", {}) or {}

        # Candidate column values matched safely to SharePoint column types
        cat_name = document.category.name if document.category else ""
        iso_date = f"{document.document_date.strftime('%Y-%m-%d')}T00:00:00Z" if document.document_date else None

        candidate_fields: Dict[str, Any] = {
            "Title": (document.document_title or document.get_sanitized_filename())[:255],
            "CompanyName": (document.company_name or "")[:255],
            "Company": (document.company_name or "")[:255],
            "DocumentCategory": cat_name[:255],
            "Category": cat_name[:255],
            "DocumentYear": document.document_year,
            "Year": document.document_year,
            "DocumentDate": iso_date,
            "ConfidenceScore": f"{document.confidence_percent}%" if document.confidence_percent is not None else "",
            "AISummary": (document.ai_summary or "")[:2000],
            "DocumentChecksum": document.checksum_sha256[:64] if document.checksum_sha256 else "",
        }

        if isinstance(document.extracted_entities, dict):
            for k, v in document.extracted_entities.items():
                pascal_key = "".join(part.capitalize() for part in re.split(r"[_\-\s]+", str(k)) if part)
                if pascal_key and v not in (None, ""):
                    candidate_fields[pascal_key] = str(v)[:255]

        # Match case-insensitively against SharePoint internal field names
        existing_key_map = {k.lower(): k for k in available_fields.keys()}
        read_only_keys = {"id", "contenttype", "created", "modified", "authorlookupid", "editorlookupid"}

        patch_payload: Dict[str, Any] = {}
        for col_name, col_val in candidate_fields.items():
            if col_val is None or col_val == "":
                continue
            actual_key = existing_key_map.get(col_name.lower())
            if actual_key and actual_key.lower() not in read_only_keys:
                patch_payload[actual_key] = col_val

        if not patch_payload:
            return {}, list_item_id, True, ""

        patch_url = f"{GRAPH_BASE}/sites/{site_id}/drives/{drive_id}/items/{item_id}/listItem/fields"
        patch_resp = _graph_request(
            "PATCH",
            patch_url,
            headers={"Content-Type": "application/json"},
            json=patch_payload,
            timeout=20,
        )
        if patch_resp.status_code in (200, 201, 204):
            return patch_payload, list_item_id, True, ""

        err = f"SharePoint column PATCH returned [{patch_resp.status_code}]: {patch_resp.text}"
        logger.warning(err)
        return patch_payload, list_item_id, False, err

    except Exception as exc:
        err = f"SharePoint metadata sync exception: {exc}"
        logger.error("Error syncing SharePoint metadata for doc %s: %s", document.pk, exc)
        return {}, "", False, err