from urllib.parse import quote
from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import NoReverseMatch, reverse


class LoginRequiredMiddleware:
    """
    Ensures every page requires an authenticated session, except health checks,
    Django admin, static/media assets, and authentication endpoints.
    Returns 401 JSON for expired AJAX/API calls instead of HTML redirects.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def _get_login_url(self) -> str:
        configured = getattr(settings, "LOGIN_URL", "/login/")
        try:
            return reverse(configured)
        except NoReverseMatch:
            try:
                return reverse("login")
            except NoReverseMatch:
                return configured if configured.startswith("/") else "/login/"

    def __call__(self, request):
        path = request.path
        login_url = self._get_login_url()

        static_prefix = "/" + getattr(settings, "STATIC_URL", "static/").lstrip("/")
        media_prefix = "/" + getattr(settings, "MEDIA_URL", "media/").lstrip("/")

        exempt_exact = {
            login_url,
            "/healthz/",
            "/favicon.ico",
            "/robots.txt",
        }
        exempt_prefixes = (
            "/admin/",
            "/auth/",
            "/login/",
            "/logout/",
            static_prefix,
            media_prefix,
        )

        if (
            request.user.is_authenticated
            or path in exempt_exact
            or any(path.startswith(prefix) for prefix in exempt_prefixes)
        ):
            return self.get_response(request)

        # Return 401 JSON for XHR, Fetch JSON, or /api/ routes
        accept_header = request.headers.get("Accept", "")
        if (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or path.startswith("/api/")
            or "application/json" in accept_header
        ):
            return JsonResponse(
                {
                    "ok": False,
                    "error": "Session expired. Please sign in again.",
                    "login_url": login_url,
                },
                status=401,
            )

        next_target = quote(request.get_full_path(), safe="/?=&")
        return redirect(f"{login_url}?next={next_target}")