"""
Handles user sign-in against Microsoft Entra ID using MSAL (msal-python)
via the OAuth2 authorization code flow.
"""
import logging
import msal
from django.conf import settings

logger = logging.getLogger("vault")


def _msal_app():
    return msal.ConfidentialClientApplication(
        client_id=settings.MS_CLIENT_ID,
        client_credential=settings.MS_CLIENT_SECRET,
        authority=settings.MS_AUTHORITY,
    )


def build_auth_url(state: str) -> str:
    """Returns the Microsoft login URL."""
    return _msal_app().get_authorization_request_url(
        scopes=settings.MS_SCOPES,
        state=state,
        redirect_uri=settings.MS_REDIRECT_URI,
    )


def acquire_token_by_auth_code(code: str) -> dict:
    """Exchanges the authorization code from callback for user tokens."""
    result = _msal_app().acquire_token_by_authorization_code(
        code=code,
        scopes=settings.MS_SCOPES,
        redirect_uri=settings.MS_REDIRECT_URI,
    )
    if "error" in result:
        logger.warning("MSAL sign-in failed: %s", result.get("error_description"))
        raise ValueError(result.get("error_description", result["error"]))
    return result


def extract_profile(token_result: dict) -> dict:
    """Extracts user profile details from the ID token claims."""
    claims = token_result.get("id_token_claims", {})
    return {
        "oid": claims.get("oid", ""),
        "name": claims.get("name", ""),
        "email": claims.get("preferred_username") or claims.get("email", ""),
    }