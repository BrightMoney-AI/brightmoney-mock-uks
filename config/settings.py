"""
Django settings for the Vendor Mock & Test Framework.

A self-contained project that hosts:
  * ``mockvendor``  — the reusable mock server app (Section 3-5 of the design doc)
  * ``dummy_aut``   — a tiny stand-in Application Under Test used to demo the
                      CSV-driven test runner end to end (Section 6, 10).

Storage is SQLite by default so the project runs with zero external
infrastructure (design doc §1: "No new infrastructure beyond the existing
Django project and database").
"""
from __future__ import annotations

import os as _os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# Where the SQLite files live. Defaults to the project dir; override with
# MOCKVENDOR_DB_DIR (e.g. on network/FUSE mounts where SQLite locking misbehaves).
DB_DIR = Path(_os.environ.get("MOCKVENDOR_DB_DIR", BASE_DIR))
DB_DIR.mkdir(parents=True, exist_ok=True)

SECRET_KEY = _os.environ.get("SECRET_KEY", "dev-only-not-secret-mock-vendor-framework")

# DEBUG defaults True; the mock admin/seed API is gated on DEBUG (design §8.1).
DEBUG = _os.environ.get("DEBUG", "True").lower() not in ("false", "0", "no")
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.admin",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "mockvendor",
    "dummy_aut",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

WSGI_APPLICATION = "config.wsgi.application"

# The mock server DB and the AUT's own DB are kept separate so the test runner
# can verify the AUT's database independently of the mock.
# Set DB_ENGINE=django.db.backends.postgresql in .env to switch to PostgreSQL.
_DB_ENGINE = _os.environ.get("DB_ENGINE", "django.db.backends.sqlite3")

if _DB_ENGINE == "django.db.backends.sqlite3":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": DB_DIR / "mock.sqlite3",
        },
        "aut": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": DB_DIR / "aut.sqlite3",
        },
    }
else:
    # PostgreSQL (or any other engine) — all values read from .env
    _pg_host = _os.environ.get("DB_HOST", "localhost")
    _pg_port = _os.environ.get("DB_PORT", "5432")
    _pg_user = _os.environ.get("DB_USER", "")
    _pg_pass = _os.environ.get("DB_PASSWORD", "")
    DATABASES = {
        "default": {
            "ENGINE": _DB_ENGINE,
            "NAME": _os.environ.get("DB_NAME", "mockvendor"),
            "USER": _pg_user,
            "PASSWORD": _pg_pass,
            "HOST": _pg_host,
            "PORT": _pg_port,
        },
        "aut": {
            "ENGINE": _DB_ENGINE,
            "NAME": _os.environ.get("AUT_DB_NAME", "mockvendor_aut"),
            "USER": _os.environ.get("AUT_DB_USER", _pg_user),
            "PASSWORD": _os.environ.get("AUT_DB_PASSWORD", _pg_pass),
            "HOST": _os.environ.get("AUT_DB_HOST", _pg_host),
            "PORT": _os.environ.get("AUT_DB_PORT", _pg_port),
        },
    }

DATABASE_ROUTERS = ["config.routers.AutRouter"]

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "UNAUTHENTICATED_USER": None,
}

# --- mockvendor plugin registry (design §3.4) -------------------------------
# Adding a format = add one class + one line here, no core change.
MOCKVENDOR_SERIALIZERS = [
    "mockvendor.serializers_fmt.JsonSerializer",
    "mockvendor.serializers_fmt.XmlSerializer",
]

# Gate the admin/seed API so it is unreachable in production (design §8.1).
MOCKVENDOR_ADMIN_ENABLED = DEBUG

# Where the dummy AUT sends its vendor calls (the mock server's base URL).
MOCK_BASE_URL = _os.environ.get("MOCK_BASE_URL", "http://127.0.0.1:8000")
AUT_VENDOR_TIMEOUT = 10

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
