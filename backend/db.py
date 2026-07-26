"""Supabase client, shared across routers via FastAPI dependency injection."""

import os
from functools import lru_cache

from supabase import Client, create_client

# Placeholder values from .env.example / a fresh local Supabase stack. When
# SUPABASE_URL is unset or still one of these, there's no real database to
# write to, so routes fall back to a DB-less dev mode for local testing.
_DUMMY_SUPABASE_URLS = {"", "http://localhost:54321", "https://your-project.supabase.co"}


def is_dev_mode() -> bool:
    return os.environ.get("SUPABASE_URL", "") in _DUMMY_SUPABASE_URLS


@lru_cache
def get_supabase() -> Client:
    url = os.environ.get("SUPABASE_URL") or "http://localhost:54321"
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "dummy-local-key"
    return create_client(url, key)
