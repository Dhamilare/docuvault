import hashlib
import logging
import os
import posixpath
import re
import string
import uuid
from typing import Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import IntegrityError, models, transaction
from django.utils import timezone
from django.utils.text import slugify

logger = logging.getLogger(__name__)

# SharePoint / OneDrive Online path & naming constraints
SHAREPOINT_FORBIDDEN_CHARS_RE = re.compile(r'[\~\"\#\%\&\*\:\<\>\?\/\\\{\|\}]')
SHAREPOINT_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    ".lock", "desktop.ini", "_vti_",
})
SHAREPOINT_MAX_PATH_LENGTH = 400
ALLOWED_FOLDER_TEMPLATE_KEYS = frozenset({
    "root",
    "company_name",
    "document_year",
    "document_month",
    "category_name",
})


def sanitize_sharepoint_segment(text: Optional[str], fallback: str = "General", max_length: int = 120) -> str:
    """
    Sanitizes a single SharePoint folder or filename stem segment.
    Removes forbidden characters, leading/trailing dots/spaces, consecutive dots,
    and avoids SharePoint/Windows reserved names.
    """
    val = (text or "").strip()
    val = SHAREPOINT_FORBIDDEN_CHARS_RE.sub("_", val)
    val = re.sub(r"\.{2,}", ".", val)  # Collapse consecutive dots
    val = re.sub(r"\s+", " ", val).strip(". ")

    if not val or val.upper() in SHAREPOINT_RESERVED_NAMES or val.lower() in SHAREPOINT_RESERVED_NAMES:
        val = fallback

    if len(val) > max_length:
        val = val[:max_length].rstrip(". ")

    return val or fallback


def validate_folder_template(value: str) -> None:
    """Ensures admin-configured folder templates only use supported placeholders."""
    formatter = string.Formatter()
    try:
        fields = [fname for _, fname, _, _ in formatter.parse(value) if fname is not None]
    except ValueError as exc:
        raise ValidationError(f"Invalid folder template syntax: {exc}") from exc

    unknown = set(fields) - ALLOWED_FOLDER_TEMPLATE_KEYS
    if unknown:
        raise ValidationError(
            f"Unsupported template placeholder(s): {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(sorted(ALLOWED_FOLDER_TEMPLATE_KEYS))}."
        )


class DocumentCategory(models.Model):
    """
    A running registry of document types discovered by Gemini or defined by admins.
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
        unique=True,
        blank=True,
        help_text="Normalized lowercase representation used to prevent near-duplicate categories.",
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
        default=False,
        help_text="Hide from filter dropdowns without deleting history.",
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

    def clean(self):
        super().clean()
        if self.merged_into_id:
            if self.merged_into_id == self.pk:
                raise ValidationError({"merged_into": "A category cannot be merged into itself."})
            # Prevent circular merges (A -> B -> A)
            seen = {self.pk}
            cursor = self.merged_into
            while cursor is not None:
                if cursor.pk in seen:
                    raise ValidationError({"merged_into": "Circular category merge detected."})
                seen.add(cursor.pk)
                cursor = cursor.merged_into

    def save(self, *args, **kwargs):
        self.name = re.sub(r"\s+", " ", (self.name or "").strip())
        self.canonical_name = self.normalize_category_name(self.name)

        if not self.slug:
            base_slug = slugify(self.name) or "category"
            slug = base_slug[:130]
            counter = 1
            while DocumentCategory.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                suffix = f"-{counter}"
                slug = f"{base_slug[:140 - len(suffix)]}{suffix}"
                counter += 1
            self.slug = slug

        super().save(*args, **kwargs)

    @classmethod
    def normalize_category_name(cls, raw_name: str) -> str:
        """Strip punctuation and collapse whitespace for reliable matching."""
        if not raw_name:
            return "uncategorized"
        cleaned = re.sub(r"[_\-]+", " ", raw_name).strip().lower()
        cleaned = re.sub(r"[^\w\s]", "", cleaned)
        return re.sub(r"\s+", " ", cleaned).strip() or "uncategorized"

    @classmethod
    def format_display_name(cls, raw_name: str) -> str:
        """
        Capitalizes words cleanly without destroying uppercase acronyms like NDA, PO, VAT, HR.
        """
        words = re.sub(r"\s+", " ", (raw_name or "Uncategorized").strip()).split(" ")
        formatted = []
        for w in words:
            if w.isupper() and len(w) <= 5:
                formatted.append(w)
            else:
                formatted.append(w.capitalize())
        return " ".join(formatted)[:120]

    def resolve_active_category(self) -> "DocumentCategory":
        """Follows merged_into chain safely to return the primary active category."""
        seen = set()
        current = self
        while current.merged_into_id and current.pk not in seen:
            seen.add(current.pk)
            current = current.merged_into
        return current

    @classmethod
    def get_or_create_discovered(cls, raw_name: str, description: str = "") -> "DocumentCategory":
        """
        Concurrency-safe retrieval or creation of a discovered category.
        Deterministic color assignment across all worker processes.
        """
        display_name = cls.format_display_name(raw_name)
        canonical = cls.normalize_category_name(display_name)

        existing = cls.objects.select_related("merged_into").filter(canonical_name=canonical).first()
        if existing:
            return existing.resolve_active_category()

        colors = [c[0] for c in cls.COLOR_CHOICES]
        digest = hashlib.sha256(canonical.encode("utf-8")).digest()
        assigned_color = colors[int.from_bytes(digest[:4], "big") % len(colors)]

        try:
            with transaction.atomic():
                category = cls.objects.create(
                    name=display_name,
                    canonical_name=canonical,
                    description=description.strip() if description else "",
                    color=assigned_color,
                )
                return category
        except IntegrityError:
            # Another worker created this category concurrently
            existing = cls.objects.select_related("merged_into").filter(canonical_name=canonical).first()
            if existing:
                return existing.resolve_active_category()
            # Fallback match by exact name if canonical collided differently
            existing = cls.objects.select_related("merged_into").filter(name__iexact=display_name).first()
            if existing:
                return existing.resolve_active_category()
            raise

    def update_document_count(self) -> None:
        count = self.documents.count()
        DocumentCategory.objects.filter(pk=self.pk).update(document_count=count)
        self.document_count = count

    @transaction.atomic
    def merge_into_target(self, target_category: "DocumentCategory") -> None:
        target = target_category.resolve_active_category()
        if self.pk == target.pk:
            return

        self.documents.update(category=target)
        # Re-point any aliases that pointed to self directly to target
        DocumentCategory.objects.filter(merged_into=self).update(merged_into=target)

        self.merged_into = target
        self.is_hidden = True
        self.document_count = 0
        self.save(update_fields=["merged_into", "is_hidden", "document_count"])
        target.update_document_count()


class Document(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        NEEDS_REVIEW = "needs_review", "Needs review"
        FILED = "filed", "Filed"
        FAILED = "failed", "Failed"

    class MetadataSyncStatus(models.TextChoices):
        NOT_APPLICABLE = "n_a", "Not synced"
        PENDING = "pending", "Pending"
        SYNCED = "synced", "Synced"
        FAILED = "failed", "Sync failed"

    # Public identifier for safe external URLs/APIs (prevents IDOR enumeration)
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)

    original_filename = models.CharField(max_length=255)
    filed_filename = models.CharField(
        max_length=255,
        blank=True,
        help_text="Sanitized or standardized filename used when uploading to SharePoint.",
    )
    temp_file = models.FileField(
        upload_to="incoming/%Y/%m/%d/",
        null=True,
        blank=True,
        help_text="Transient local/object copy — deleted only after verified SharePoint filing.",
    )
    file_size_bytes = models.PositiveBigIntegerField(default=0)
    content_type = models.CharField(max_length=120, blank=True)
    checksum_sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    page_count = models.PositiveIntegerField(null=True, blank=True)

    # Deduplication tracking
    duplicate_of = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="duplicates",
        help_text="Points to an earlier document with the same SHA-256 checksum, if detected.",
    )

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )

    # Background worker & retry resilience
    task_id = models.CharField(max_length=100, blank=True, db_index=True)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    locked_at = models.DateTimeField(null=True, blank=True)

    # AI-derived metadata
    category = models.ForeignKey(
        DocumentCategory,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="documents",
    )
    company_name = models.CharField(max_length=255, blank=True, db_index=True)
    document_title = models.CharField(max_length=255, blank=True)
    document_date = models.DateField(
        null=True, blank=True, help_text="Detected formal date in the document."
    )
    document_year = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        db_index=True,
        validators=[MinValueValidator(1900), MaxValueValidator(2100)],
    )
    document_language = models.CharField(max_length=16, blank=True)
    ai_summary = models.TextField(blank=True)
    extracted_text_excerpt = models.TextField(blank=True)
    confidence_score = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
    )
    extracted_entities = models.JSONField(
        default=dict,
        blank=True,
        help_text="Dynamic key-value pairs (invoice_no, total, currency, PO, parties, etc.).",
    )
    ai_raw_response = models.JSONField(null=True, blank=True)

    # AI telemetry & cost observability
    ai_model_used = models.CharField(max_length=80, blank=True)
    ai_input_tokens = models.PositiveIntegerField(null=True, blank=True)
    ai_output_tokens = models.PositiveIntegerField(null=True, blank=True)

    # Operator review tracking
    is_manually_edited = models.BooleanField(default=False)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_documents",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    # SharePoint filing & column metadata telemetry
    sharepoint_site_id = models.CharField(max_length=255, blank=True)
    sharepoint_drive_id = models.CharField(max_length=255, blank=True)
    sharepoint_folder_path = models.CharField(max_length=500, blank=True)
    sharepoint_item_id = models.CharField(max_length=255, blank=True, db_index=True)
    sharepoint_list_item_id = models.CharField(max_length=255, blank=True)
    sharepoint_etag = models.CharField(max_length=255, blank=True)
    sharepoint_web_url = models.URLField(max_length=1000, blank=True)
    sharepoint_uploaded_at = models.DateTimeField(null=True, blank=True)
    sharepoint_metadata_status = models.CharField(
        max_length=15,
        choices=MetadataSyncStatus.choices,
        default=MetadataSyncStatus.NOT_APPLICABLE,
    )
    sharepoint_metadata_payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Snapshot of column metadata fields pushed to the SharePoint ListItem.",
    )

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
            models.Index(fields=["status", "-created_at"], name="doc_status_created_idx"),
            models.Index(fields=["company_name", "document_year"], name="doc_company_year_idx"),
            models.Index(fields=["category", "status"], name="doc_category_status_idx"),
            models.Index(fields=["checksum_sha256", "status"], name="doc_checksum_status_idx"),
        ]

    def __str__(self):
        return f"{self.original_filename} ({self.get_status_display()})"

    @property
    def has_temp_file(self) -> bool:
        """Verifies transient file presence safely across local or remote storage backends."""
        if not self.temp_file or not self.temp_file.name:
            return False
        try:
            return self.temp_file.storage.exists(self.temp_file.name)
        except Exception as exc:
            logger.warning("Storage check failed for Document %s: %s", self.pk, exc)
            return False

    @property
    def needs_attention(self) -> bool:
        return self.status in (self.Status.NEEDS_REVIEW, self.Status.FAILED)

    @property
    def file_extension(self) -> str:
        return os.path.splitext(self.original_filename)[1].lower()

    @property
    def formatted_size(self) -> str:
        b = self.file_size_bytes
        if b < 1024:
            return f"{b} B"
        if b < 1024 * 1024:
            return f"{b / 1024:.1f} KB"
        if b < 1024 * 1024 * 1024:
            return f"{b / (1024 * 1024):.2f} MB"
        return f"{b / (1024 * 1024 * 1024):.2f} GB"

    @property
    def confidence_percent(self) -> Optional[int]:
        if self.confidence_score is not None:
            return int(round(self.confidence_score * 100))
        return None

    def mark(self, status: str, **fields) -> None:
        """
        Atomically updates status and specified fields without clobbering unrelated columns.
        """
        self.status = status
        update_fields = ["status", "updated_at"]

        if status in (self.Status.FILED, self.Status.NEEDS_REVIEW, self.Status.FAILED) and "processed_at" not in fields:
            fields["processed_at"] = timezone.now()

        for key, value in fields.items():
            setattr(self, key, value)
            update_fields.append(key)

        self.save(update_fields=list(dict.fromkeys(update_fields)))

    def log_step(self, step, message="", actor=None, duration_ms=None, metadata=None):
        return self.logs.create(
            step=step,
            message=message,
            actor=actor,
            duration_ms=duration_ms,
            metadata=metadata or {},
        )

    def purge_temp_file(self, force: bool = False) -> bool:
        """
        Safely removes transient file. Only executes if the document
        has been confirmed filed to SharePoint, or if force=True.
        Returns True if cleared, False if skipped or deletion failed.
        """
        if self.status != self.Status.FILED and not force:
            return False

        if not self.temp_file:
            return True

        storage = self.temp_file.storage
        path = self.temp_file.name
        try:
            if path and storage.exists(path):
                storage.delete(path)
        except Exception as exc:
            logger.error(
                "Failed to delete transient file '%s' for Document %s: %s",
                path,
                self.pk,
                exc,
                exc_info=True,
            )
            return False

        self.temp_file = None
        self.save(update_fields=["temp_file", "updated_at"])
        return True

    def get_sanitized_filename(self) -> str:
        """
        Returns a SharePoint-safe filename, preserving extension and preferring
        `filed_filename` if already generated.
        """
        raw = self.filed_filename or self.original_filename or f"document_{self.pk}.pdf"
        stem, ext = os.path.splitext(raw)
        clean_stem = sanitize_sharepoint_segment(stem, fallback=f"Document_{self.pk}", max_length=160)
        clean_ext = re.sub(r"[^a-zA-Z0-9\.]", "", ext)[:15]
        return f"{clean_stem}{clean_ext}"

    def clean_sharepoint_path(self, base_root: Optional[str] = None, template: Optional[str] = None) -> str:
        """
        Generates a SharePoint-compliant folder path using SystemConfiguration.folder_template
        while sanitizing every segment and enforcing SharePoint path length limits.
        """
        config = SystemConfiguration.get_settings() if (base_root is None or template is None) else None
        root_raw = base_root if base_root is not None else config.sharepoint_root_folder
        tpl = template if template is not None else config.folder_template

        root = sanitize_sharepoint_segment(root_raw, fallback="Scanned Documents", max_length=80)
        company = sanitize_sharepoint_segment(self.company_name, fallback="Unsorted Company", max_length=100)

        if self.document_year:
            year = str(self.document_year)
        elif self.document_date:
            year = str(self.document_date.year)
        else:
            year = "Undated"

        month = f"{self.document_date.month:02d}" if self.document_date else "00"
        cat_obj = self.category.resolve_active_category() if self.category else None
        cat_name = sanitize_sharepoint_segment(
            cat_obj.name if cat_obj else "Uncategorized",
            fallback="Uncategorized",
            max_length=80,
        )

        try:
            rendered = tpl.format(
                root=root,
                company_name=company,
                document_year=year,
                document_month=month,
                category_name=cat_name,
            )
        except Exception as exc:
            logger.warning("Folder template error (%s); falling back to default path.", exc)
            rendered = f"{root}/{company}/{year}/{cat_name}"

        # Normalize segments individually so injected slashes or dots cannot break SharePoint
        parts = [
            sanitize_sharepoint_segment(seg, fallback="General", max_length=100)
            for seg in rendered.strip("/").split("/")
            if seg.strip()
        ]
        if root_raw and (not parts or parts[0].lower() != root.lower()):
            if "{root}" not in tpl:
                parts.insert(0, root)

        folder_path = posixpath.join(*parts) if parts else root

        # Enforce SharePoint 400-char ceiling (reserving room for '/' + filename)
        filename_len = len(self.get_sanitized_filename()) + 1
        max_folder_len = max(50, SHAREPOINT_MAX_PATH_LENGTH - filename_len)
        if len(folder_path) > max_folder_len:
            folder_path = folder_path[:max_folder_len].rstrip("/. ")

        return folder_path


class ProcessingLog(models.Model):
    class Step(models.TextChoices):
        UPLOADED = "uploaded", "Uploaded"
        VALIDATED = "validated", "Validated"
        DUPLICATE_DETECTED = "duplicate_detected", "Duplicate detected"
        CLASSIFYING = "classifying", "Classifying"
        CLASSIFIED = "classified", "Classified"
        FILING = "filing", "Filing to SharePoint"
        FILED = "filed", "Filed"
        METADATA_SYNCED = "metadata_synced", "SharePoint metadata synced"
        METADATA_FAILED = "metadata_failed", "SharePoint metadata sync failed"
        REVIEWED = "reviewed", "Manually reviewed"
        RETRIED = "retried", "Retried"
        PURGED = "purged", "Temp file purged"
        FAILED = "failed", "Failed"

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="logs")
    step = models.CharField(max_length=25, choices=Step.choices, db_index=True)
    message = models.TextField(blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["document", "created_at"], name="log_doc_created_idx"),
        ]

    def __str__(self):
        return f"{self.document_id} · {self.get_step_display()}"


class SystemConfiguration(models.Model):
    class ConflictBehavior(models.TextChoices):
        RENAME = "rename", "Auto-rename if file exists"
        REPLACE = "replace", "Overwrite existing file"
        FAIL = "fail", "Fail & send to Needs Review"

    auto_file_threshold = models.FloatField(
        default=0.80,
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
        help_text="Minimum AI confidence score (0.00 - 1.00) required to auto-file to SharePoint.",
    )
    sharepoint_root_folder = models.CharField(
        max_length=120,
        default="Scanned Documents",
        help_text="Top-level SharePoint document library folder.",
    )
    folder_template = models.CharField(
        max_length=255,
        default="{company_name}/{document_year}/{category_name}",
        validators=[validate_folder_template],
        help_text="Supported variables: {root}, {company_name}, {document_year}, {document_month}, {category_name}",
    )
    sharepoint_conflict_behavior = models.CharField(
        max_length=10,
        choices=ConflictBehavior.choices,
        default=ConflictBehavior.RENAME,
        help_text="How SharePoint handles filename collisions during upload.",
    )
    sync_sharepoint_columns = models.BooleanField(
        default=True,
        help_text="Push extracted metadata (Company, Category, Date, Summary) to SharePoint ListItem columns.",
    )
    gemini_model = models.CharField(max_length=60, default="gemini-3.8-flash")
    max_file_size_mb = models.PositiveIntegerField(
        default=50,
        validators=[MinValueValidator(1), MaxValueValidator(500)],
        help_text="Maximum allowed upload size per document in MB.",
    )
    max_retry_attempts = models.PositiveSmallIntegerField(
        default=3,
        validators=[MinValueValidator(1), MaxValueValidator(10)],
    )
    delete_local_on_file = models.BooleanField(
        default=True,
        help_text="Delete temporary local file immediately after verified upload to SharePoint.",
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )

    class Meta:
        verbose_name = "System Configuration"
        verbose_name_plural = "System Configuration"

    def save(self, *args, **kwargs):
        self.pk = 1  # Enforce true singleton
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("SystemConfiguration singleton cannot be deleted.")

    @classmethod
    def get_settings(cls) -> "SystemConfiguration":
        config, _ = cls.objects.get_or_create(pk=1)
        return config

    def __str__(self):
        return f"DocuVault Settings ({self.gemini_model}, auto-file: {self.auto_file_threshold:.2f})"