import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / 'data'
CACHE_FILE = DATA_DIR / 'sessions.json'
ORDER_FILE = DATA_DIR / 'latest_order.json'

_lock = threading.Lock()


def _ensure_dir():
    DATA_DIR.mkdir(exist_ok=True)


# ── sessions cache ────────────────────────────────────────────────────────────

def load_cache() -> dict:
    _ensure_dir()
    if not CACHE_FILE.exists():
        return {}
    with open(CACHE_FILE, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def _save_cache(cache: dict):
    _ensure_dir()
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def save_session(session_id: str, data: dict):
    with _lock:
        cache = load_cache()
        cache[session_id] = data
        _save_cache(cache)
    # Sync to Supabase outside the lock (non-blocking, best-effort)
    try:
        from app.database import upsert_session
        upsert_session(data)
    except Exception as e:
        logger.warning(f"DB sync skipped for {session_id[:8]}: {e}")


def get_session(session_id: str) -> dict | None:
    return load_cache().get(session_id)


def is_cached(session_id: str) -> bool:
    return session_id in load_cache()


def get_cached_ids() -> set:
    """
    Return IDs of sessions that should NOT be re-fetched:
    - completed sessions that already have conversation/result data
    """
    cache = load_cache()
    skip = set()
    for sid, data in cache.items():
        status = (data.get('status') or '').lower()
        has_data = data.get('conversation') or data.get('result_json')
        if status == 'completed' and has_data:
            skip.add(sid)
    return skip


def get_cache_snapshot() -> dict:
    """
    Return a lightweight snapshot of the cache: {session_id: {status, msg_count}}
    Used by the fetcher to detect changes in active sessions.
    """
    cache = load_cache()
    return {
        sid: {
            'status': (data.get('status') or '').lower(),
            'msg_count': data.get('msg_count', 0) or 0,
            'has_data': bool(data.get('conversation') or data.get('result_json')),
        }
        for sid, data in cache.items()
    }


def get_all_sessions() -> dict:
    return load_cache()


# ── latest scrape order ───────────────────────────────────────────────────────

def save_latest_order(session_ids: list):
    _ensure_dir()
    with _lock:
        with open(ORDER_FILE, 'w', encoding='utf-8') as f:
            json.dump(session_ids, f)


def get_latest_order() -> list:
    if not ORDER_FILE.exists():
        return []
    with open(ORDER_FILE, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except Exception:
            return []
