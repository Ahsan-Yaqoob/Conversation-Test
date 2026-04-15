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
        'session_created_at': data.get('created_at'),
        'conversation':     data.get('conversation'),
        'result_json':      data.get('result_json'),
        'reference_data':   data.get('reference_data'),
        'db_updated_at':    datetime.now(timezone.utc).isoformat(),
    }
    # Only include analysis columns when we actually have analysis data.
    # Omitting them preserves any existing DB analysis state (important on
    # ephemeral-filesystem restarts where the JSON cache may be gone).
    if analysis:
        row.update({
            'analysis_status':  analysis.get('overall_status'),
            'analysis_summary': analysis.get('summary'),
            'analysis_issues':  analysis.get('issues'),
            'extractor_rating': analysis.get('extractor_rating'),
            'rating_reason':    analysis.get('rating_reason'),
            'analyzed_at':      analysis.get('analyzed_at'),
        })
    # Keep None for explicit null fields, but drop completely missing keys
    return {k: v for k, v in row.items() if k in row}


def upsert_session(data: dict, reset_dismissed: bool = False):
    """
    Sync a session from the JSON cache to Supabase.
    reset_dismissed=True clears stale dismissed_issues when a fresh analysis is saved.
    """
    global _table_missing_warned
    client = get_client()
    if not client:
        return
    try:
        row = _row_from_session(data)
        if not row.get('session_id'):
            return
        # Clear old dismiss state so stale indices don't corrupt new analysis display
        if reset_dismissed:
            row['dismissed_issues'] = None
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
    limit: int = 25,
    offset: int = 0,
    search: str = '',
    status_filter: str = '',       # 'ok' | 'warning' | 'error' | 'pending' | ''
    date_filter: str = '',         # 'today' | 'week' | ''
    session_status: str = '',      # 'completed' | 'active' | ''
def list_sessions(
    offset: int = 0,
    limit: int = 50,
    search: str = '',
    status_filter: str = '',
    session_status: str = '',
    date_filter: str = '',
) -> tuple[list, int]:
    """Returns (sessions_list, total_count) applying filters."""
    client = get_client()
    if not client:
        return [], 0
    
    # Retry logic for transient connection errors
    max_retries = 2
    retry_count = 0
    
    while retry_count <= max_retries:
        try:
            cols = (
                'session_id,status,services,msg_count,scraped_at,session_created_at,'
                'analysis_status,analysis_summary,analysis_issues,'
                'extractor_rating,rating_reason,analyzed_at,db_updated_at,dismissed_issues'
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
                q = q.gte('session_created_at', today)
            elif date_filter == 'week':
                from datetime import timedelta
                week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                q = q.gte('session_created_at', week_ago)

            result = q.order('session_created_at', desc=True, nullsfirst=False).range(offset, offset + limit - 1).execute()
            return result.data or [], result.count or 0
        except Exception as e:
            retry_count += 1
            if retry_count <= max_retries:
                logger.warning(f"DB list error (retry {retry_count}/{max_retries}): {e}")
                import time
                time.sleep(0.5)  # Wait before retry
                continue
            else:
                logger.error(f"DB list error (final): {e}")
                return [], 0



def get_stats_db() -> dict:
    """Return aggregate counts for the dashboard stats bar."""
    client = get_client()
    if not client:
        return {}
    
    # Retry logic for transient connection errors
    max_retries = 2
    retry_count = 0
    
    while retry_count <= max_retries:
        try:
            from datetime import timedelta
            now = datetime.now(timezone.utc)
            today = now.strftime('%Y-%m-%d')
            week_ago = (now - timedelta(days=7)).isoformat()
            month_ago = (now - timedelta(days=30)).isoformat()

            def _count(**filters):
                q = client.table('sessions').select('session_id', count='exact')
                for k, v in filters.items():
                    if k == 'gte_scraped_at':
                        q = q.gte('session_created_at', v)
                    elif k == 'eq_analysis_status':
                        q = q.eq('analysis_status', v)
                    elif k == 'null_analysis_status':
                        q = q.is_('analysis_status', 'null')
                    elif k == 'ilike_status':
                        q = q.ilike('status', v)
                    elif k == 'eq_status':
                        q = q.eq('status', v)
                return q.execute().count or 0

            completed        = _count(ilike_status='completed')
            ok_completed     = _count(ilike_status='completed', eq_analysis_status='ok')
            ok_pct           = round(ok_completed / completed * 100) if completed else 0
            completed_today  = _count(eq_status='completed', gte_scraped_at=today)
            today_total      = _count(gte_scraped_at=today)
            today_ok         = _count(gte_scraped_at=today, eq_analysis_status='ok')
            today_err        = _count(gte_scraped_at=today, eq_analysis_status='error')
            today_analyzed   = today_ok + today_err
            today_pct        = round(today_ok / today_analyzed * 100) if today_analyzed else 0
            week_ok          = _count(gte_scraped_at=week_ago, eq_analysis_status='ok')
            week_err         = _count(gte_scraped_at=week_ago, eq_analysis_status='error')
            week_analyzed    = week_ok + week_err
            week_pct         = round(week_ok / week_analyzed * 100) if week_analyzed else 0
            month_ok         = _count(gte_scraped_at=month_ago, eq_analysis_status='ok')
            month_err        = _count(gte_scraped_at=month_ago, eq_analysis_status='error')
            month_analyzed   = month_ok + month_err
            month_pct        = round(month_ok / month_analyzed * 100) if month_analyzed else 0

            return {
                'total':           _count(),
                'today':           today_total,
                'ok':              _count(eq_analysis_status='ok'),
                'warning':         _count(eq_analysis_status='warning'),
                'error':           _count(eq_analysis_status='error'),
                'pending':         _count(null_analysis_status=True),
                'completed':       completed,
                'ok_pct':          ok_pct,
                'ok_completed':    ok_completed,
                'completed_today': completed_today,
                'today_pct':       today_pct,
                'today_ok':        today_ok,
                'today_analyzed':  today_analyzed,
                'week_pct':        week_pct,
                'week_ok':         week_ok,
                'week_analyzed':   week_analyzed,
                'month_pct':       month_pct,
                'month_ok':        month_ok,
                'month_analyzed':  month_analyzed,
            }
        except Exception as e:
            retry_count += 1
            if retry_count <= max_retries:
                logger.warning(f"DB stats error (retry {retry_count}/{max_retries}): {e}")
                import time
                time.sleep(0.5)
                continue
            else:
                logger.error(f"DB stats error (final): {e}")
                return {}


def _recompute_effective(issues: list, dismissed: list[int]) -> tuple[str | None, int | None]:
    """
    Given the full issues list and dismissed indices, return
    (effective_status, effective_rating_adjustment).
    effective_status: 'ok' | 'warning' | 'error'
    effective_rating: integer adjustment added to original rating (capped 1-10)
    """
    remaining = [iss for i, iss in enumerate(issues) if i not in dismissed]
    dismissed_issues = [iss for i, iss in enumerate(issues) if i in dismissed]

    if any(i.get('severity') == 'high' for i in remaining):
        eff_status = 'error'
    elif any(i.get('severity') in ('medium', 'warning') for i in remaining):
        eff_status = 'warning'
    elif remaining:
        eff_status = 'warning'
    else:
        eff_status = 'ok'

    # Points recovered per dismissed issue
    bonus = sum(
        2 if i.get('severity') == 'high' else 1
        for i in dismissed_issues
    )
    # When all issues are dismissed → perfect score
    if not remaining:
        eff_status = 'ok'
        bonus = 10  # will be capped properly by caller
    return eff_status, bonus


def _ensure_session_in_db(client, session_id: str) -> bool:
    """
    If the session isn't in the DB yet, try to upsert it from the JSON cache.
    Returns True if the session now exists in DB, False otherwise.
    """
    try:
        from app.cache import get_session as cache_get_session
        cached = cache_get_session(session_id)
        if cached:
            upsert_session(cached)
            logger.info(f"_ensure_session_in_db: upserted {session_id[:8]}… from cache")
            return True
    except Exception as e:
        logger.warning(f"_ensure_session_in_db cache fallback failed [{session_id[:8]}]: {e}")
    return False


def dismiss_issue(session_id: str, issue_index: int, restore: bool = False) -> dict | None:
    """
    Dismiss (or restore) a single issue by its index in analysis_issues.
    Only updates dismissed_issues + analysis_status in DB.
    extractor_rating is NEVER overwritten — effective rating is computed in frontend.
    Returns updated fields dict on success, None on failure.
    """
    client = get_client()
    if not client:
        return None
    try:
        row = (
            client.table('sessions')
            .select('analysis_issues,extractor_rating,dismissed_issues')
            .eq('session_id', session_id)
            .maybe_single()
            .execute()
        ).data
        if not row:
            # Session missing from DB — try to sync from cache then re-fetch
            _ensure_session_in_db(client, session_id)
            row = (
                client.table('sessions')
                .select('analysis_issues,extractor_rating,dismissed_issues')
                .eq('session_id', session_id)
                .maybe_single()
                .execute()
            ).data
        if not row:
            return None

        issues      = row.get('analysis_issues') or []
        orig_rating = row.get('extractor_rating') or 0
        dismissed   = list(row.get('dismissed_issues') or [])

        if restore:
            dismissed = [i for i in dismissed if i != issue_index]
        else:
            if issue_index not in dismissed:
                dismissed.append(issue_index)

        eff_status, bonus = _recompute_effective(issues, dismissed)
        # When all dismissed bonus==10 → always 10; otherwise cap normally
        eff_rating = 10 if bonus == 10 else (min(10, max(1, orig_rating + bonus)) if orig_rating else None)

        update = {
            'dismissed_issues': dismissed,
            'analysis_status':  eff_status,
            'db_updated_at':    datetime.now(timezone.utc).isoformat(),
        }
        client.table('sessions').update(update).eq('session_id', session_id).execute()
        logger.info(f"dismiss_issue {session_id[:8]}… idx={issue_index} restore={restore} → {eff_status} eff_rating={eff_rating}")
        # Sync back to cache so backfill never overwrites dismissed state
        try:
            from app.cache import update_session_dismiss_state
            update_session_dismiss_state(session_id, eff_status, dismissed)
        except Exception as ce:
            logger.warning(f"Cache dismiss sync failed [{session_id[:8]}]: {ce}")
        return {
            **update,
            'orig_rating':      orig_rating,
            'effective_rating': eff_rating,
        }
    except Exception as e:
        logger.error(f"dismiss_issue error [{session_id[:8]}]: {e}")
        return None


def dismiss_all_issues(session_id: str, restore: bool = False) -> dict | None:
    """
    Dismiss all active issues at once (or restore all dismissed ones).
    Only updates dismissed_issues + analysis_status.  extractor_rating is never touched.
    """
    client = get_client()
    if not client:
        return None
    try:
        row = (
            client.table('sessions')
            .select('analysis_issues,extractor_rating,dismissed_issues')
            .eq('session_id', session_id)
            .maybe_single()
            .execute()
        ).data
        if not row:
            # Session missing from DB — try to sync from cache then re-fetch
            _ensure_session_in_db(client, session_id)
            row = (
                client.table('sessions')
                .select('analysis_issues,extractor_rating,dismissed_issues')
                .eq('session_id', session_id)
                .maybe_single()
                .execute()
            ).data
        if not row:
            return None

        issues      = row.get('analysis_issues') or []
        orig_rating = row.get('extractor_rating') or 0

        dismissed = [] if restore else list(range(len(issues)))

        eff_status, bonus = _recompute_effective(issues, dismissed)
        eff_rating = 10 if bonus == 10 else (min(10, max(1, orig_rating + bonus)) if orig_rating else None)

        update = {
            'dismissed_issues': dismissed,
            'analysis_status':  eff_status,
            'db_updated_at':    datetime.now(timezone.utc).isoformat(),
        }
        client.table('sessions').update(update).eq('session_id', session_id).execute()
        logger.info(f"dismiss_all {session_id[:8]}… restore={restore} → {eff_status} eff_rating={eff_rating}")
        # Sync back to cache so backfill never overwrites dismissed state
        try:
            from app.cache import update_session_dismiss_state
            update_session_dismiss_state(session_id, eff_status, dismissed)
        except Exception as ce:
            logger.warning(f"Cache dismiss sync failed [{session_id[:8]}]: {ce}")
        return {
            **update,
            'orig_rating':      orig_rating,
            'effective_rating': eff_rating,
        }
    except Exception as e:
        logger.error(f"dismiss_all error [{session_id[:8]}]: {e}")
        return None


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
