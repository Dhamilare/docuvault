import hashlib
import json
import logging
import secrets

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import connection, transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .forms import CategoryEditForm, CategoryMergeForm, DocumentReviewForm, DocumentUploadForm
from .models import Document, DocumentCategory, ProcessingLog, SystemConfiguration
from .services import document_processor, msal_auth

logger = logging.getLogger("vault")
User = get_user_model()


# ==============================================================================
# 1. Health Check & MSAL Authentication
# ==============================================================================

@require_GET
def healthz(request):
    """Lightweight liveness/readiness endpoint for load balancers & orchestrators."""
    checks = {"db": False, "cache": False}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        checks["db"] = True
    except Exception as exc:
        logger.error("Healthcheck DB failure: %s", exc)

    try:
        cache.set("vault:healthz", "1", timeout=5)
        checks["cache"] = cache.get("vault:healthz") == "1"
    except Exception as exc:
        logger.warning("Healthcheck cache warning: %s", exc)

    status_code = 200 if checks["db"] else 503
    return JsonResponse({"ok": checks["db"], "checks": checks}, status=status_code)


def _safe_next_url(request, candidate: str) -> str:
    if candidate and url_has_allowed_host_and_scheme(
        url=candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return reverse("dashboard")


def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    state = secrets.token_urlsafe(24)
    request.session["auth_state"] = state
    request.session["next_url"] = _safe_next_url(request, request.GET.get("next", ""))
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

    expected_state = request.session.pop("auth_state", None)
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

    first_name = (profile.get("given_name") or profile.get("name", ""))[:150]
    last_name = (profile.get("family_name") or "")[:150]

    user = User.objects.filter(username=profile["oid"]).first()
    if not user and profile["email"]:
        user = User.objects.filter(email__iexact=profile["email"]).first()

    if not user:
        user = User.objects.create_user(
            username=profile["oid"],
            email=profile["email"],
            first_name=first_name,
            last_name=last_name,
        )
    else:
        changed = []
        if profile["email"] and user.email != profile["email"]:
            user.email = profile["email"]
            changed.append("email")
        if first_name and user.first_name != first_name:
            user.first_name = first_name
            changed.append("first_name")
        if last_name and user.last_name != last_name:
            user.last_name = last_name
            changed.append("last_name")
        if changed:
            user.save(update_fields=changed)

    login(request, user)
    next_url = _safe_next_url(request, request.session.pop("next_url", ""))
    return redirect(next_url)


@require_POST
def logout_view(request):
    logout(request)
    return redirect("login")


# ==============================================================================
# 2. Main Dashboard & Queues
# ==============================================================================

@login_required
def dashboard(request):
    agg = Document.objects.aggregate(
        total=Count("id"),
        pending=Count("id", filter=Q(status=Document.Status.PENDING)),
        processing=Count("id", filter=Q(status=Document.Status.PROCESSING)),
        needs_review=Count("id", filter=Q(status=Document.Status.NEEDS_REVIEW)),
        filed=Count("id", filter=Q(status=Document.Status.FILED)),
        failed=Count("id", filter=Q(status=Document.Status.FAILED)),
    )
    counts = {
        Document.Status.PENDING.value: agg["pending"],
        Document.Status.PROCESSING.value: agg["processing"],
        Document.Status.NEEDS_REVIEW.value: agg["needs_review"],
        Document.Status.FILED.value: agg["filed"],
        Document.Status.FAILED.value: agg["failed"],
    }

    recent = Document.objects.select_related("category", "uploaded_by")[:8]
    top_categories = (
        DocumentCategory.objects.filter(is_hidden=False, merged_into__isnull=True)
        .annotate(active_count=Count("documents"))
        .order_by("-active_count", "name")[:6]
    )
    return render(
        request,
        "vault/dashboard.html",
        {
            "counts": counts,
            "recent": recent,
            "top_categories": top_categories,
            "total_docs": agg["total"],
        },
    )


@login_required
def upload_page(request):
    config = SystemConfiguration.get_settings()
    return render(request, "vault/upload.html", {"MAX_UPLOAD_SIZE_MB": config.max_file_size_mb})


def _apply_filters(request, qs):
    status = request.GET.get("status")
    category = request.GET.get("category")
    company = request.GET.get("company")
    year = request.GET.get("year")
    query = request.GET.get("q")

    if status and status in Document.Status.values:
        qs = qs.filter(status=status)
    if category and category.isdigit():
        qs = qs.filter(category_id=int(category))
    if company:
        qs = qs.filter(company_name__icontains=company.strip())
    if year and year.isdigit():
        qs = qs.filter(document_year=int(year))
    if query:
        q_clean = query.strip()
        qs = qs.filter(
            Q(original_filename__icontains=q_clean)
            | Q(filed_filename__icontains=q_clean)
            | Q(company_name__icontains=q_clean)
            | Q(document_title__icontains=q_clean)
            | Q(ai_summary__icontains=q_clean)
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
        "categories": DocumentCategory.objects.filter(
            is_hidden=False, merged_into__isnull=True
        ).order_by("name"),
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
    qs = (
        Document.objects.filter(status__in=[Document.Status.NEEDS_REVIEW, Document.Status.FAILED])
        .select_related("category", "uploaded_by", "duplicate_of")
        .order_by("-created_at")
    )
    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page", 1))
    return render(request, "vault/review_queue.html", {"items": page_obj, "page_obj": page_obj})


@login_required
def categories_list(request):
    categories = (
        DocumentCategory.objects.filter(merged_into__isnull=True)
        .annotate(doc_count=Count("documents"))
        .order_by("-doc_count", "name")
    )
    merge_form = CategoryMergeForm()

    if request.method == "POST" and "merge_action" in request.POST:
        if getattr(settings, "VAULT_REQUIRE_STAFF_FOR_ADMIN_ACTIONS", False) and not request.user.is_staff:
            messages.error(request, "Administrator privileges are required to merge categories.")
            return redirect("categories_list")

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


@login_required
@require_http_methods(["GET", "POST"])
def category_edit(request, pk):
    category = get_object_or_404(DocumentCategory, pk=pk)
    if request.method == "POST":
        form = CategoryEditForm(request.POST, instance=category)
        if form.is_valid():
            form.save()
            messages.success(request, f"Updated category '{category.name}'.")
            return redirect("categories_list")
    else:
        form = CategoryEditForm(instance=category)
    return render(request, "vault/partials/_category_edit_modal.html", {"category": category, "form": form})


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
        "uuid": str(document.uuid),
        "filename": document.original_filename,
        "filed_filename": document.filed_filename or document.get_sanitized_filename(),
        "file_size": document.formatted_size,
        "page_count": document.page_count,
        "has_temp_file": document.has_temp_file,
        "status": document.status,
        "status_label": document.get_status_display(),
        "category": document.category.name if document.category else None,
        "category_color": document.category.color if document.category else "slate",
        "company_name": document.company_name or "—",
        "document_title": document.document_title,
        "document_date": document.document_date.strftime("%Y-%m-%d") if document.document_date else None,
        "document_year": document.document_year,
        "confidence": document.confidence_score,
        "confidence_percent": document.confidence_percent,
        "ai_summary": document.ai_summary,
        "error_message": document.error_message,
        "sharepoint_web_url": document.sharepoint_web_url,
        "sharepoint_folder_path": document.sharepoint_folder_path,
        "sharepoint_metadata_status": document.sharepoint_metadata_status,
        "is_duplicate": bool(document.duplicate_of_id),
        "is_done": is_done,
        "detail_url": reverse("api_document_detail", args=[document.pk]),
    }


def _extract_doc_ids(request) -> list[int]:
    raw_ids = []
    if request.body:
        try:
            payload = json.loads(request.body)
            if isinstance(payload, dict):
                raw_ids = payload.get("document_ids", [])
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    if not raw_ids:
        raw_ids = request.POST.getlist("document_ids[]") or request.POST.getlist("document_ids")

    valid_ids = []
    for item in raw_ids:
        try:
            valid_ids.append(int(item))
        except (ValueError, TypeError):
            continue
    return valid_ids[:25]


@login_required
@require_POST
def api_upload(request):
    form = DocumentUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        errors = "; ".join(str(e) for field in form.errors.values() for e in field)
        return JsonResponse({"ok": False, "error": errors, "field_errors": form.errors.get_json_data()}, status=400)

    uploaded_file = form.cleaned_data["temp_file"]
    checksum = _checksum(uploaded_file)

    with transaction.atomic():
        document = form.save(commit=False)
        document.original_filename = uploaded_file.name[:255]
        document.file_size_bytes = uploaded_file.size
        document.content_type = (uploaded_file.content_type or "")[:120]
        document.checksum_sha256 = checksum
        document.uploaded_by = request.user
        document.status = Document.Status.PENDING
        document.save()

        document.log_step(
            ProcessingLog.Step.UPLOADED,
            f"Uploaded {document.original_filename} ({document.formatted_size}). SHA-256: {checksum[:12]}...",
            actor=request.user,
        )

        # Check for identical active or already-filed duplicate
        duplicate = (
            Document.objects.filter(
                checksum_sha256=checksum,
                status__in=[
                    Document.Status.FILED,
                    Document.Status.PROCESSING,
                    Document.Status.PENDING,
                ],
            )
            .exclude(pk=document.pk)
            .order_by("-created_at")
            .first()
        )

        if duplicate:
            document.mark(
                Document.Status.NEEDS_REVIEW,
                duplicate_of=duplicate,
                ai_summary=(
                    f"Potential duplicate of '{duplicate.original_filename}' "
                    f"(#{duplicate.pk}, status: {duplicate.get_status_display()})."
                ),
            )
            document.log_step(
                ProcessingLog.Step.DUPLICATE_DETECTED,
                f"Matched SHA-256 checksum of Document #{duplicate.pk}. Routed to Review Queue.",
                actor=request.user,
                metadata={"duplicate_of_id": duplicate.pk},
            )
        else:
            # Schedule asynchronous processing strictly after DB commit
            transaction.on_commit(lambda doc_id=document.pk: document_processor.enqueue_document_processing(doc_id))

    document.refresh_from_db()
    return JsonResponse(_document_json(document), status=202 if document.status == Document.Status.PENDING else 200)


@login_required
@require_GET
def api_document_status(request, pk):
    document = get_object_or_404(Document.objects.select_related("category"), pk=pk)
    return JsonResponse(_document_json(document))


@login_required
@require_GET
def api_document_detail(request, pk):
    document = get_object_or_404(
        Document.objects.select_related("category", "uploaded_by", "reviewed_by", "duplicate_of")
        .prefetch_related("logs__actor"),
        pk=pk,
    )
    return render(
        request,
        "vault/partials/_document_detail_modal.html",
        {"doc": document, "logs": document.logs.all()},
    )


@login_required
@require_http_methods(["GET", "POST"])
def api_review_document(request, pk):
    """
    Handles operator manual metadata verification and filing to SharePoint.
    Preserves original AI confidence score while flagging `is_manually_edited=True`.
    """
    document = get_object_or_404(Document, pk=pk)

    if request.method == "POST":
        form = DocumentReviewForm(request.POST, instance=document)
        if not form.is_valid():
            errors = "; ".join(str(e) for field in form.errors.values() for e in field)
            return JsonResponse(
                {"ok": False, "error": errors, "field_errors": form.errors.get_json_data()},
                status=400,
            )

        previous_category = document.category
        reviewed_doc = form.save(commit=False)

        new_cat_name = (form.cleaned_data.get("new_category_name") or "").strip()
        if new_cat_name:
            reviewed_doc.category = DocumentCategory.get_or_create_discovered(new_cat_name)

        reviewed_doc.is_manually_edited = True
        reviewed_doc.reviewed_by = request.user
        reviewed_doc.reviewed_at = timezone.now()
        if reviewed_doc.status == Document.Status.FAILED and not form.cleaned_data.get("file_to_sharepoint_now", True):
            reviewed_doc.status = Document.Status.NEEDS_REVIEW
        reviewed_doc.save()

        if previous_category and previous_category != reviewed_doc.category:
            previous_category.update_document_count()
        if reviewed_doc.category:
            reviewed_doc.category.update_document_count()

        reviewed_doc.log_step(
            ProcessingLog.Step.REVIEWED,
            f"Metadata verified by {request.user.get_full_name() or request.user.username}.",
            actor=request.user,
        )

        if form.cleaned_data.get("file_to_sharepoint_now", True):
            if not reviewed_doc.has_temp_file:
                return JsonResponse(
                    {"ok": False, "error": "The transient scan file is no longer in storage. Please re-upload."},
                    status=400,
                )

            filing_success, err_msg = document_processor.file_reviewed_document(reviewed_doc, actor=request.user)
            if not filing_success:
                return JsonResponse({"ok": False, "error": f"SharePoint Filing Error: {err_msg}"}, status=502)

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
        return JsonResponse({"ok": False, "error": "Transient scan file was purged — please re-upload."}, status=400)

    document.mark(Document.Status.PENDING, error_message="")
    document.log_step(ProcessingLog.Step.RETRIED, "Manual retry initiated by operator.", actor=request.user)
    document_processor.enqueue_document_processing(document.pk)
    document.refresh_from_db()
    return JsonResponse(_document_json(document), status=202)


@login_required
@require_POST
def api_retry_metadata(request, pk):
    document = get_object_or_404(Document, pk=pk)
    ok, err = document_processor.retry_metadata_sync(document, actor=request.user)
    if not ok:
        return JsonResponse({"ok": False, "error": err}, status=502)
    document.refresh_from_db()
    return JsonResponse(_document_json(document))


@login_required
@require_POST
def api_bulk_file(request):
    doc_ids = _extract_doc_ids(request)
    if not doc_ids:
        return JsonResponse({"ok": False, "error": "No valid document IDs selected."}, status=400)

    documents = Document.objects.filter(
        pk__in=doc_ids,
        status__in=[Document.Status.NEEDS_REVIEW, Document.Status.FAILED],
    ).select_related("category")

    filed_count = 0
    failed_count = 0
    errors = []

    for doc in documents:
        if not doc.category:
            doc.category = DocumentCategory.get_or_create_discovered("General Document")
        if not doc.company_name:
            doc.company_name = "Unsorted Company"
        doc.is_manually_edited = True
        doc.reviewed_by = request.user
        doc.reviewed_at = timezone.now()
        doc.save(update_fields=["category", "company_name", "is_manually_edited", "reviewed_by", "reviewed_at", "updated_at"])
        doc.log_step(ProcessingLog.Step.REVIEWED, "Approved via bulk filing action.", actor=request.user)

        success, err = document_processor.file_reviewed_document(doc, actor=request.user)
        if success:
            filed_count += 1
        else:
            failed_count += 1
            errors.append(f"{doc.original_filename}: {err}")

    return JsonResponse({
        "ok": True,
        "filed_count": filed_count,
        "failed_count": failed_count,
        "errors": errors,
    })


@login_required
@require_http_methods(["POST", "DELETE"])
def api_delete_document(request, pk):
    document = get_object_or_404(Document, pk=pk)

    if document.status == Document.Status.FILED:
        return JsonResponse(
            {"ok": False, "error": "Filed documents cannot be discarded here. Manage them in SharePoint."},
            status=400,
        )

    cat = document.category
    document.purge_temp_file(force=True)
    doc_id = document.pk
    document.delete()
    if cat:
        cat.update_document_count()

    return JsonResponse({"ok": True, "id": doc_id, "message": "Document discarded successfully."})


@login_required
@require_POST
def api_bulk_discard(request):
    doc_ids = _extract_doc_ids(request)
    if not doc_ids:
        return JsonResponse({"ok": False, "error": "No valid document IDs selected."}, status=400)

    documents = Document.objects.filter(pk__in=doc_ids).exclude(status=Document.Status.FILED)
    discarded_ids = []
    affected_categories = set()

    for doc in documents:
        if doc.category:
            affected_categories.add(doc.category)
        doc.purge_temp_file(force=True)
        discarded_ids.append(doc.pk)
        doc.delete()

    for cat in affected_categories:
        cat.update_document_count()

    return JsonResponse({
        "ok": True,
        "discarded_count": len(discarded_ids),
        "discarded_ids": discarded_ids,
    })