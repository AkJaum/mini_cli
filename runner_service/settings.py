import os
from pathlib import Path
from urllib.parse import urlparse

from runner_service.project_config import load_project_catalog

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_CATALOG = load_project_catalog(BASE_DIR / "config.json")


def _normalize_allowed_host(raw_value: str) -> str:
    value = (raw_value or "").strip()
    if not value:
        return ""

    if value.startswith("."):
        return value

    if "://" in value:
        parsed = urlparse(value)
        return (parsed.hostname or "").strip()

    value = value.split("/", 1)[0].strip()
    if value.count(":") == 1:
        value = value.split(":", 1)[0].strip()

    return value


SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY",
    "change-me-runner-service-secret-key",
)
DEBUG = False
ALLOWED_HOSTS = [
    host
    for host in (
        _normalize_allowed_host(item)
        for item in os.getenv(
            "DJANGO_ALLOWED_HOSTS",
            "localhost,127.0.0.1,project-runner",
        ).split(",")
    )
    if host
]
APPEND_SLASH = False

ROOT_URLCONF = "runner_service.urls"
WSGI_APPLICATION = "runner_service.wsgi.application"

INSTALLED_APPS = []
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]
TEMPLATES = []

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
DATA_UPLOAD_MAX_MEMORY_SIZE = int(
    os.getenv("DATA_UPLOAD_MAX_MEMORY_SIZE", str(256 * 1024))
)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

PORT = int(os.getenv("PORT", "8080"))
WORKDIR_BASE = Path(os.getenv("WORKDIR_BASE", "/tmp/runner-workspaces"))
MAX_FILE_SIZE_BYTES = int(
    os.getenv("MAX_FILE_SIZE_BYTES", str(8 * 1024 * 1024))
)
RUNNER_SHARED_TOKEN = os.getenv("RUNNER_SHARED_TOKEN", "").strip()
MAX_REQUESTS_PER_ID_PER_MINUTE = int(
    os.getenv("MAX_REQUESTS_PER_ID_PER_MINUTE", "120")
)
RATE_WINDOW_SECONDS = int(os.getenv("RATE_WINDOW_SECONDS", "60"))
MAX_CONCURRENT_REQUESTS_PER_ID = int(
    os.getenv("MAX_CONCURRENT_REQUESTS_PER_ID", "6")
)
MAX_GLOBAL_CONCURRENT_REQUESTS = int(
    os.getenv("MAX_GLOBAL_CONCURRENT_REQUESTS", "64")
)
PENDING_REQUEST_TTL_SECONDS = int(
    os.getenv("PENDING_REQUEST_TTL_SECONDS", "30")
)
MAX_OUTPUT_BYTES = int(os.getenv("MAX_OUTPUT_BYTES", str(256 * 1024)))
BUILD_TIMEOUT_MS = int(os.getenv("BUILD_TIMEOUT_MS", "30000"))
CLONE_TIMEOUT_MS = int(os.getenv("CLONE_TIMEOUT_MS", "30000"))
RUN_TIMEOUT_MS = int(os.getenv("RUN_TIMEOUT_MS", "10000"))
MAX_BUILD_ARGS = int(os.getenv("MAX_BUILD_ARGS", "8"))
MAX_RUN_ARGS = int(os.getenv("MAX_RUN_ARGS", "32"))
MAX_PTY_INPUT_BYTES = int(os.getenv("MAX_PTY_INPUT_BYTES", "4096"))
MAX_PTY_BUFFER_BYTES = int(
    os.getenv("MAX_PTY_BUFFER_BYTES", str(512 * 1024))
)
PTY_MAX_LIFETIME_SECONDS = int(
    os.getenv("PTY_MAX_LIFETIME_SECONDS", "300")
)
