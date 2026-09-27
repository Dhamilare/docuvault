from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import NoReverseMatch, reverse


class LoginRequiredMiddleware:
    """
    Ensures every page requires a signed-in Microsoft Entra ID session,
    except the Django admin, static/media files, and authentication endpoints.
    Returns 401 JSON for expired AJAX/API calls instead of redirect loops.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path

        # Determine login URL safely
        try:
            login_url = reverse("login")
        except NoReverseMatch:
            try:
                login_url = reverse("login")
            except NoReverseMatch:
                login_url = getattr(settings, "LOGIN_URL", "/login/")

        # Paths that must never be blocked
        exempt_prefixes = (
            "/admin",
            "/auth",
            "/login",
            "/static",
            "/media",
            login_url,
        )

        # Allow authenticated users and exempt paths
        if request.user.is_authenticated or any(path.startswith(p) for p in exempt_prefixes):
            return self.get_response(request)

        # For AJAX/API calls, return 401 JSON so client-side JavaScript handles it gracefully
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or path.startswith("/api/"):
            return JsonResponse(
                {"ok": False, "error": "Session expired. Please sign in again.", "login_url": login_url},
                status=401,
            )

        # Standard browser navigation redirect
        return redirect(f"{login_url}?next={path}")