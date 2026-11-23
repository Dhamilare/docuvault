"""
Django settings for the DocuVault AI project.
Production-hardened with strict boolean parsing, proxy/HTTPS headers,
PostgreSQL/Redis support (with safe local dev fallbacks), and structured logging.
"""
import importlib.util
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# --- Core ---
DEBUG = _env_bool("DJANGO_DEBUG", default=False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "").strip()
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "dev-insecure-change-me-before-production"
    else:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DEBUG is False.")

ALLOWED_HOSTS = _env_list("ALLOWED_HOSTS", "localhost,127.0.0.1" if DEBUG else "")
if not DEBUG and (not ALLOWED_HOSTS or "*" in ALLOWED_HOSTS):
    raise ImproperlyConfigured("ALLOWED_HOSTS must list explicit domain(s) in production (no wildcard '*').")

CSRF_TRUSTED_ORIGINS = _env_list("CSRF_TRUSTED_ORIGINS", "")

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

_HAS_WHITENOISE = importlib.util.find_spec("whitenoise") is not None

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    *(["whitenoise.middleware.WhiteNoiseMiddleware"] if _HAS_WHITENOISE else []),
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

# --- Database (PostgreSQL in production; SQLite with busy timeout in dev) ---
if os.environ.get("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["POSTGRES_DB"],
            "USER": os.environ.get("POSTGRES_USER", "docuvault"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": int(os.environ.get("DB_CONN_MAX_AGE", "60")),
            "CONN_HEALTH_CHECKS": True,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            "OPTIONS": {
                "timeout": 20,
            },
        }
    }

# --- Cache (Redis when REDIS_URL is set, otherwise LocMemCache) ---
REDIS_URL = os.environ.get("REDIS_URL", "").strip()
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "docuvault-default",
        }
    }

# --- Password Validation (Protects local Django Admin superusers) ---
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- Internationalization ---
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Lagos"
USE_I18N = True
USE_TZ = True

# --- Static & Transient Staging Media ---
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "vault" / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

if _HAS_WHITENOISE:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
    }

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media_tmp"
os.makedirs(MEDIA_ROOT, exist_ok=True)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"

# --- Microsoft Entra ID (User interactive sign-in via MSAL) ---
MS_CLIENT_ID = os.environ.get("MS_CLIENT_ID", "").strip()
MS_CLIENT_SECRET = os.environ.get("MS_CLIENT_SECRET", "").strip()
MS_TENANT_ID = os.environ.get("MS_TENANT_ID", "common").strip() or "common"
MS_AUTHORITY = f"https://login.microsoftonline.com/{MS_TENANT_ID}"
MS_REDIRECT_URI = os.environ.get("MS_REDIRECT_URI", "http://localhost:8000/auth/callback/").strip()
MS_SCOPES = ["User.Read"]

# --- Microsoft Graph (App-only client credentials for SharePoint filing) ---
GRAPH_SITE_ID = os.environ.get("GRAPH_SITE_ID", "").strip()
GRAPH_DRIVE_ID = os.environ.get("GRAPH_DRIVE_ID", "").strip()
GRAPH_LIBRARY_ROOT_FOLDER = os.environ.get("GRAPH_LIBRARY_ROOT_FOLDER", "Scanned Documents").strip()
GRAPH_SCOPES = ["https://graph.microsoft.com/.default"]

# --- Gemini Configuration ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip()
GEMINI_MAX_RETRIES = int(os.environ.get("GEMINI_MAX_RETRIES", "3"))

# --- App & Worker Pipeline Rules ---
VAULT_USE_CELERY = _env_bool("VAULT_USE_CELERY", default=False)
VAULT_WORKER_THREADS = int(os.environ.get("VAULT_WORKER_THREADS", "4"))
VAULT_REQUIRE_STAFF_FOR_ADMIN_ACTIONS = _env_bool("VAULT_REQUIRE_STAFF_FOR_ADMIN_ACTIONS", default=False)

MAX_UPLOAD_SIZE_MB = int(os.environ.get("MAX_UPLOAD_SIZE_MB", "25"))
CLASSIFICATION_CONFIDENCE_THRESHOLD = float(os.environ.get("CLASSIFICATION_CONFIDENCE_THRESHOLD", "0.80"))
ALLOWED_UPLOAD_EXTENSIONS = [
    ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".txt", ".docx", ".csv",
]

# Upload request memory bounds
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024   # Stream uploads > 5 MB to temp disk instead of RAM
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024

# --- Security & Reverse Proxy Hardening ---
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False  # Read by AJAX upload JS
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = _env_bool("SECURE_SSL_REDIRECT", default=True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

# --- Logging ---
LOG_TO_FILE = _env_bool("DJANGO_LOG_TO_FILE", default=DEBUG)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "%(asctime)s [%(levelname)s] %(process)d %(name)s: %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
        **(
            {
                "file": {
                    "class": "logging.handlers.WatchedFileHandler",
                    "filename": BASE_DIR / "docuvault.log",
                    "formatter": "verbose",
                }
            }
            if LOG_TO_FILE
            else {}
        ),
    },
    "root": {
        "handlers": ["console"],
        "level": os.environ.get("LOG_LEVEL", "INFO"),
    },
    "loggers": {
        "vault": {
            "handlers": ["console", "file"] if LOG_TO_FILE else ["console"],
            "level": os.environ.get("LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        "django.security": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}