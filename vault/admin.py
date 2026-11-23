import json
from django.contrib import admin, messages
from django.utils.html import format_html
from .models import Document, DocumentCategory, ProcessingLog, SystemConfiguration
from .services import document_processor

BADGE_PALETTE = {
    "indigo": ("#4338ca", "#e0e7ff"),
    "emerald": ("#047857", "#d1fae5"),
    "sky": ("#0369a1", "#e0f2fe"),
    "amber": ("#b45309", "#fef3c7"),
    "purple": ("#6d28d9", "#ede9fe"),
    "rose": ("#be123c", "#ffe4e6"),
    "teal": ("#0f766e", "#ccfbf1"),
    "slate": ("#334155", "#f1f5f9"),
}


class ProcessingLogInline(admin.TabularInline):
    model = ProcessingLog
    extra = 0
    can_delete = False
    readonly_fields = ["step", "message", "duration_display", "metadata_pretty", "actor", "created_at"]
    fields = ["created_at", "step", "message", "duration_display", "metadata_pretty", "actor"]
    ordering = ["-created_at"]

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Latency")
    def duration_display(self, obj):
        return f"{obj.duration_ms} ms" if obj.duration_ms else "—"

    @admin.display(description="Telemetry / Payload")
    def metadata_pretty(self, obj):
        if not obj.metadata:
            return "—"
        try:
            formatted = json.dumps(obj.metadata, indent=2)
            return format_html(
                '<pre style="margin:0; font-family:monospace; font-size:11px; max-height:80px; overflow-y:auto; '
                'background:#0f172a; color:#94a3b8; padding:4px 8px; border-radius:6px;">{}</pre>',
                formatted,
            )
        except Exception:
            return str(obj.metadata)


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = [
        "original_filename",
        "status_badge",
        "category_badge",
        "company_name",
        "document_year",
        "confidence_pill",
        "metadata_sync_badge",
        "file_cache_status",
        "sharepoint_link",
        "uploaded_by",
        "created_at",
    ]
    list_filter = [
        "status",
        "sharepoint_metadata_status",
        "category",
        "document_year",
        "is_manually_edited",
        "created_at",
    ]
    search_fields = [
        "uuid",
        "original_filename",
        "filed_filename",
        "company_name",
        "document_title",
        "checksum_sha256",
        "ai_summary",
        "sharepoint_item_id",
    ]
    readonly_fields = [
        "uuid",
        "original_filename",
        "file_size_formatted",
        "content_type",
        "checksum_sha256",
        "page_count",
        "duplicate_of",
        "task_id",
        "attempt_count",
        "locked_at",
        "confidence_score",
        "ai_model_used",
        "ai_input_tokens",
        "ai_output_tokens",
        "ai_summary",
        "extracted_text_excerpt",
        "extracted_entities_display",
        "ai_raw_response_display",
        "sharepoint_site_id",
        "sharepoint_drive_id",
        "sharepoint_folder_path",
        "sharepoint_item_id",
        "sharepoint_list_item_id",
        "sharepoint_etag",
        "sharepoint_link",
        "sharepoint_uploaded_at",
        "sharepoint_metadata_status",
        "sharepoint_metadata_payload_display",
        "error_message_display",
        "is_manually_edited",
        "reviewed_by",
        "reviewed_at",
        "created_at",
        "updated_at",
        "processed_at",
    ]
    inlines = [ProcessingLogInline]
    actions = [
        "action_file_to_sharepoint",
        "action_retry_metadata_sync",
        "action_reprocess_documents",
        "action_discard_documents",
    ]

    fieldsets = (
        ("File & Staging Storage", {
            "fields": (
                "uuid",
                "original_filename",
                "filed_filename",
                "temp_file",
                "file_size_formatted",
                "page_count",
                "content_type",
                "checksum_sha256",
                "duplicate_of",
                "uploaded_by",
            )
        }),
        ("AI Classification & ECM Extraction", {
            "fields": (
                "status",
                "category",
                "company_name",
                "document_title",
                "document_date",
                "document_year",
                "document_language",
                "confidence_score",
                "ai_summary",
                "extracted_entities_display",
                "extracted_text_excerpt",
                "ai_raw_response_display",
            )
        }),
        ("AI Telemetry & Worker State", {
            "fields": (
                "ai_model_used",
                "ai_input_tokens",
                "ai_output_tokens",
                "task_id",
                "attempt_count",
                "locked_at",
            ),
            "classes": ("collapse",),
        }),
        ("Operator Verification", {
            "fields": ("is_manually_edited", "reviewed_by", "reviewed_at")
        }),
        ("SharePoint Filing & Column Sync (ECM)", {
            "fields": (
                "sharepoint_folder_path",
                "sharepoint_site_id",
                "sharepoint_drive_id",
                "sharepoint_item_id",
                "sharepoint_list_item_id",
                "sharepoint_etag",
                "sharepoint_link",
                "sharepoint_uploaded_at",
                "sharepoint_metadata_status",
                "sharepoint_metadata_payload_display",
                "error_message_display",
            )
        }),
        ("Audit Timestamps", {
            "fields": ("created_at", "updated_at", "processed_at"),
            "classes": ("collapse",),
        }),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            "category", "uploaded_by", "reviewed_by", "duplicate_of"
        )

    @admin.display(description="Status")
    def status_badge(self, obj):
        colors = {
            Document.Status.PENDING: ("#475569", "#f1f5f9"),
            Document.Status.PROCESSING: ("#0284c7", "#e0f2fe"),
            Document.Status.NEEDS_REVIEW: ("#d97706", "#fef3c7"),
            Document.Status.FILED: ("#059669", "#d1fae5"),
            Document.Status.FAILED: ("#dc2626", "#fee2e2"),
        }
        fg, bg = colors.get(obj.status, ("#475569", "#f1f5f9"))
        return format_html(
            '<span style="background:{}; color:{}; padding:3px 9px; border-radius:9999px; '
            'font-weight:700; font-size:11px; text-transform:uppercase; letter-spacing:0.025em;">{}</span>',
            bg, fg, obj.get_status_display(),
        )

    @admin.display(description="Category")
    def category_badge(self, obj):
        if not obj.category:
            return format_html('<span style="color:#94a3b8; font-style:italic;">Uncategorized</span>')
        fg, bg = BADGE_PALETTE.get(obj.category.color, ("#5b21b6", "#ede9fe"))
        return format_html(
            '<span style="background:{}; color:{}; padding:2px 8px; border-radius:6px; font-weight:600; font-size:11px;">{}</span>',
            bg, fg, obj.category.name,
        )

    @admin.display(description="Confidence")
    def confidence_pill(self, obj):
        if obj.confidence_score is None:
            return "—"
        pct = obj.confidence_percent
        color = "#059669" if pct >= 80 else ("#d97706" if pct >= 60 else "#dc2626")
        return format_html('<strong style="color:{}; font-size:12px;">{}%</strong>', color, pct)

    @admin.display(description="ECM Sync")
    def metadata_sync_badge(self, obj):
        colors = {
            Document.MetadataSyncStatus.SYNCED: "#059669",
            Document.MetadataSyncStatus.FAILED: "#dc2626",
            Document.MetadataSyncStatus.PENDING: "#d97706",
            Document.MetadataSyncStatus.NOT_APPLICABLE: "#94a3b8",
        }
        color = colors.get(obj.sharepoint_metadata_status, "#94a3b8")
        return format_html(
            '<span style="color:{}; font-size:11px; font-weight:600;">{}</span>',
            color, obj.get_sharepoint_metadata_status_display(),
        )

    @admin.display(description="Local Scan")
    def file_cache_status(self, obj):
        if obj.has_temp_file:
            return format_html('<span style="color:#059669; font-weight:600;">&#x2714; Cached</span>')
        if obj.status == Document.Status.FILED:
            return format_html('<span style="color:#94a3b8; font-size:11px;">Purged (Filed)</span>')
        return format_html('<span style="color:#dc2626; font-size:11px;">Missing</span>')

    @admin.display(description="SharePoint Link")
    def sharepoint_link(self, obj):
        if obj.sharepoint_web_url:
            return format_html(
                '<a href="{}" target="_blank" rel="noopener noreferrer" style="color:#2563eb; font-weight:600; text-decoration:underline;">'
                'Open in SharePoint &rarr;</a>',
                obj.sharepoint_web_url,
            )
        return "—"

    @admin.display(description="File Size")
    def file_size_formatted(self, obj):
        return obj.formatted_size

    @admin.display(description="Error Callout")
    def error_message_display(self, obj):
        if not obj.error_message:
            return "—"
        return format_html(
            '<div style="background:#fee2e2; border-left:4px solid #dc2626; color:#991b1b; padding:8px 12px; border-radius:4px; font-family:monospace; font-size:12px;">{}</div>',
            obj.error_message,
        )

    def _pretty_json(self, data, empty_label="—", text_color="#94a3b8"):
        if not data:
            return empty_label
        try:
            formatted = json.dumps(data, indent=2)
            return format_html(
                '<pre style="background:#0f172a; color:{}; padding:8px 12px; border-radius:6px; font-size:11px; max-height:220px; overflow:auto;">{}</pre>',
                text_color, formatted,
            )
        except Exception:
            return str(data)

    @admin.display(description="Extracted Entities")
    def extracted_entities_display(self, obj):
        return self._pretty_json(obj.extracted_entities, "None discovered", "#38bdf8")

    @admin.display(description="AI Raw Payload")
    def ai_raw_response_display(self, obj):
        return self._pretty_json(obj.ai_raw_response)

    @admin.display(description="Synced SharePoint Columns")
    def sharepoint_metadata_payload_display(self, obj):
        return self._pretty_json(obj.sharepoint_metadata_payload, "None", "#34d399")

    # --- Batch Actions ---

    @admin.action(description="File selected documents to SharePoint (max 25)")
    def action_file_to_sharepoint(self, request, queryset):
        success_count = 0
        fail_count = 0
        for doc in queryset[:25]:
            if not doc.has_temp_file:
                fail_count += 1
                continue
            try:
                ok, _ = document_processor.file_reviewed_document(doc, actor=request.user)
                if ok:
                    success_count += 1
                else:
                    fail_count += 1
            except Exception:
                fail_count += 1

        if success_count:
            self.message_user(request, f"Successfully filed {success_count} document(s) to SharePoint.", level=messages.SUCCESS)
        if fail_count:
            self.message_user(request, f"{fail_count} document(s) failed or lacked local scan files.", level=messages.WARNING)

    @admin.action(description="Retry SharePoint metadata column sync on filed docs")
    def action_retry_metadata_sync(self, request, queryset):
        ok_count = 0
        fail_count = 0
        for doc in queryset.filter(status=Document.Status.FILED)[:25]:
            ok, _ = document_processor.retry_metadata_sync(doc, actor=request.user)
            if ok:
                ok_count += 1
            else:
                fail_count += 1
        self.message_user(request, f"Metadata synced: {ok_count}, failed: {fail_count}.", level=messages.INFO)

    @admin.action(description="Queue Gemini AI reprocessing for selected documents")
    def action_reprocess_documents(self, request, queryset):
        count = 0
        for doc in queryset.exclude(status=Document.Status.FILED)[:50]:
            if doc.has_temp_file:
                doc.mark(Document.Status.PENDING, error_message="")
                document_processor.enqueue_document_processing(doc.pk)
                count += 1
        self.message_user(request, f"Queued Gemini reprocessing for {count} document(s).", level=messages.INFO)

    @admin.action(description="Discard unfiled documents (Purge transient files)")
    def action_discard_documents(self, request, queryset):
        count = 0
        for doc in queryset.exclude(status=Document.Status.FILED):
            doc.purge_temp_file(force=True)
            doc.delete()
            count += 1
        self.message_user(request, f"Permanently discarded {count} unfiled document(s).", level=messages.SUCCESS)


@admin.register(DocumentCategory)
class DocumentCategoryAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "color_badge",
        "document_count",
        "canonical_name",
        "first_seen_at",
        "is_hidden",
        "merged_into",
    ]
    search_fields = ["name", "canonical_name", "description"]
    list_filter = ["is_hidden", "color"]
    readonly_fields = ["canonical_name", "first_seen_at", "document_count"]
    prepopulated_fields = {"slug": ("name",)}
    actions = ["action_recalculate_counts"]

    @admin.display(description="Badge Color")
    def color_badge(self, obj):
        fg, bg = BADGE_PALETTE.get(obj.color, ("#4338ca", "#e0e7ff"))
        return format_html(
            '<span style="background:{}; color:{}; padding:2px 8px; border-radius:9999px; font-size:11px; font-weight:600;">{}</span>',
            bg, fg, obj.get_color_display(),
        )

    @admin.action(description="Recalculate associated document counts")
    def action_recalculate_counts(self, request, queryset):
        for cat in queryset:
            cat.update_document_count()
        self.message_user(request, f"Updated document counts for {queryset.count()} categories.")


@admin.register(ProcessingLog)
class ProcessingLogAdmin(admin.ModelAdmin):
    list_display = ["document", "step_badge", "duration_display", "actor", "created_at"]
    list_filter = ["step", "created_at"]
    search_fields = ["document__original_filename", "message"]
    readonly_fields = ["document", "step", "message", "duration_ms", "metadata_display", "actor", "created_at"]
    ordering = ["-created_at"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Step")
    def step_badge(self, obj):
        colors = {
            ProcessingLog.Step.FILED: "#059669",
            ProcessingLog.Step.METADATA_SYNCED: "#059669",
            ProcessingLog.Step.FAILED: "#dc2626",
            ProcessingLog.Step.METADATA_FAILED: "#d97706",
            ProcessingLog.Step.CLASSIFYING: "#0284c7",
            ProcessingLog.Step.CLASSIFIED: "#4338ca",
        }
        color = colors.get(obj.step, "#475569")
        return format_html(
            '<span style="color:{}; font-weight:700; text-transform:uppercase; font-size:11px;">{}</span>',
            color, obj.get_step_display(),
        )

    @admin.display(description="Latency")
    def duration_display(self, obj):
        return f"{obj.duration_ms} ms" if obj.duration_ms else "—"

    @admin.display(description="Telemetry Metadata")
    def metadata_display(self, obj):
        if not obj.metadata:
            return "—"
        return format_html(
            '<pre style="background:#0f172a; color:#94a3b8; padding:8px 12px; border-radius:6px; font-size:11px;">{}</pre>',
            json.dumps(obj.metadata, indent=2),
        )


@admin.register(SystemConfiguration)
class SystemConfigurationAdmin(admin.ModelAdmin):
    list_display = [
        "gemini_model",
        "auto_file_threshold_percent",
        "sharepoint_root_folder",
        "folder_template",
        "sharepoint_conflict_behavior",
        "sync_sharepoint_columns",
        "delete_local_on_file",
        "updated_at",
    ]
    readonly_fields = ["updated_at", "updated_by"]
    fieldsets = (
        ("AI Classification Settings", {
            "fields": ("gemini_model", "auto_file_threshold", "max_retry_attempts"),
        }),
        ("SharePoint Filing & ECM Metadata", {
            "fields": (
                "sharepoint_root_folder",
                "folder_template",
                "sharepoint_conflict_behavior",
                "sync_sharepoint_columns",
            ),
        }),
        ("Intake & Storage Hygiene", {
            "fields": ("max_file_size_mb", "delete_local_on_file"),
        }),
        ("Audit", {
            "fields": ("updated_at", "updated_by"),
        }),
    )

    @admin.display(description="Auto-File Threshold")
    def auto_file_threshold_percent(self, obj):
        return f"{int(obj.auto_file_threshold * 100)}% Confidence"

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)

    def has_add_permission(self, request):
        return not SystemConfiguration.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False