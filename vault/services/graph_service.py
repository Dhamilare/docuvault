"""
Files classified documents into the target SharePoint document library
over Microsoft Graph, using an app-only token (client credentials grant).
"""
import logging
import time
import unicodedata
import msal
import requests
from django.conf import settings

logger = logging.getLogger("vault")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024  # 4 MB Graph simple upload cutoff

_token_cache = {"value": None, "expires_at": 0}


class GraphError(Exception):
    pass


def _get_app_token() -> str:
    now = time.time()
    if _token_cache["value"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["value"]

    app = msal.ConfidentialClientApplication(
        client_id=settings.MS_CLIENT_ID,
        client_credential=settings.MS_CLIENT_SECRET,
        authority=settings.MS_AUTHORITY,
    )
    result = app.acquire_token_for_client(scopes=settings.GRAPH_SCOPES)
    if "access_token" not in result:
        raise GraphError(result.get("error_description", "Failed to acquire Graph app token"))

    _token_cache["value"] = result["access_token"]
    _token_cache["expires_at"] = now + result.get("expires_in", 3600)
    return _token_cache["value"]


def _headers():
    return {"Authorization": f"Bearer {_get_app_token()}"}


def sanitize_path_segment(value: str) -> str:
    """SharePoint forbids characters: ~"#%&*:<>?/\\{|}."""
    value = unicodedata.normalize("NFKD", str(value or "")).strip()
    forbidden = '~"#%&*:<>?/\\{|}'
    for ch in forbidden:
        value = value.replace(ch, "-")
    value = value.strip(" .")
    return value[:120] or "Unclassified"


def ensure_folder_path(folder_path: str) -> str:
    """
    Creates every segment of folder_path under the library root if it doesn't exist.
    Returns the item ID of the deepest folder.
    """
    site_id = settings.GRAPH_SITE_ID
    drive_id = settings.GRAPH_DRIVE_ID
    root_folder = getattr(settings, "GRAPH_LIBRARY_ROOT_FOLDER", "Documents")
    segments = [s for s in f"{root_folder}/{folder_path}".split("/") if s]

    parent_ref = "root"
    current_path = ""
    for segment in segments:
        current_path = f"{current_path}/{segment}" if current_path else segment
        url = f"{GRAPH_BASE}/sites/{site_id}/drives/{drive_id}/root:/{current_path}"
        resp = requests.get(url, headers=_headers(), timeout=15)
        if resp.status_code == 200:
            parent_ref = resp.json()["id"]
            continue
        if resp.status_code != 404:
            raise GraphError(f"Graph error checking folder '{current_path}': {resp.text}")

        create_url = f"{GRAPH_BASE}/sites/{site_id}/drives/{drive_id}/items/{parent_ref}/children"
        create_resp = requests.post(
            create_url,
            headers={**_headers(), "Content-Type": "application/json"},
            json={
                "name": segment,
                "folder": {},
                "@microsoft.graph.conflictBehavior": "replace",
            },
            timeout=15,
        )
        if create_resp.status_code not in (200, 201):
            raise GraphError(f"Graph error creating folder '{segment}': {create_resp.text}")
        parent_ref = create_resp.json()["id"]

    return parent_ref


def upload_file(folder_path: str, filename: str, content: bytes, content_type: str) -> dict:
    """
    Uploads bytes as filename into folder_path, automatically selecting
    simple upload or chunked session based on size.
    """
    site_id = settings.GRAPH_SITE_ID
    drive_id = settings.GRAPH_DRIVE_ID
    root_folder = getattr(settings, "GRAPH_LIBRARY_ROOT_FOLDER", "Documents")
    full_path = f"{root_folder}/{folder_path}/{filename}".strip("/")

    ensure_folder_path(folder_path)

    if len(content) <= _SIMPLE_UPLOAD_LIMIT:
        url = f"{GRAPH_BASE}/sites/{site_id}/drives/{drive_id}/root:/{full_path}:/content"
        resp = requests.put(
            url,
            headers={**_headers(), "Content-Type": content_type or "application/octet-stream"},
            data=content,
            timeout=60,
        )
        if resp.status_code not in (200, 201):
            raise GraphError(f"Graph upload failed ({resp.status_code}): {resp.text}")
        item = resp.json()
    else:
        item = _upload_large_file(site_id, drive_id, full_path, content)

    return {
        "item_id": item["id"],
        "web_url": item.get("webUrl", ""),
        "full_path": full_path,
    }


def _upload_large_file(site_id: str, drive_id: str, full_path: str, content: bytes) -> dict:
    """Chunked upload session for files exceeding 4 MB."""
    session_url = f"{GRAPH_BASE}/sites/{site_id}/drives/{drive_id}/root:/{full_path}:/createUploadSession"
    session_resp = requests.post(
        session_url,
        headers={**_headers(), "Content-Type": "application/json"},
        json={"item": {"@microsoft.graph.conflictBehavior": "rename"}},
        timeout=15,
    )
    if session_resp.status_code not in (200, 201):
        raise GraphError(f"Could not open upload session: {session_resp.text}")
    upload_url = session_resp.json()["uploadUrl"]

    chunk_size = 5 * 1024 * 1024
    total = len(content)
    item = None
    for start in range(0, total, chunk_size):
        end = min(start + chunk_size, total)
        chunk = content[start:end]
        resp = requests.put(
            upload_url,
            headers={"Content-Length": str(len(chunk)), "Content-Range": f"bytes {start}-{end - 1}/{total}"},
            data=chunk,
            timeout=120,
        )
        if resp.status_code in (200, 201):
            item = resp.json()
        elif resp.status_code != 202:
            raise GraphError(f"Chunked upload failed at byte {start}: {resp.text}")

    if item is None:
        raise GraphError("Upload session completed without returning the final item.")
    return item


def set_item_metadata(item_id: str, fields: dict):
    """Writes classification results into SharePoint list-item columns."""
    site_id = settings.GRAPH_SITE_ID
    drive_id = settings.GRAPH_DRIVE_ID
    url = f"{GRAPH_BASE}/sites/{site_id}/drives/{drive_id}/items/{item_id}/listItem/fields"
    resp = requests.patch(
        url,
        headers={**_headers(), "Content-Type": "application/json"},
        json=fields,
        timeout=15,
    )
    if resp.status_code not in (200, 201):
        logger.warning("Could not write SharePoint metadata for item %s: %s", item_id, resp.text)