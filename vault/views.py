import hashlib
import logging
import secrets
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from .forms import CategoryMergeForm, DocumentReviewForm, DocumentUploadForm
from .models import Document, DocumentCategory, ProcessingLog
from .services import document_processor, msal_auth
from django.views.decorators.http import require_http_methods
logger = logging.getLogger("vault")
User = get_user_model()

# ==============================================================================
# 1. MSAL / Microsoft 365 Authentication
# ==============================================================================

def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    state = secrets.token_urlsafe(24)
    request.session["auth_state"] = state
    request.session["next_url"] = request.GET.get("next", reverse("dashboard"))
    auth_url = msal_auth.build_auth_url(state)
    return render(request, "vault/login.html", {"auth_url": auth_url})

def auth_callback(request):
    if request.GET.get("error"):
        return render(
            request,
            "vault/login.html",
            {
                "error": request.GET.get("error_description", "Sign-in was cancelled."),
                "auth_url": None,
            },
        )

    expected_state = request.session.get("auth_state")
    if not expected_state or request.GET.get("state") != expected_state:
        return render(
            request,
            "vault/login.html",
            {"error": "Sign-in session expired — please try again.", "auth_url": None},
        )

    try:
        code = request.GET.get("code", "")
        token_result = msal_auth.acquire_token_by_auth_code(code)
        profile = msal_auth.extract_profile(token_result)
    except ValueError as exc:
        return render(request, "vault/login.html", {"error": str(exc), "auth_url": None})

    if not profile.get("oid"):
        return render(
            request,
            "vault/login.html",
            {"error": "Microsoft identity token missing subject claim.", "auth_url": None},
        )

    user, _ = User.objects.get_or_create(
        username=profile["oid"],
        defaults={"email": profile["email"], "first_name": profile["name"][:150]},
    )
    if user.email != profile["email"] or user.first_name != profile["name"][:150]:
        user.email = profile["email"]
        user.first_name = profile["name"][:150]
        user.save(update_fields=["email", "first_name"])

    login(request, user)
    next_url = request.session.pop("next_url", None) or reverse("dashboard")
    return redirect(next_url)

def logout_view(request):
    logout(request)
    return redirect("login")

# ==============================================================================
# 2. Main Dashboard & Queues
# ==============================================================================

@login_required
def dashboard(request):
    counts = {
        status.value: Document.objects.filter(status=status).count()
        for status in Document.Status
    }
    recent = Document.objects.select_related("category", "uploaded_by")[:8]
    top_categories = (
        DocumentCategory.objects.filter(is_hidden=False)
        .annotate(active_count=Count("documents"))
        .order_by("-document_count")[:6]
    )
    return render(
        request,
        "vault/dashboard.html",
        {
            "counts": counts,
            "recent": recent,
            "top_categories": top_categories,
            "total_docs": Document.objects.count(),
        },
    )

@login_required
def upload_page(request):
    max_mb = getattr(settings, "MAX_UPLOAD_SIZE_MB", 25)
    return render(request, "vault/upload.html", {"MAX_UPLOAD_SIZE_MB": max_mb})

def _apply_filters(request, qs):
    status = request.GET.get("status")
    category = request.GET.get("category")
    company = request.GET.get("company")
    year = request.GET.get("year")
    query = request.GET.get("q")

    if status and status in Document.Status.values:
        qs = qs.filter(status=status)
    if category:
        qs = qs.filter(category_id=category)
    if company:
        qs = qs.filter(company_name__icontains=company)
    if year and year.isdigit():
        qs = qs.filter(document_year=int(year))
    if query:
        qs = qs.filter(
            Q(original_filename__icontains=query)
            | Q(company_name__icontains=query)
            | Q(ai_summary__icontains=query)
        )
    return qs

@login_required
def document_list(request):
    qs = Document.objects.select_related("category", "uploaded_by")
    qs = _apply_filters(request, qs)

    paginator = Paginator(qs, 15)
    page_obj = paginator.get_page(request.GET.get("page", 1))

    available_years = (
        Document.objects.exclude(document_year__isnull=True)
        .values_list("document_year", flat=True)
        .distinct()
        .order_by("-document_year")
    )

    context = {
        "page_obj": page_obj,
        "categories": DocumentCategory.objects.filter(is_hidden=False).order_by("name"),
        "statuses": Document.Status.choices,
        "available_years": available_years,
        "active_filters": request.GET,
    }
    template = (
        "vault/partials/_document_table.html"
        if request.headers.get("X-Requested-With") == "XMLHttpRequest"
        else "vault/documents_list.html"
    )
    return render(request, template, context)

@login_required
def review_queue(request):
    items = Document.objects.filter(
        status__in=[Document.Status.NEEDS_REVIEW, Document.Status.FAILED]
    ).select_related("category", "uploaded_by")
    return render(request, "vault/review_queue.html", {"items": items})

@login_required
def categories_list(request):
    categories = (
        DocumentCategory.objects.annotate(doc_count=Count("documents"))
        .order_by("-doc_count", "name")
    )
    merge_form = CategoryMergeForm()

    if request.method == "POST" and "merge_action" in request.POST:
        merge_form = CategoryMergeForm(request.POST)
        if merge_form.is_valid():
            source = merge_form.cleaned_data["source_category"]
            target = merge_form.cleaned_data["target_category"]
            source.merge_into_target(target)
            messages.success(request, f"Merged '{source.name}' into '{target.name}'.")
            return redirect("categories_list")

    return render(
        request,
        "vault/categories.html",
        {"categories": categories, "merge_form": merge_form},
    )

# ==============================================================================
# 3. AJAX Endpoints & Document Operations
# ==============================================================================

def _checksum(uploaded_file) -> str:
    hasher = hashlib.sha256()
    for chunk in uploaded_file.chunks():
        hasher.update(chunk)
    uploaded_file.seek(0)
    return hasher.hexdigest()

def _document_json(document: Document) -> dict:
    is_done = document.status in (
        Document.Status.FILED,
        Document.Status.NEEDS_REVIEW,
        Document.Status.FAILED,
    )
    return {
        "ok": True,
        "id": document.pk,
        "filename": document.original_filename,
        "file_size": document.formatted_size,
        "has_temp_file": document.has_temp_file,
        "status": document.status,
        "status_label": document.get_status_display(),
        "category": document.category.name if document.category else None,
        "category_color": document.category.color if document.category else "slate",
        "company_name": document.company_name or "—",
        "document_date": document.document_date.strftime("%Y-%m-%d") if document.document_date else None,
        "document_year": document.document_year,
        "confidence": document.confidence_score,
        "confidence_percent": document.confidence_percent,
        "ai_summary": document.ai_summary,
        "error_message": document.error_message,
        "sharepoint_web_url": document.sharepoint_web_url,
        "sharepoint_folder_path": document.sharepoint_folder_path,
        "is_done": is_done,
        "detail_url": reverse("api_document_detail", args=[document.pk]),
    }

@login_required
@require_POST
def api_upload(request):
    form = DocumentUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        errors = "; ".join(str(e) for field in form.errors.values() for e in field)
        return JsonResponse({"ok": False, "error": errors}, status=400)

    uploaded_file = form.cleaned_data["temp_file"]
    checksum = _checksum(uploaded_file)

    document = form.save(commit=False)
    document.original_filename = uploaded_file.name
    document.file_size_bytes = uploaded_file.size
    document.content_type = uploaded_file.content_type or ""
    document.checksum_sha256 = checksum
    document.uploaded_by = request.user
    document.status = Document.Status.PROCESSING
    document.save()

    document.log_step(
        ProcessingLog.Step.UPLOADED,
        f"Uploaded {uploaded_file.name} ({document.formatted_size}). Checksum: {checksum[:8]}...",
        actor=request.user,
    )

    # Check for identical duplicate
    duplicate = (
        Document.objects.filter(checksum_sha256=checksum, status=Document.Status.FILED)
        .exclude(pk=document.pk)
        .first()
    )
    if duplicate:
        document.mark(
            Document.Status.NEEDS_REVIEW,
            ai_summary=f"Identical duplicate of already-filed document: '{duplicate.original_filename}' (#{duplicate.pk}).",
        )
        document.log_step(
            ProcessingLog.Step.VALIDATED,
            "Flagged as duplicate of filed document.",
            actor=request.user,
        )
    else:
        # Run processing safely
        try:
            document_processor.process_document(document.pk)
        except Exception as exc:
            logger.exception("Initial processing error for document %s: %s", document.pk, exc)
            document.mark(
                Document.Status.NEEDS_REVIEW,
                error_message=str(exc),
                ai_summary="AI classification temporarily unavailable. Queued for manual review.",
            )

    document.refresh_from_db()
    return JsonResponse(_document_json(document))

@login_required
@require_GET
def api_document_status(request, pk):
    document = get_object_or_404(Document, pk=pk)
    return JsonResponse(_document_json(document))

@login_required
@require_GET
def api_document_detail(request, pk):
    document = get_object_or_404(
        Document.objects.select_related("category", "uploaded_by", "reviewed_by").prefetch_related("logs"),
        pk=pk,
    )
    return render(
        request,
        "vault/partials/_document_detail_modal.html",
        {"doc": document, "logs": document.logs.all()},
    )

@login_required
def api_review_document(request, pk):
    """
    Handles operator manual metadata verification and filing to SharePoint.
    """
    document = get_object_or_404(Document, pk=pk)

    if request.method == "POST":
        form = DocumentReviewForm(request.POST, instance=document)
        if not form.is_valid():
            errors = "; ".join(str(e) for field in form.errors.values() for e in field)
            return JsonResponse({"ok": False, "error": errors}, status=400)

        reviewed_doc = form.save(commit=False)

        new_cat_name = form.cleaned_data.get("new_category_name")
        if new_cat_name:
            reviewed_doc.category = DocumentCategory.get_or_create_discovered(new_cat_name.strip())

        reviewed_doc.is_manually_edited = True
        reviewed_doc.reviewed_by = request.user
        reviewed_doc.reviewed_at = timezone.now()
        reviewed_doc.confidence_score = 1.0
        reviewed_doc.save()

        if reviewed_doc.category:
            reviewed_doc.category.update_document_count()

        reviewed_doc.log_step(
            ProcessingLog.Step.REVIEWED,
            f"Metadata verified by {request.user.get_full_name() or request.user.username}.",
            actor=request.user,
        )

        # File to SharePoint
        if form.cleaned_data.get("file_to_sharepoint_now", True):
            if not reviewed_doc.has_temp_file:
                return JsonResponse({
                    "ok": False,
                    "error": "The local scan file is not available on disk. Please re-upload the document."
                }, status=400)

            filing_success, err_msg = document_processor.file_reviewed_document(reviewed_doc, actor=request.user)
            if not filing_success:
                return JsonResponse({
                    "ok": False,
                    "error": f"SharePoint Filing Error: {err_msg}"
                }, status=502)

        reviewed_doc.refresh_from_db()
        return JsonResponse(_document_json(reviewed_doc))

    form = DocumentReviewForm(instance=document)
    return render(
        request,
        "vault/partials/_document_review_modal.html",
        {"doc": document, "form": form},
    )

@login_required
@require_POST
def api_retry_document(request, pk):
    document = get_object_or_404(Document, pk=pk)
    if document.status not in (Document.Status.FAILED, Document.Status.NEEDS_REVIEW):
        return JsonResponse({"ok": False, "error": "Only unfiled documents can be retried."}, status=400)
    if not document.has_temp_file:
        return JsonResponse({"ok": False, "error": "Local scan was purged — please re-upload."}, status=400)

    document.log_step(ProcessingLog.Step.RETRIED, "Manual retry initiated by operator.", actor=request.user)
    document_processor.process_document(document.pk)
    document.refresh_from_db()
    return JsonResponse(_document_json(document))


@login_required
@require_http_methods(["POST", "DELETE"])
def api_delete_document(request, pk):
    document = get_object_or_404(Document, pk=pk)

    if document.status == Document.Status.FILED:
        return JsonResponse(
            {"ok": False, "error": "Filed documents cannot be discarded here. Delete them in SharePoint."},
            status=400,
        )

    # Clean local disk scan safely
    document.purge_temp_file(force=True)
    doc_id = document.pk
    document.delete()

    return JsonResponse({"ok": True, "id": doc_id, "message": "Document discarded successfully."})