# DocuVault AI

Scan → AI classification → filed into SharePoint. Built for a single
company's internal document filing: staff upload scans, Gemini reads
and classifies them (open-ended — no fixed category list), and the app
files each one into the right `Company/Year/` folder in a SharePoint
document library, with the discovered type/company/year written back
as searchable metadata columns.

## Stack

- Django 5, SQLite (fine at this volume; swap `DATABASES` for Postgres if you outgrow it)
- Microsoft Entra ID sign-in via MSAL (`vault/services/msal_auth.py`)
- Microsoft Graph (app-only, `Sites.Selected`) for SharePoint filing (`vault/services/graph_service.py`)
- Google Gemini (`google-genai` SDK) for OCR + classification in one call (`vault/services/gemini_service.py`)
- Tailwind CSS (CDN) + Alpine.js + vanilla fetch/AJAX — no build step needed for local use

## 1. Azure AD app registration (one-time)

1. Azure Portal → **Microsoft Entra ID** → **App registrations** → **New registration**.
   - Name: `DocuVault AI`
   - Supported account types: **Single tenant**
   - Redirect URI: **Web** → `http://localhost:8000/auth/callback/`
2. **Certificates & secrets** → new client secret → copy the *value* into `.env` as `MS_CLIENT_SECRET`.
3. Copy the **Application (client) ID** → `MS_CLIENT_ID`, and **Directory (tenant) ID** → `MS_TENANT_ID`.
4. **API permissions** → add:
   - `Microsoft Graph` → **Delegated** → `User.Read` (sign-in)
   - `Microsoft Graph` → **Application** → `Sites.Selected` (SharePoint filing) → **Grant admin consent**
5. Grant this app access to just your target SharePoint site (keeps it from touching any other site in the tenant):
   ```
   POST https://graph.microsoft.com/v1.0/sites/{site-id}/permissions
   {
     "roles": ["write"],
     "grantedToIdentities": [
       {"application": {"id": "<MS_CLIENT_ID>", "displayName": "DocuVault AI"}}
     ]
   }
   ```
   Run this once via Graph Explorer (https://developer.microsoft.com/graph/graph-explorer) signed in as a site admin.
6. Find your site ID and drive (library) ID:
   ```
   GET https://graph.microsoft.com/v1.0/sites/{hostname}:/sites/{site-path}
   GET https://graph.microsoft.com/v1.0/sites/{site-id}/drives
   ```
   Put these into `.env` as `GRAPH_SITE_ID` and `GRAPH_DRIVE_ID`.

For production, swap the client secret for **certificate-based** client credentials in
`graph_service.py` and `msal_auth.py` (MSAL supports this directly) — a cert is harder
to leak by accident than a secret string.

### Optional: SharePoint metadata columns

`graph_service.set_item_metadata()` writes `DocumentType`, `CompanyName`,
`DocumentYear`, `AIConfidence` onto each filed item. Add matching columns
(Text/Number) to the target library if you want these visible/filterable
in SharePoint's own UI — if they don't exist yet, the write is skipped
with a logged warning rather than failing the upload.

## 2. Gemini API key

Get a free-tier key at https://aistudio.google.com/apikey and put it in
`.env` as `GEMINI_API_KEY`. Start on `gemini-2.5-flash` — cheap and
accurate enough for this; the free tier is rate-limited (resets midnight
Pacific), so if you ever batch-upload a large backlog, expect to hit the
daily cap and add a paid billing account for headroom (`GEMINI_MODEL` and
pricing can be changed later with no code changes).

## 3. Run it locally

```bash
python3 -m venv .venv
source .venv/bin/activate          # .venv\Scripts\activate on Windows
pip install -r requirements.txt

cp .env.example .env               # then fill in the values from steps 1–2
python manage.py migrate
python manage.py createsuperuser   # for /admin/ access — separate from MS sign-in
python manage.py runserver
```

Visit `http://localhost:8000/` — you'll be redirected to Microsoft sign-in.
`/admin/` uses the Django superuser you just created, not MS sign-in.

## Folder structure logic

Controlled by `FOLDER_STRUCTURE` in `.env` (default `company_name,year` →
`Acme Ltd/2024/`). Reorder or drop entries; `document_type` is also
available as a folder-path segment if you'd rather nest by type instead
of/as well as company or year — it's stored as metadata either way, so
you can leave it out of the path and still filter by it in SharePoint.

## Security notes already built in

- Every page requires Entra ID sign-in (`vault/middleware.py`) except `/admin/`
- Least-privilege Graph scope (`Sites.Selected`, not `Sites.ReadWrite.All`)
- File type/size validated server-side, not just by the file picker
- SHA-256 checksum on every upload — an exact duplicate of an already-filed
  document is routed to review instead of silently refiled
- Local scan copies are deleted once successfully filed (`temp_file.delete()`
  in `document_processor.py`) — nothing lingers on disk after filing
- Full audit trail per document in `ProcessingLog` (who uploaded, every
  pipeline step, who reviewed/corrected what)
- CSRF protection on every AJAX call; session cookies `HttpOnly`/`SameSite=Lax`

## Still worth adding before this goes into real production use

- **Malware scanning on upload** (e.g. ClamAV) — not included; add before
  exposing this beyond a trusted internal network
- **HTTPS** — if this ever leaves localhost/LAN, put it behind a reverse
  proxy (Caddy/nginx) with TLS; several `settings.py` security flags
  (`SESSION_COOKIE_SECURE`, etc.) already auto-enable when `DEBUG=False`
- **Backups** for `db.sqlite3` (or move to Postgres) — it's the only record
  of what got filed where and by whom
- A compiled Tailwind build (`npx tailwindcss`) instead of the CDN script,
  once you're past local dev — the CDN Play script is fine for
  development but ships unused CSS and recompiles client-side
