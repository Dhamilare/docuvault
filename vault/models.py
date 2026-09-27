import os
import re
from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.text import slugify


class DocumentCategory(models.Model):
    """
    A running registry of document types Gemini has discovered so far.

    Classification is open-ended — the AI is never restricted to a fixed list —
    tracking what it has produced lets an admin see the set of categories in use,
    rename/merge near-duplicates, and display sleek Tailwind badges.
    """

    COLOR_CHOICES = [
        ("indigo", "Indigo"),
        ("emerald", "Emerald"),
        ("sky", "Sky Blue"),
        ("amber", "Amber"),
        ("purple", "Purple"),
        ("rose", "Rose"),
        ("teal", "Teal"),
        ("slate", "Slate"),
    ]

    name = models.CharField(max_length=120, unique=True)
    canonical_name = models.CharField(
        max_length=120,
        blank=True,
        db_index=True,
        help_text="Normalized lowercase representation used to match near-duplicates automatically.",
    )
    slug = models.SlugField(max_length=140, unique=True, blank=True)
    description = models.TextField(
        blank=True,
        help_text="AI-generated or admin summary of what documents belong in this category.",
    )
    color = models.CharField(
        max_length=20,
        choices=COLOR_CHOICES,
        default="indigo",
        help_text="Tailwind badge theme color.",
    )
    first_seen_at = models.DateTimeField(default=timezone.now)
    document_count = models.PositiveIntegerField(default=0)
    is_hidden = models.BooleanField(
        default=False, help_text="Hide from filter dropdowns without deleting history."
    )
    merged_into = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="aliases",
        help_text="If merged into another category, references the primary category.",
    )

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "Document categories"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.canonical_name:
            self.canonical_name = self.normalize_category_name(self.name)
        if not self.slug:
            base_slug = slugify(self.name) or "category"
            slug = base_slug
            counter = 1
            while DocumentCategory.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)

    @classmethod
    def normalize_category_name(cls, raw_name: str) -> str:
        """Strip punctuation and whitespace for reliable matching."""
        if not raw_name:
            return "uncategorized"
        cleaned = re.sub(r"[_\-]+", " ", raw_name).strip().lower()
        return re.sub(r"\s+", " ", cleaned)

    @classmethod
    def get_or_create_discovered(cls, raw_name: str, description: str = "") -> "DocumentCategory":
        """
        Locates an existing category using normalized text matching,
        or creates a newly discovered category.
        """
        name = (raw_name or "Uncategorized").strip()
        canonical = cls.normalize_category_name(name)

        category = cls.objects.filter(canonical_name=canonical).first()
        if category:
            return category.merged_into or category

        # Assign a deterministic Tailwind color based on string hash
        colors = [c[0] for c in cls.COLOR_CHOICES]
        assigned_color = colors[hash(canonical) % len(colors)]

        return cls.objects.create(
            name=name.title(),
            canonical_name=canonical,
            description=description,
            color=assigned_color,
        )

    def update_document_count(self):
        self.document_count = self.documents.count()
        self.save(update_fields=["document_count"])

    def merge_into_target(self, target_category: "DocumentCategory"):
        """Consolidates this category into target_category and marks as alias."""
        if self.pk == target_category.pk:
            return
        self.documents.update(category=target_category)
        self.merged_into = target_category
        self.is_hidden = True
        self.save()
        target_category.update_document_count()


class Document(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        NEEDS_REVIEW = "needs_review", "Needs review"
        FILED = "filed", "Filed"
        FAILED = "failed", "Failed"

    original_filename = models.CharField(max_length=255)
    temp_file = models.FileField(
        upload_to="incoming/%Y/%m/%d/",
        null=True,
        blank=True,
        help_text="Transient local copy — deleted once filed to SharePoint.",
    )
    file_size_bytes = models.PositiveIntegerField(default=0)
    content_type = models.CharField(max_length=100, blank=True)
    checksum_sha256 = models.CharField(max_length=64, blank=True, db_index=True)

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )

    # --- AI-derived fields (open-ended; not constrained to a fixed enum) ---
    category = models.ForeignKey(
        DocumentCategory,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="documents",
    )
    company_name = models.CharField(max_length=255, blank=True, db_index=True)
    document_date = models.DateField(
        null=True, blank=True, help_text="Detected formal date in the document."
    )
    document_year = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    ai_summary = models.TextField(blank=True)
    extracted_text_excerpt = models.TextField(blank=True)
    confidence_score = models.FloatField(null=True, blank=True)
    extracted_entities = models.JSONField(
        default=dict,
        blank=True,
        help_text="Dynamic key-value pairs (invoice_no, total, parties, PO, etc.).",
    )
    ai_raw_response = models.JSONField(null=True, blank=True)

    # --- Operator Review Tracking ---
    is_manually_edited = models.BooleanField(default=False)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_documents",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    # --- SharePoint filing result ---
    sharepoint_folder_path = models.CharField(max_length=500, blank=True)
    sharepoint_item_id = models.CharField(max_length=255, blank=True)
    sharepoint_web_url = models.URLField(max_length=1000, blank=True)
    sharepoint_uploaded_at = models.DateTimeField(null=True, blank=True)

    error_message = models.TextField(blank=True)

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="uploaded_documents",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["company_name"]),
            models.Index(fields=["document_year"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"{self.original_filename} ({self.get_status_display()})"

    @property
    def needs_attention(self):
        return self.status in (self.Status.NEEDS_REVIEW, self.Status.FAILED)

    @property
    def file_extension(self):
        return os.path.splitext(self.original_filename)[1].lower()

    @property
    def formatted_size(self):
        b = self.file_size_bytes
        if b < 1024:
            return f"{b} B"
        elif b < 1024 * 1024:
            return f"{b / 1024:.1f} KB"
        return f"{b / (1024 * 1024):.2f} MB"

    @property
    def confidence_percent(self):
        if self.confidence_score is not None:
            return int(round(self.confidence_score * 100))
        return None

    def mark(self, status, **fields):
        self.status = status
        for key, value in fields.items():
            setattr(self, key, value)
        self.save()

    def log_step(self, step, message="", actor=None, duration_ms=None, metadata=None):
        return self.logs.create(
            step=step,
            message=message,
            actor=actor,
            duration_ms=duration_ms,
            metadata=metadata or {},
        )

    def purge_temp_file(self):
        """Safely removes the transient local file from storage once filed."""
        if self.temp_file:
            storage = self.temp_file.storage
            path = self.temp_file.name
            if storage.exists(path):
                storage.delete(path)
            self.temp_file = None
            self.save(update_fields=["temp_file"])


class ProcessingLog(models.Model):
    """Append-only audit trail of every step a document goes through."""

    class Step(models.TextChoices):
        UPLOADED = "uploaded", "Uploaded"
        VALIDATED = "validated", "Validated"
        CLASSIFYING = "classifying", "Classifying"
        CLASSIFIED = "classified", "Classified"
        FILING = "filing", "Filing to SharePoint"
        FILED = "filed", "Filed"
        REVIEWED = "reviewed", "Manually reviewed"
        RETRIED = "retried", "Retried"
        FAILED = "failed", "Failed"

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="logs")
    step = models.CharField(max_length=25, choices=Step.choices)
    message = models.TextField(blank=True)
    duration_ms = models.PositiveIntegerField(
        null=True, blank=True, help_text="Step latency in milliseconds."
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Telemetry (e.g. token counts, model name, HTTP codes).",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.document_id} · {self.get_step_display()}"


class SystemConfiguration(models.Model):
    """Runtime dynamic settings for AI thresholds and SharePoint destinations."""

    auto_file_threshold = models.FloatField(
        default=0.80,
        help_text="Minimum AI confidence score (0.00 - 1.00) required to auto-file to SharePoint.",
    )
    folder_template = models.CharField(
        max_length=255,
        default="/Documents/{company_name}/{document_year}/{category_name}",
        help_text="Dynamic folder structure in SharePoint.",
    )
    gemini_model = models.CharField(
        max_length=60,
        default="gemini-2.5-flash",
        help_text="Active Gemini model ('gemini-2.5-flash' or 'gemini-2.5-pro').",
    )
    delete_local_on_file = models.BooleanField(
        default=True,
        help_text="Delete temporary local file immediately after verified upload to SharePoint.",
    )

    class Meta:
        verbose_name = "System Configuration"
        verbose_name_plural = "System Configuration"

    @classmethod
    def get_settings(cls) -> "SystemConfiguration":
        config, _ = cls.objects.get_or_create(id=1)
        return config

    def __str__(self):
        return f"DocuFlow Settings ({self.gemini_model}, auto-file: {self.auto_file_threshold})"