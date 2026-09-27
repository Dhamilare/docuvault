import os
from django import forms
from django.conf import settings
from .models import Document, DocumentCategory

# Standard Tailwind CSS classes for form inputs
TAILWIND_INPUT = (
    "w-full px-3.5 py-2.5 rounded-xl bg-slate-900 border border-slate-700/80 text-slate-100 "
    "placeholder-slate-500 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500/50 "
    "focus:border-indigo-500 transition"
)
TAILWIND_SELECT = (
    "w-full px-3.5 py-2.5 rounded-xl bg-slate-900 border border-slate-700/80 text-slate-100 "
    "text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500/50 focus:border-indigo-500 transition"
)
TAILWIND_CHECKBOX = (
    "w-4 h-4 text-indigo-600 bg-slate-900 border-slate-700 rounded "
    "focus:ring-indigo-500 focus:ring-offset-slate-950 transition"
)


class DocumentUploadForm(forms.ModelForm):
    """
    Validates a single file upload from the AJAX drag-and-drop dropzone.
    Enforces extension whitelist and file size limits from settings.
    """

    class Meta:
        model = Document
        fields = ["temp_file"]
        widgets = {
            "temp_file": forms.ClearableFileInput(
                attrs={
                    "class": "hidden",
                    "id": "file-upload-input",
                    "accept": ".pdf,.png,.jpg,.jpeg,.webp,.tiff,.tif,.txt,.docx,.csv",
                }
            )
        }

    def clean_temp_file(self):
        f = self.cleaned_data["temp_file"]

        # Default allowed extensions if not defined in settings
        allowed_extensions = getattr(
            settings,
            "ALLOWED_UPLOAD_EXTENSIONS",
            [".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tiff", ".tif", ".txt", ".docx", ".csv"],
        )

        ext = os.path.splitext(f.name)[1].lower()
        if ext not in allowed_extensions:
            raise forms.ValidationError(
                f"'{ext or 'unknown'}' is not a supported file type. "
                f"Allowed: {', '.join(allowed_extensions)}."
            )

        max_mb = getattr(settings, "MAX_UPLOAD_SIZE_MB", 25)
        max_bytes = max_mb * 1024 * 1024
        if f.size > max_bytes:
            raise forms.ValidationError(
                f"'{f.name}' is {f.size / (1024 * 1024):.1f} MB — exceeds the "
                f"{max_mb} MB upload limit."
            )

        return f


class DocumentReviewForm(forms.ModelForm):
    """
    Form for human verification in the review queue or inspection modal.
    Allows picking an existing category, discovering a new one, correcting
    dates/company name, and triggering SharePoint upload.
    """

    category = forms.ModelChoiceField(
        queryset=DocumentCategory.objects.none(),
        required=False,
        label="Existing Category",
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
            "document_date",
            "document_year",
            "ai_summary",
        ]
        widgets = {
            "company_name": forms.TextInput(
                attrs={"class": TAILWIND_INPUT, "placeholder": "e.g., Acme Corporation"}
            ),
            "document_date": forms.DateInput(
                attrs={"class": TAILWIND_INPUT, "type": "date"}
            ),
            "document_year": forms.NumberInput(
                attrs={"class": TAILWIND_INPUT, "placeholder": "YYYY", "min": 1900, "max": 2100}
            ),
            "ai_summary": forms.Textarea(
                attrs={
                    "class": TAILWIND_INPUT,
                    "rows": 3,
                    "placeholder": "Brief summary of the document contents...",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = DocumentCategory.objects.filter(is_hidden=False).order_by("name")

    def clean(self):
        cleaned_data = super().clean()
        category = cleaned_data.get("category")
        new_category_name = (cleaned_data.get("new_category_name") or "").strip()

        if not category and not new_category_name:
            raise forms.ValidationError("Please select an existing category or enter a new one.")

        return cleaned_data


class CategoryMergeForm(forms.Form):
    """
    Consolidates near-duplicate open-ended categories discovered by AI
    (e.g., merging 'Tax 2024' into 'Tax Return').
    """

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
        active_cats = DocumentCategory.objects.filter(is_hidden=False).order_by("name")
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
    """Quick edit form for an existing category name, color, and description."""

    class Meta:
        model = DocumentCategory
        fields = ["name", "color", "description", "is_hidden"]
        widgets = {
            "name": forms.TextInput(attrs={"class": TAILWIND_INPUT}),
            "color": forms.Select(attrs={"class": TAILWIND_SELECT}),
            "description": forms.Textarea(attrs={"class": TAILWIND_INPUT, "rows": 2}),
            "is_hidden": forms.CheckboxInput(attrs={"class": TAILWIND_CHECKBOX}),
        }