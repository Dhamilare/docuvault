from django.contrib import admin
from django.utils.html import format_html
from .models import Document, DocumentCategory, ProcessingLog, SystemConfiguration
from .services import document_processor

class ProcessingLogInline(admin.TabularInline):
    model = ProcessingLog
    extra = 0
    can_delete = False
    readonly_fields = ["step", "message", "duration_ms", "actor", "created_at"]
    ordering = ["-created_at"]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = [
        "original_filename",
        "status_badge",
        "category_badge",
        "company_name",
        "document_year",
        "confidence_pill",
        "sharepoint_link",
        "uploaded_by",
        "created_at",
    ]
    list_filter = ["status", "category", "document_year", "is_manually_edited", "created_at"]
    search_fields = [
        "original_filename",
        "company_name",
        "checksum_sha256",
        "ai_summary",
        "extracted_text_excerpt",
    ]
    readonly_fields = [
        "original_filename",
        "file_size_bytes",
        "content_type",
        "checksum_sha256",
        "confidence_score",
        "ai_summary",
        "extracted_text_excerpt",
        "extracted_entities",
        "ai_raw_response",
        "sharepoint_folder_path",
        "sharepoint_item_id",
        "sharepoint_link",
        "sharepoint_uploaded_at",
        "is_manually_edited",
        "reviewed_by",
        "reviewed_at",
        "created_at",
        "updated_at",
        "processed_at",
    ]
    inlines = [ProcessingLogInline]
    actions = ["action_reprocess_documents", "action_file_to_sharepoint"]

    fieldsets = (
        ("File Information", {
            "fields": ("original_filename", "temp_file", "file_size_bytes", "content_type", "checksum_sha256", "uploaded_by")
        }),
        ("AI Classification & Discovery", {
            "fields": ("status", "category", "company_name", "document_date", "document_year", "confidence_score", "ai_summary", "extracted_entities", "extracted_text_excerpt", "ai_raw_response")
        }),
        ("Operator Verification", {
            "fields": ("is_manually_edited", "reviewed_by", "reviewed_at")
        }),
        ("SharePoint Filing Destination", {
            "fields": ("sharepoint_folder_path", "sharepoint_item_id", "sharepoint_link", "sharepoint_uploaded_at", "error_message")
        }),
        ("System Timestamps", {
            "fields": ("created_at", "updated_at", "processed_at"),
            "classes": ("collapse",)
        }),
    )

    # --- Visual Badge Methods ---

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

    @admin.display(description="SharePoint")
    def sharepoint_link(self, obj):
        if obj.sharepoint_web_url:
            return format_html(
                '<a href="{}" target="_blank" rel="noopener noreferrer" style="color:#2563eb; font-weight:600; text-decoration:underline;">Open in SharePoint &rarr;</a>',
                obj.sharepoint_web_url,
            )
        return "—"

    # --- Admin Actions ---

    @admin.action(description="Re-run Gemini AI processing on selected documents")
    def action_reprocess_documents(self, request, queryset):
        count = 0
        for doc in queryset:
            document_processor.process_document(doc.pk)
            count += 1
        self.message_user(request, f"Triggered reprocessing for {count} document(s).")

    @admin.action(description="File selected verified documents to SharePoint")
    def action_file_to_sharepoint(self, request, queryset):
        count = 0
        for doc in queryset:
            if doc.temp_file:
                document_processor.file_reviewed_document(doc, actor=request.user)
                count += 1
        self.message_user(request, f"Initiated SharePoint filing for {count} document(s).")


@admin.register(DocumentCategory)
class DocumentCategoryAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "color_badge",
        "document_count",
        "first_seen_at",
        "is_hidden",
        "merged_into",
    ]
    search_fields = ["name", "canonical_name", "description"]
    list_filter = ["is_hidden", "color"]
    prepopulated_fields = {"slug": ("name",)}

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


@admin.register(ProcessingLog)
class ProcessingLogAdmin(admin.ModelAdmin):
    list_display = ["document", "step", "duration_display", "actor", "created_at"]
    list_filter = ["step", "created_at"]
    search_fields = ["document__original_filename", "message"]
    readonly_fields = ["document", "step", "message", "duration_ms", "metadata", "actor", "created_at"]

    @admin.display(description="Latency")
    def duration_display(self, obj):
        if obj.duration_ms:
            return f"{obj.duration_ms} ms"
        return "—"


@admin.register(SystemConfiguration)
class SystemConfigurationAdmin(admin.ModelAdmin):
    list_display = ["gemini_model", "auto_file_threshold", "folder_template", "delete_local_on_file"]

    def has_add_permission(self, request):
        # Enforce singleton pattern: only one configuration row
        return not SystemConfiguration.objects.exists()