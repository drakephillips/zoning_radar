"""Supabase client, shared across routers via FastAPI dependency injection."""

import os
from functools import lru_cache

import httpx
from supabase import Client, PostgrestAPIError, create_client

# Placeholder values from .env.example / a fresh local Supabase stack. When
# SUPABASE_URL is unset or still one of these, there's no real database to
# write to, so routes fall back to a DB-less dev mode for local testing.
_DUMMY_SUPABASE_URLS = {"", "http://localhost:54321", "https://your-project.supabase.co"}

# Raised when Supabase is *configured* (a real, non-placeholder URL) but
# actually unusable at call time: local `supabase start` isn't running
# (httpx.ConnectError/RequestError), a request times out, or PostgREST
# itself returns an API-level error. is_dev_mode() alone can't catch this
# case since the URL looks legitimate — routes must catch this tuple around
# the actual query and fall back to the same dev-mode-style behavior.
SUPABASE_UNAVAILABLE_ERRORS = (httpx.RequestError, PostgrestAPIError)


def is_dev_mode() -> bool:
    return os.environ.get("SUPABASE_URL", "") in _DUMMY_SUPABASE_URLS


@lru_cache
def get_supabase() -> Client:
    url = os.environ.get("SUPABASE_URL") or "http://localhost:54321"
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "dummy-local-key"
    return create_client(url, key)
