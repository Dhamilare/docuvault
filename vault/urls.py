from django.urls import path
from . import views

urlpatterns = [
    # --- Auth (Microsoft 365 MSAL) ---
    path("login/", views.login_view, name="login"),
    path("auth/callback/", views.auth_callback, name="auth_callback"),
    path("logout/", views.logout_view, name="logout"),

    # --- Core Application Pages ---
    path("", views.dashboard, name="dashboard"),
    path("upload/", views.upload_page, name="upload"),
    path("documents/", views.document_list, name="document_list"),
    path("review/", views.review_queue, name="review_queue"),
    path("categories/", views.categories_list, name="categories_list"),

    # --- AJAX & Client APIs ---
    path("api/upload/", views.api_upload, name="api_upload"),
    path("api/documents/<int:pk>/status/", views.api_document_status, name="api_document_status"),
    path("api/documents/<int:pk>/detail/", views.api_document_detail, name="api_document_detail"),
    path("api/documents/<int:pk>/review/", views.api_review_document, name="api_review_document"),
    path("api/documents/<int:pk>/retry/", views.api_retry_document, name="api_retry_document"),
    path("api/documents/<int:pk>/delete/", views.api_delete_document, name="api_delete_document"),
]