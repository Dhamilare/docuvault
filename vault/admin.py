import json
from django.contrib import admin, messages
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from .models import Document, DocumentCategory, ProcessingLog, SystemConfiguration
from .services import document_processor


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
        "file_cache_status",
        "sharepoint_link",
        "uploaded_by",
        "created_at",
    ]
    list_filter = [
        "status",
        "category",
        "document_year",
        "is_manually_edited",
        "created_at",
    ]
    search_fields = [
        "original_filename",
        "company_name",
        "checksum_sha256",
        "ai_summary",
        "extracted_text_excerpt",
        "sharepoint_item_id",
    ]
    readonly_fields = [
        "original_filename",
        "file_size_formatted",
        "content_type",
        "checksum_sha256",
        "confidence_score",
        "ai_summary",
        "extracted_text_excerpt",
        "extracted_entities_display",
        "ai_raw_response_display",
        "sharepoint_folder_path",
        "sharepoint_item_id",
        "sharepoint_link",
        "sharepoint_uploaded_at",
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
        "action_reprocess_documents",
        "action_discard_documents",
    ]

    fieldsets = (
        ("File & Staging Storage", {
            "fields": (
                "original_filename",
                "temp_file",
                "file_size_formatted",
                "content_type",
                "checksum_sha256",
                "uploaded_by",
            )
        }),
        ("AI Classification & ECM Extraction", {
            "fields": (
                "status",
                "category",
                "company_name",
                "document_date",
                "document_year",
                "confidence_score",
                "ai_summary",
                "extracted_entities_display",
                "extracted_text_excerpt",
                "ai_raw_response_display",
            )
        }),
        ("Operator Verification", {
            "fields": ("is_manually_edited", "reviewed_by", "reviewed_at")
        }),
        ("SharePoint Filing Destination (ECM)", {
            "fields": (
                "sharepoint_folder_path",
                "sharepoint_item_id",
                "sharepoint_link",
                "sharepoint_uploaded_at",
                "error_message_display",
            )
        }),
        ("Audit Timestamps", {
            "fields": ("created_at", "updated_at", "processed_at"),
            "classes": ("collapse",),
        }),
    )

    # --- Visual Column Badges ---

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
            bg,
            fg,
            obj.get_status_display(),
        )

    @admin.display(description="Category")
    def category_badge(self, obj):
        if not obj.category:
            return format_html('<span style="color:#94a3b8; font-style:italic;">Uncategorized</span>')
        return format_html(
            '<span style="background:#ede9fe; color:#5b21b6; padding:2px 8px; border-radius:6px; font-weight:600; font-size:11px;">{}</span>',
            obj.category.name,
        )

    @admin.display(description="Confidence")
    def confidence_pill(self, obj):
        if obj.confidence_score is None:
            return "—"
        pct = int(obj.confidence_score * 100)
        color = "#059669" if pct >= 80 else ("#d97706" if pct >= 60 else "#dc2626")
        return format_html('<strong style="color:{}; font-size:12px;">{}%</strong>', color, pct)

    @admin.display(description="Local Scan")
    def file_cache_status(self, obj):
        if obj.has_temp_file:
            return format_html('<span style="color:#059669; font-weight:600;">&#x2714; Cached</span>')
        return format_html('<span style="color:#94a3b8; font-size:11px;">Purged (Filed)</span>')

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

    @admin.display(description="Extracted Entities")
    def extracted_entities_display(self, obj):
        if not obj.extracted_entities:
            return "None discovered"
        try:
            formatted = json.dumps(obj.extracted_entities, indent=2)
            return format_html(
                '<pre style="background:#0f172a; color:#38bdf8; padding:8px 12px; border-radius:6px; font-size:12px; max-height:220px; overflow:auto;">{}</pre>',
                formatted,
            )
        except Exception:
            return str(obj.extracted_entities)

    @admin.display(description="AI Raw Payload")
    def ai_raw_response_display(self, obj):
        if not obj.ai_raw_response:
            return "—"
        try:
            formatted = json.dumps(obj.ai_raw_response, indent=2)
            return format_html(
                '<pre style="background:#0f172a; color:#94a3b8; padding:8px 12px; border-radius:6px; font-size:11px; max-height:200px; overflow:auto;">{}</pre>',
                formatted,
            )
        except Exception:
            return str(obj.ai_raw_response)

    # --- Batch Actions ---

    @admin.action(description="File selected documents to SharePoint (Sync ECM Columns)")
    def action_file_to_sharepoint(self, request, queryset):
        success_count = 0
        fail_count = 0

        for doc in queryset:
            if not doc.has_temp_file:
                fail_count += 1
                continue

            success, _ = document_processor.file_reviewed_document(doc, actor=request.user)
            if success:
                success_count += 1
            else:
                fail_count += 1

        if success_count:
            self.message_user(request, f"Successfully filed {success_count} document(s) to SharePoint.", level=messages.SUCCESS)
        if fail_count:
            self.message_user(
                request,
                f"{fail_count} document(s) could not be filed (local scan missing or SharePoint error).",
                level=messages.WARNING,
            )

    @admin.action(description="Re-run Gemini AI processing on selected documents")
    def action_reprocess_documents(self, request, queryset):
        count = 0
        for doc in queryset:
            if doc.has_temp_file:
                document_processor.process_document(doc.pk)
                count += 1
        self.message_user(request, f"Triggered Gemini reprocessing for {count} document(s).", level=messages.INFO)

    @admin.action(description="Discard selected documents (Purge local files)")
    def action_discard_documents(self, request, queryset):
        count = 0
        for doc in queryset:
            if doc.status == Document.Status.FILED:
                continue
            doc.purge_temp_file(force=True)
            doc.delete()
            count += 1
        self.message_user(request, f"Permanently discarded {count} document(s) and cleared local staging.", level=messages.SUCCESS)


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
    prepopulated_fields = {"slug": ("name",)}
    actions = ["action_recalculate_counts"]

    @admin.display(description="Badge Color")
    def color_badge(self, obj):
        colors = {
            "indigo": "#6366f1",
            "emerald": "#10b981",
            "sky": "#0ea5e9",
            "amber": "#f59e0b",
            "purple": "#a855f7",
            "rose": "#f43f5e",
            "teal": "#14b8a6",
            "slate": "#64748b",
        }
        hex_code = colors.get(obj.color, "#6366f1")
        return format_html(
            '<span style="background:{}; color:#fff; padding:2px 8px; border-radius:9999px; font-size:11px; font-weight:600;">{}</span>',
            hex_code,
            obj.get_color_display(),
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

    @admin.display(description="Step")
    def step_badge(self, obj):
        colors = {
            ProcessingLog.Step.FILED: "#059669",
            ProcessingLog.Step.FAILED: "#dc2626",
            ProcessingLog.Step.CLASSIFYING: "#0284c7",
            ProcessingLog.Step.CLASSIFIED: "#4338ca",
        }
        color = colors.get(obj.step, "#475569")
        return format_html(
            '<span style="color:{}; font-weight:700; text-transform:uppercase; font-size:11px;">{}</span>',
            color,
            obj.get_step_display(),
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
        "folder_template",
        "delete_local_on_file",
    ]
    fields = [
        "gemini_model",
        "auto_file_threshold",
        "folder_template",
        "delete_local_on_file",
    ]

    @admin.display(description="Auto-File Threshold")
    def auto_file_threshold_percent(self, obj):
        return f"{int(obj.auto_file_threshold * 100)}% Confidence"

    def has_add_permission(self, request):
        # Enforce singleton pattern: only one configuration row
        return not SystemConfiguration.objects.exists()

    def has_delete_permission(self, request, obj=None):
        # Prevent accidental deletion of system settings
        return False