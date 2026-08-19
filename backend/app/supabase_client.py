from functools import lru_cache

from supabase import Client, create_client

from backend.app.config import settings


@lru_cache
def get_supabase() -> Client:
    return create_client(settings.supabase_url, settings.supabase_key)
