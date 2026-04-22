import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / 'data'
CACHE_FILE = DATA_DIR / 'sessions.json'
ORDER_FILE = DATA_DIR / 'latest_order.json'

_lock = threading.Lock()
_cache: dict | None = None  # in-memory cache — loaded once, updated in place


_BLOB_KEYS = ('conversation', 'result_json', 'reference_data')


def _strip_blobs(data: dict) -> dict:
    """Remove large blob fields — they're stored in Supabase, no need to keep in RAM."""
    return {k: v for k, v in data.items() if k not in _BLOB_KEYS}


def _ensure_dir():
    DATA_DIR.mkdir(exist_ok=True)


# ── sessions cache ────────────────────────────────────────────────────────────

def _load_from_disk() -> dict:
    _ensure_dir()
    if not CACHE_FILE.exists():
        return {}
    with open(CACHE_FILE, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def load_cache() -> dict:
    global _cache
    if _cache is None:
        _cache = _load_from_disk()
    return _cache


def _save_cache(cache: dict):
    global _cache
    _cache = cache  # keep in-memory copy in sync
    _ensure_dir()
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def update_session_dismiss_state(session_id: str, analysis_status: str, dismissed_issues: list):
    """
    Sync dismiss state back into the JSON cache so backfill never overwrites it.
    Called after every dismiss/restore DB update.
    """
    with _lock:
        cache = load_cache()
        if session_id not in cache:
            return
        session = cache[session_id]
        if session.get('analysis'):
            session['analysis']['overall_status'] = analysis_status
        session['dismissed_issues'] = dismissed_issues
        _save_cache(cache)


def save_session(session_id: str, data: dict, reset_dismissed: bool = False):
    with _lock:
        cache = load_cache()
        cache[session_id] = data
        _save_cache(cache)
    # Sync to Supabase outside the lock (non-blocking, best-effort)
    try:
        from app.database import upsert_session, is_session_stored
        upsert_session(data, reset_dismissed=reset_dismissed)
        # Strip blobs from RAM once analysis is done and data is safely in DB.
        # Blobs are only needed for analysis; the detail panel fetches them from DB on demand.
        overall = (data.get('analysis') or {}).get('overall_status', '')
        if overall in ('ok', 'warning', 'error') and is_session_stored(session_id):
            with _lock:
                c = load_cache()
                if session_id in c:
                    c[session_id] = _strip_blobs(c[session_id])
                    _save_cache(c)
    except Exception as e:
        logger.warning(f"DB sync skipped for {session_id[:8]}: {e}")


def get_session(session_id: str) -> dict | None:
    return load_cache().get(session_id)


def is_cached(session_id: str) -> bool:
    return session_id in load_cache()


def get_cached_ids() -> set:
    """
    Return IDs of sessions that should NOT be re-fetched:
    - completed sessions that already have data (blobs may have been stripped after DB upload)
    """
    cache = load_cache()
    skip = set()
    for sid, data in cache.items():
        status = (data.get('status') or '').lower()
        # has_data: blobs present OR already stripped (analysis done → blobs removed)
        has_data = (data.get('conversation') or data.get('result_json')
                    or data.get('analysis'))
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
            # blobs may be stripped after analysis — treat as having data if analysis exists
            'has_data': bool(data.get('conversation') or data.get('result_json')
                             or data.get('analysis')),
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
