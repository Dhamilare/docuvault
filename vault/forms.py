import os
import zipfile
from django import forms
from django.utils import timezone
from .models import Document, DocumentCategory, SystemConfiguration

TAILWIND_INPUT = (
    "w-full px-3.5 py-2.5 rounded-xl bg-white border border-slate-300 text-slate-900 placeholder-slate-400 "
    "text-xs focus:outline-none focus:ring-2 focus:ring-indigo-500/30 focus:border-indigo-500 transition shadow-sm"
)
TAILWIND_SELECT = (
    "w-full px-3.5 py-2.5 pr-8 rounded-xl bg-white border border-slate-300 text-slate-900 "
    "text-xs focus:outline-none focus:ring-2 focus:ring-indigo-500/30 focus:border-indigo-500 transition cursor-pointer shadow-sm"
)
TAILWIND_CHECKBOX = (
    "w-4 h-4 text-indigo-600 bg-white border-slate-300 rounded "
    "focus:ring-indigo-500 focus:ring-offset-white transition cursor-pointer"
)

ALLOWED_EXTENSIONS = (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tiff", ".tif", ".txt", ".docx", ".csv")

MAGIC_SIGNATURES = {
    ".pdf": (b"%PDF-",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".webp": (b"RIFF",),
    ".tiff": (b"II*\x00", b"MM\x00*"),
    ".tif": (b"II*\x00", b"MM\x00*"),
    ".docx": (b"PK\x03\x04",),
}


def get_year_choices():
    """Generates dropdown year choices from 5 years ahead back to 1950."""
    current_year = timezone.now().year
    years = [(y, str(y)) for y in range(current_year + 5, 1949, -1)]
    return [("", "— Select Document Year —")] + years


class DocumentUploadForm(forms.ModelForm):
    """
    Validates a single uploaded file (size, extension, non-empty, and magic bytes).
    """

    class Meta:
        model = Document
        fields = ["temp_file"]
        widgets = {
            "temp_file": forms.ClearableFileInput(
                attrs={
                    "class": "hidden",
                    "id": "file-upload-input",
                    "accept": ",".join(ALLOWED_EXTENSIONS),
                }
            )
        }

    def clean_temp_file(self):
        f = self.cleaned_data["temp_file"]

        # Strip directory traversal components
        f.name = os.path.basename(f.name or "").replace("\x00", "")
        ext = os.path.splitext(f.name)[1].lower()

        if ext not in ALLOWED_EXTENSIONS:
            raise forms.ValidationError(
                f"'{ext or 'unknown'}' is not a supported file type. Allowed: {', '.join(ALLOWED_EXTENSIONS)}."
            )

        if f.size == 0:
            raise forms.ValidationError(f"'{f.name}' is empty (0 bytes).")

        config = SystemConfiguration.get_settings()
        max_bytes = config.max_file_size_mb * 1024 * 1024
        if f.size > max_bytes:
            raise forms.ValidationError(
                f"'{f.name}' is {f.size / (1024 * 1024):.1f} MB — exceeds the {config.max_file_size_mb} MB upload limit."
            )

        # Magic-byte header verification
        pos = f.tell() if hasattr(f, "tell") else 0
        header = f.read(16)
        f.seek(pos)

        expected_sigs = MAGIC_SIGNATURES.get(ext)
        if expected_sigs:
            if not any(header.startswith(sig) for sig in expected_sigs):
                raise forms.ValidationError(
                    f"'{f.name}' file signature does not match a valid {ext} document."
                )
            if ext == ".webp" and header[8:12] != b"WEBP":
                raise forms.ValidationError(f"'{f.name}' is not a valid WEBP image.")
            if ext == ".docx":
                try:
                    with zipfile.ZipFile(f) as zf:
                        if "word/document.xml" not in zf.namelist():
                            raise forms.ValidationError(f"'{f.name}' is not a valid Word (.docx) document.")
                except zipfile.BadZipFile:
                    raise forms.ValidationError(f"'{f.name}' is corrupted or not a valid .docx archive.")
                finally:
                    f.seek(pos)
        elif ext in (".txt", ".csv"):
            sample = f.read(4096)
            f.seek(pos)
            if b"\x00" in sample:
                raise forms.ValidationError(f"'{f.name}' appears to be a binary file rather than plain text.")

        return f


class DocumentReviewForm(forms.ModelForm):
    """
    Operator verification form for reviewing metadata and filing to SharePoint.
    """

    category = forms.ModelChoiceField(
        queryset=DocumentCategory.objects.none(),
        required=False,
        label="Existing Category",
        empty_label="— Select Detected Category —",
        widget=forms.Select(attrs={"class": TAILWIND_SELECT}),
    )
    new_category_name = forms.CharField(
        max_length=120,
        required=False,
        label="Or Discover New Category",
        help_text="Leave blank to use the selected category above.",
        widget=forms.TextInput(
            attrs={"class": TAILWIND_INPUT, "placeholder": "e.g., Equipment Lease Agreement"}
        ),
    )
    document_date = forms.DateField(
        required=False,
        label="Formal Document Date",
        widget=forms.DateInput(
            format="%Y-%m-%d",
            attrs={"class": TAILWIND_INPUT, "type": "date", "id": "id_document_date"},
        ),
    )
    document_year = forms.TypedChoiceField(
        choices=get_year_choices,
        coerce=int,
        empty_value=None,
        required=False,
        label="Document Year",
        widget=forms.Select(attrs={"class": TAILWIND_SELECT, "id": "id_document_year"}),
    )
    file_to_sharepoint_now = forms.BooleanField(
        required=False,
        initial=True,
        label="Immediately file to SharePoint upon save",
        widget=forms.CheckboxInput(attrs={"class": TAILWIND_CHECKBOX}),
    )

    class Meta:
        model = Document
        fields = [
            "category",
            "company_name",
            "document_title",
            "filed_filename",
            "document_date",
            "document_year",
            "ai_summary",
        ]
        widgets = {
            "company_name": forms.TextInput(
                attrs={"class": TAILWIND_INPUT, "placeholder": "e.g., Acme Corporation"}
            ),
            "document_title": forms.TextInput(
                attrs={"class": TAILWIND_INPUT, "placeholder": "Optional SharePoint Title column"}
            ),
            "filed_filename": forms.TextInput(
                attrs={"class": TAILWIND_INPUT, "placeholder": "Optional SharePoint filename override"}
            ),
            "ai_summary": forms.Textarea(
                attrs={"class": TAILWIND_INPUT, "rows": 3, "placeholder": "Brief summary of the document contents..."}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = DocumentCategory.objects.filter(
            is_hidden=False, merged_into__isnull=True
        ).order_by("name")

    def clean(self):
        cleaned_data = super().clean()
        category = cleaned_data.get("category")
        new_category_name = (cleaned_data.get("new_category_name") or "").strip()

        if not category and not new_category_name:
            raise forms.ValidationError("Please select an existing category or enter a new one.")

        doc_date = cleaned_data.get("document_date")
        doc_year = cleaned_data.get("document_year")
        if doc_date and not doc_year:
            cleaned_data["document_year"] = doc_date.year

        return cleaned_data


class CategoryMergeForm(forms.Form):
    source_category = forms.ModelChoiceField(
        queryset=DocumentCategory.objects.none(),
        label="Source Category (Duplicate to absorb)",
        widget=forms.Select(attrs={"class": TAILWIND_SELECT}),
    )
    target_category = forms.ModelChoiceField(
        queryset=DocumentCategory.objects.none(),
        label="Master Category (Destination)",
        widget=forms.Select(attrs={"class": TAILWIND_SELECT}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        active_cats = DocumentCategory.objects.filter(
            is_hidden=False, merged_into__isnull=True
        ).order_by("name")
        self.fields["source_category"].queryset = active_cats
        self.fields["target_category"].queryset = active_cats

    def clean(self):
        cleaned_data = super().clean()
        source = cleaned_data.get("source_category")
        target = cleaned_data.get("target_category")
        if source and target and source.pk == target.pk:
            raise forms.ValidationError("The source and destination categories must be different.")
        return cleaned_data


class CategoryEditForm(forms.ModelForm):
    class Meta:
        model = DocumentCategory
        fields = ["name", "color", "description", "is_hidden"]
        widgets = {
            "name": forms.TextInput(attrs={"class": TAILWIND_INPUT}),
            "color": forms.Select(attrs={"class": TAILWIND_SELECT}),
            "description": forms.Textarea(attrs={"class": TAILWIND_INPUT, "rows": 2}),
            "is_hidden": forms.CheckboxInput(attrs={"class": TAILWIND_CHECKBOX}),
        }