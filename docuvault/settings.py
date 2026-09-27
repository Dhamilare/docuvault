"""
Django settings for the DocuVault AI project.

DocuVault AI takes manually-uploaded scanned documents, classifies them
with Gemini, and files them into a SharePoint document library — grouped
into folders by company name and/or year, with the discovered document
type and other extracted fields stored as searchable metadata.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# --- Core ---
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")
DEBUG = os.environ.get("DJANGO_DEBUG")
ALLOWED_HOSTS = ['*']

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "widget_tweaks",
    "vault",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "vault.middleware.LoginRequiredMiddleware",
]

ROOT_URLCONF = "docuvault.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "vault" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "vault.context_processors.review_queue_count",
            ],
        },
    },
]

WSGI_APPLICATION = "docuvault.wsgi.application"

# --- Database (local dev default: SQLite) ---
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = []  # No local passwords — auth is via Microsoft Entra ID

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Lagos"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "vault" / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
# Scans are only stored here transiently during processing; see
# services/document_processor.py — files are deleted after a successful
# (or terminally failed) SharePoint upload.
MEDIA_ROOT = BASE_DIR / "media_tmp"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"

# --- Microsoft Entra ID (user sign-in via MSAL) ---
MS_CLIENT_ID = os.environ.get("MS_CLIENT_ID", "")
MS_CLIENT_SECRET = os.environ.get("MS_CLIENT_SECRET", "")
MS_TENANT_ID = os.environ.get("MS_TENANT_ID", "")
MS_AUTHORITY = f"https://login.microsoftonline.com/{MS_TENANT_ID}"
MS_REDIRECT_URI = os.environ.get("MS_REDIRECT_URI", "http://localhost:8000/auth/callback/")
MS_SCOPES = ["User.Read"]

# --- Microsoft Graph (app-only, for filing into SharePoint) ---
GRAPH_SITE_ID = os.environ.get("GRAPH_SITE_ID", "")
GRAPH_DRIVE_ID = os.environ.get("GRAPH_DRIVE_ID", "")
GRAPH_LIBRARY_ROOT_FOLDER = os.environ.get("GRAPH_LIBRARY_ROOT_FOLDER", "Scanned Documents")
GRAPH_SCOPES = ["https://graph.microsoft.com/.default"]

# --- Gemini ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL")

# --- App behaviour ---
FOLDER_STRUCTURE = [
    part.strip() for part in os.environ.get("FOLDER_STRUCTURE", "company_name,year").split(",") if part.strip()
]
MAX_UPLOAD_SIZE_MB = int(os.environ.get("MAX_UPLOAD_SIZE_MB", "25"))
CLASSIFICATION_CONFIDENCE_THRESHOLD = float(os.environ.get("CLASSIFICATION_CONFIDENCE_THRESHOLD", "0.7"))
ALLOWED_UPLOAD_EXTENSIONS = [".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"]

# --- Session / security ---
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False  # must be readable by JS to attach to AJAX headers
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
X_FRAME_OPTIONS = "DENY"
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = True

# --- Logging (structured, with a dedicated pipeline logger) ---
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": BASE_DIR / "docuvault.log",
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 3,
            "formatter": "verbose",
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "vault": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
    },
}
