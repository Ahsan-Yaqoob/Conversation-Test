import os
import logging
import threading
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv('SUPABASE_URL', '')
SUPABASE_KEY = os.getenv('SUPABASE_KEY', '')

_client = None
_client_lock = threading.Lock()
_table_missing_warned = False  # log the missing-table error only once


def get_client():
    global _client
    with _client_lock:
        if _client is not None:
            return _client
        if not SUPABASE_URL or not SUPABASE_KEY:
            logger.warning("Supabase credentials not configured — DB disabled")
            return None
        try:
            from supabase import create_client
            _client = create_client(SUPABASE_URL, SUPABASE_KEY)
            logger.info(f"Supabase connected: {SUPABASE_URL}")
            return _client
        except Exception as e:
            logger.error(f"Supabase init failed: {e}")
            return None


def is_available() -> bool:
    return get_client() is not None


def _row_from_session(data: dict) -> dict:
    """Convert a cache session dict into a DB row dict."""
    analysis = data.get('analysis') or {}
    row = {
        'session_id':       data.get('session_id'),
        'status':           data.get('status'),
        'services':         data.get('services'),
        'msg_count':        data.get('msg_count'),
        'scraped_at':       data.get('scraped_at'),
        'conversation':     data.get('conversation'),
        'result_json':      data.get('result_json'),
        'reference_data':   data.get('reference_data'),
        'analysis_status':  analysis.get('overall_status'),
        'analysis_summary': analysis.get('summary'),
        'analysis_issues':  analysis.get('issues'),
        'extractor_rating': analysis.get('extractor_rating'),
        'rating_reason':    analysis.get('rating_reason'),
        'analyzed_at':      analysis.get('analyzed_at'),
        'db_updated_at':    datetime.now(timezone.utc).isoformat(),
    }
    # Keep None for explicit null fields, but drop completely missing keys
    return {k: v for k, v in row.items() if k in row}


def upsert_session(data: dict):
    """Sync a session from the JSON cache to Supabase."""
    global _table_missing_warned
    client = get_client()
    if not client:
        return
    try:
        row = _row_from_session(data)
        if not row.get('session_id'):
            return
        client.table('sessions').upsert(row, on_conflict='session_id').execute()
        _table_missing_warned = False  # reset on success
    except Exception as e:
        msg = str(e)
        if 'PGRST205' in msg or 'schema cache' in msg.lower():
            if not _table_missing_warned:
                _table_missing_warned = True
                logger.error(
                    "Supabase 'sessions' table missing — run the migration SQL:\n"
                    "  → https://supabase.com/dashboard/project/dqjtorcujhauozenfvch/sql/new\n"
                    "  → Paste & run: migrations/001_create_sessions.sql\n"
                    "(This message will not repeat until the table is created.)"
                )
        else:
            logger.error(f"DB upsert error [{data.get('session_id', '?')[:8]}]: {e}")


def get_session_db(session_id: str) -> dict | None:
    client = get_client()
    if not client:
        return None
    try:
        result = (
            client.table('sessions')
            .select('*')
            .eq('session_id', session_id)
            .maybe_single()
            .execute()
        )
        return result.data
    except Exception as e:
        logger.error(f"DB get_session error: {e}")
        return None


def get_all_sessions_db(
    limit: int = 50,
    offset: int = 0,
    search: str = '',
    status_filter: str = '',       # 'ok' | 'warning' | 'error' | 'pending' | ''
    date_filter: str = '',         # 'today' | 'week' | ''
    session_status: str = '',      # 'completed' | 'active' | ''
) -> tuple[list, int]:
    """Returns (sessions_list, total_count) applying filters."""
    client = get_client()
    if not client:
        return [], 0
    try:
        cols = (
            'session_id,status,services,msg_count,scraped_at,'
            'analysis_status,analysis_summary,analysis_issues,'
            'extractor_rating,rating_reason,analyzed_at,db_updated_at'
        )
        q = client.table('sessions').select(cols, count='exact')

        if search:
            q = q.ilike('session_id', f'%{search}%')
        if status_filter == 'pending':
            q = q.is_('analysis_status', 'null')
        elif status_filter:
            q = q.eq('analysis_status', status_filter)
        if session_status:
            q = q.ilike('status', session_status)
        if date_filter == 'today':
            today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            q = q.gte('scraped_at', today)
        elif date_filter == 'week':
            from datetime import timedelta
            week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            q = q.gte('scraped_at', week_ago)

        result = q.order('scraped_at', desc=True).range(offset, offset + limit - 1).execute()
        return result.data or [], result.count or 0
    except Exception as e:
        logger.error(f"DB list error: {e}")
        return [], 0


def get_stats_db() -> dict:
    """Return aggregate counts for the dashboard stats bar."""
    client = get_client()
    if not client:
        return {}
    try:
        from datetime import timedelta
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        def _count(**filters):
            q = client.table('sessions').select('session_id', count='exact')
            for k, v in filters.items():
                if k == 'gte_scraped_at':
                    q = q.gte('scraped_at', v)
                elif k == 'eq_analysis_status':
                    q = q.eq('analysis_status', v)
                elif k == 'null_analysis_status':
                    q = q.is_('analysis_status', 'null')
                elif k == 'ilike_status':
                    q = q.ilike('status', v)
            return q.execute().count or 0

        return {
            'total':     _count(),
            'today':     _count(gte_scraped_at=today),
            'ok':        _count(eq_analysis_status='ok'),
            'warning':   _count(eq_analysis_status='warning'),
            'error':     _count(eq_analysis_status='error'),
            'pending':   _count(null_analysis_status=True),
            'completed': _count(ilike_status='completed'),
        }
    except Exception as e:
        logger.error(f"DB stats error: {e}")
        return {}


def check_table() -> bool:
    """Check if sessions table exists. Logs instructions if missing."""
    client = get_client()
    if not client:
        return False
    try:
        client.table('sessions').select('session_id').limit(1).execute()
        logger.info("Supabase sessions table: ✓ ready")
        return True
    except Exception as e:
        msg = str(e)
        if 'PGRST205' in msg or 'schema cache' in msg.lower():
            logger.warning(
                "Supabase sessions table not found! "
                "Run migrations/001_create_sessions.sql in your Supabase SQL Editor: "
                "https://supabase.com/dashboard/project/dqjtorcujhauozenfvch/sql/new"
            )
        else:
            logger.error(f"Supabase table check error: {e}")
        return False


def backfill_from_cache(cache: dict):
    """Upsert all JSON cache sessions into DB — run once on startup."""
    client = get_client()
    if not client or not cache:
        return
    count = 0
    for data in cache.values():
        if data.get('session_id'):
            upsert_session(data)
            count += 1
    if count:
        logger.info(f"DB backfill complete: {count} sessions synced")
