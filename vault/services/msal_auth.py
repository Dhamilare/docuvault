"""
Handles user sign-in against Microsoft Entra ID using MSAL (msal-python)
via the OAuth2 authorization code flow.
"""
import logging
import msal
from django.conf import settings

logger = logging.getLogger("vault")


def _msal_app() -> msal.ConfidentialClientApplication:
    if not (
        getattr(settings, "MS_CLIENT_ID", "")
        and getattr(settings, "MS_CLIENT_SECRET", "")
        and getattr(settings, "MS_AUTHORITY", "")
    ):
        raise ValueError("Microsoft Entra ID credentials are not configured.")

    return msal.ConfidentialClientApplication(
        client_id=settings.MS_CLIENT_ID,
        client_credential=settings.MS_CLIENT_SECRET,
        authority=settings.MS_AUTHORITY,
    )


def build_auth_url(state: str) -> str:
    """Returns the Microsoft Entra ID login URL."""
    return _msal_app().get_authorization_request_url(
        scopes=settings.MS_SCOPES,
        state=state,
        redirect_uri=settings.MS_REDIRECT_URI,
    )


def acquire_token_by_auth_code(code: str) -> dict:
    """Exchanges the authorization code from callback for user tokens."""
    if not code:
        raise ValueError("Missing authorization code from Microsoft identity callback.")

    result = _msal_app().acquire_token_by_authorization_code(
        code=code,
        scopes=settings.MS_SCOPES,
        redirect_uri=settings.MS_REDIRECT_URI,
    )
    if "error" in result:
        logger.warning("MSAL sign-in failed: %s", result.get("error_description"))
        raise ValueError(result.get("error_description") or result["error"])
    return result


def extract_profile(token_result: dict) -> dict:
    """
    Extracts and validates user profile claims from the ID token.
    Enforces single-tenant ID verification when MS_TENANT_ID is a fixed tenant GUID.
    """
    claims = token_result.get("id_token_claims") or {}
    configured_tenant = (getattr(settings, "MS_TENANT_ID", "") or "").strip().lower()
    token_tenant = (claims.get("tid") or "").strip().lower()

    if configured_tenant and configured_tenant not in ("common", "organizations", "consumers"):
        if token_tenant and token_tenant != configured_tenant:
            logger.warning("Rejected login from unauthorized Entra tenant: %s", token_tenant)
            raise ValueError("User belongs to an unauthorized Microsoft Entra tenant.")

    email = (
        claims.get("email")
        or claims.get("preferred_username")
        or claims.get("upn")
        or ""
    ).strip().lower()

    return {
        "oid": (claims.get("oid") or claims.get("sub") or "").strip(),
        "name": (claims.get("name") or "").strip(),
        "given_name": (claims.get("given_name") or "").strip(),
        "family_name": (claims.get("family_name") or "").strip(),
        "email": email,
    }