import os
import logging
import threading
import time
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv('SUPABASE_URL', '')
SUPABASE_KEY = os.getenv('SUPABASE_KEY', '')

_client = None
_client_lock = threading.Lock()
_session_write_lock = threading.RLock()
_table_missing_warned = False  # log the missing-table error only once

# Track which session IDs already have their full data (conversation/result_json/reference_data)
# stored in Supabase. Populated on first DB write after startup. Prevents re-uploading large
# blobs on every scrape cycle or restart.
_db_stored_ids: set = set()
_db_ids_loaded: bool = False
_db_ids_lock = threading.Lock()


class SessionDBError(Exception):
    """Base error for session DB mutations."""


class SessionNotFoundError(SessionDBError):
    """The requested session does not exist in the DB or cache."""


class SessionBusyError(SessionDBError):
    """A transient DB failure occurred after retries."""


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


def is_session_stored(session_id: str) -> bool:
    """Return True if this session's full blobs are already in the DB."""
    with _db_ids_lock:
        return session_id in _db_stored_ids


def _load_db_ids():
    """Load all session IDs already stored in DB into the in-memory set (once per startup)."""
    global _db_ids_loaded
    client = get_client()
    if not client:
        return
    try:
        result = client.table('sessions').select('session_id').execute()
        ids = {r['session_id'] for r in (result.data or []) if r.get('session_id')}
        with _db_ids_lock:
            _db_stored_ids.update(ids)
            _db_ids_loaded = True
        logger.info(f"DB ID cache loaded: {len(_db_stored_ids)} sessions already stored")
    except Exception as e:
        logger.warning(f"Failed to load DB session IDs: {e}")
        with _db_ids_lock:
            _db_ids_loaded = True  # mark loaded anyway to avoid repeated failures


def _run_db_with_retry(operation, *, op_name: str, max_retries: int = 2, delay_seconds: float = 0.5):
    """Retry transient DB operations with a short backoff."""
    retry_count = 0
    while retry_count <= max_retries:
        try:
            return operation()
        except Exception as e:
            retry_count += 1
            if retry_count <= max_retries:
                logger.warning(f"{op_name} (retry {retry_count}/{max_retries}): {e}")
                time.sleep(delay_seconds * retry_count)
                continue
            logger.error(f"{op_name} (final): {e}")
            raise SessionBusyError(f"{op_name} failed") from e


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


def _meta_row_from_session(data: dict) -> dict:
    """
    Build a DB row with ONLY metadata and analysis fields — no large blob fields
    (conversation, result_json, reference_data). Used for updates on already-stored sessions.
    """
    analysis = data.get('analysis') or {}
    row = {
        'session_id':         data.get('session_id'),
        'status':             data.get('status'),
        'services':           data.get('services'),
        'msg_count':          data.get('msg_count'),
        'scraped_at':         data.get('scraped_at'),
        'session_created_at': data.get('created_at'),
        'db_updated_at':      datetime.now(timezone.utc).isoformat(),
    }
    if analysis:
        row.update({
            'analysis_status':  analysis.get('overall_status'),
            'analysis_summary': analysis.get('summary'),
            'analysis_issues':  analysis.get('issues'),
            'extractor_rating': analysis.get('extractor_rating'),
            'rating_reason':    analysis.get('rating_reason'),
            'analyzed_at':      analysis.get('analyzed_at'),
        })
    return {k: v for k, v in row.items() if v is not None}


def upsert_session(data: dict, reset_dismissed: bool = False):
    """
    Sync a session from the JSON cache to Supabase.
    reset_dismissed=True clears stale dismissed_issues when a fresh analysis is saved.
    """
    global _table_missing_warned
    client = get_client()
    if not client:
        return

    session_id = data.get('session_id')
    if not session_id:
        return

    # Ensure the in-memory set of already-stored IDs is populated
    if not _db_ids_loaded:
        _load_db_ids()

    with _db_ids_lock:
        already_stored = session_id in _db_stored_ids

    try:
        if already_stored:
            # Session already has full data in DB — only update metadata/analysis fields.
            # Never re-upload conversation/result_json/reference_data blobs.
            row = _meta_row_from_session(data)
            if reset_dismissed:
                row['dismissed_issues'] = None
            with _session_write_lock:
                _run_db_with_retry(
                    lambda: client.table('sessions').update(row).eq('session_id', session_id).execute(),
                    op_name=f"DB meta-update [{session_id[:8]}]",
                )
        else:
            # First time — do a full insert including all blob fields
            row = _row_from_session(data)
            if reset_dismissed:
                row['dismissed_issues'] = None
            with _session_write_lock:
                _run_db_with_retry(
                    lambda: client.table('sessions').upsert(row, on_conflict='session_id').execute(),
                    op_name=f"DB full-insert [{session_id[:8]}]",
                )
            # Mark as fully stored so future calls skip blob re-upload
            with _db_ids_lock:
                _db_stored_ids.add(session_id)

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
            logger.error(f"DB upsert error [{session_id[:8]}]: {e}")


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
            # Fetch analysis_issues to compute count, but strip it before sending to browser
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

            # Strip large blob fields — replace analysis_issues with a count integer.
            # The full issues array is only needed in the detail panel (fetched via get_session_db).
            rows = []
            for row in (result.data or []):
                row['issue_count'] = len(row.get('analysis_issues') or [])
                row.pop('analysis_issues', None)
                rows.append(row)
            return rows, result.count or 0
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



_stats_cache: dict = {}
_stats_cache_time: float = 0.0
_STATS_CACHE_TTL = 300  # seconds — stats are aggregate counts, 5-minute freshness is fine


def get_stats_db() -> dict:
    """Return aggregate counts for the dashboard stats bar (cached 5 min)."""
    global _stats_cache, _stats_cache_time
    import time as _time
    if _stats_cache and (_time.monotonic() - _stats_cache_time) < _STATS_CACHE_TTL:
        return _stats_cache

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

            computed = {
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
            import time as _time2
            _stats_cache.update(computed)
            _stats_cache_time = _time2.monotonic()
            return computed
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
    effective_rating: integer adjustment added to original rating
    """
    remaining = [iss for i, iss in enumerate(issues) if i not in dismissed]

    if any(i.get('severity') == 'high' for i in remaining):
        eff_status = 'error'
    elif any(i.get('severity') in ('medium', 'warning') for i in remaining):
        eff_status = 'warning'
    elif remaining:
        eff_status = 'warning'
    else:
        eff_status = 'ok'

    # Each dismissed issue recovers one rating point.
    # A perfect 10 is only allowed when no active issues remain.
    return eff_status, len(dismissed)


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


def _get_session_issue_row(client, session_id: str) -> dict | None:
    return _run_db_with_retry(
        lambda: (
            client.table('sessions')
            .select('analysis_issues,extractor_rating,dismissed_issues')
            .eq('session_id', session_id)
            .maybe_single()
            .execute()
        ).data,
        op_name=f"DB issue lookup [{session_id[:8]}]",
    )


def dismiss_issue(session_id: str, issue_index: int, restore: bool = False) -> dict:
    """
    Dismiss (or restore) a single issue by its index in analysis_issues.
    Only updates dismissed_issues + analysis_status in DB.
    extractor_rating is NEVER overwritten — effective rating is computed in frontend.
    Returns updated fields dict on success.
    """
    client = get_client()
    if not client:
        raise SessionBusyError('Database unavailable')
    try:
        with _session_write_lock:
            row = _get_session_issue_row(client, session_id)
            if not row:
                # Session missing from DB — try to sync from cache then re-fetch
                _ensure_session_in_db(client, session_id)
                row = _get_session_issue_row(client, session_id)
            if not row:
                raise SessionNotFoundError(f"Session {session_id} not found")

            issues      = row.get('analysis_issues') or []
            orig_rating = row.get('extractor_rating') or 0
            dismissed   = list(row.get('dismissed_issues') or [])

            if restore:
                dismissed = [i for i in dismissed if i != issue_index]
            else:
                if issue_index not in dismissed:
                    dismissed.append(issue_index)

            eff_status, bonus = _recompute_effective(issues, dismissed)
            if orig_rating:
                # All dismissed (or no issues) → allow 10; active issues remain → cap at 9
                max_rating = 10 if len(dismissed) >= len(issues) else 9
                eff_rating = min(max_rating, max(1, orig_rating + bonus))
            else:
                eff_rating = None

            update = {
                'dismissed_issues': dismissed,
                'analysis_status':  eff_status,
                'db_updated_at':    datetime.now(timezone.utc).isoformat(),
            }

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
    except SessionDBError:
        raise
    except Exception as e:
        logger.error(f"dismiss_issue error [{session_id[:8]}]: {e}")
        raise SessionBusyError('Unexpected dismiss issue error') from e


def dismiss_all_issues(session_id: str, restore: bool = False) -> dict:
    """
    Dismiss all active issues at once (or restore all dismissed ones).
    Only updates dismissed_issues + analysis_status.  extractor_rating is never touched.
    """
    client = get_client()
    if not client:
        raise SessionBusyError('Database unavailable')
    try:
        with _session_write_lock:
            row = _get_session_issue_row(client, session_id)
            if not row:
                # Session missing from DB — try to sync from cache then re-fetch
                _ensure_session_in_db(client, session_id)
                row = _get_session_issue_row(client, session_id)
            if not row:
                raise SessionNotFoundError(f"Session {session_id} not found")

            issues      = row.get('analysis_issues') or []
            orig_rating = row.get('extractor_rating') or 0

            dismissed = [] if restore else list(range(len(issues)))

            eff_status, bonus = _recompute_effective(issues, dismissed)
            if orig_rating:
                # All dismissed (or no issues) → allow 10; active issues remain → cap at 9
                max_rating = 10 if len(dismissed) >= len(issues) else 9
                eff_rating = min(max_rating, max(1, orig_rating + bonus))
            else:
                eff_rating = None

            update = {
                'dismissed_issues': dismissed,
                'analysis_status':  eff_status,
                'db_updated_at':    datetime.now(timezone.utc).isoformat(),
            }
            _run_db_with_retry(
                lambda: client.table('sessions').update(update).eq('session_id', session_id).execute(),
                op_name=f"DB dismiss-all update [{session_id[:8]}]",
            )
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
    except SessionDBError:
        raise
    except Exception as e:
        logger.error(f"dismiss_all error [{session_id[:8]}]: {e}")
        raise SessionBusyError('Unexpected dismiss-all error') from e


def check_table() -> bool:
    """Check if sessions table exists and pre-load stored session IDs. Logs instructions if missing."""
    client = get_client()
    if not client:
        return False
    try:
        client.table('sessions').select('session_id').limit(1).execute()
        logger.info("Supabase sessions table: ✓ ready")
        # Pre-load stored IDs so the first scrape/backfill knows what's already in DB
        _load_db_ids()
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
    """
    Sync JSON cache sessions into DB on startup.
    - Sessions NOT yet in DB: full insert (conversation + result_json + reference_data).
    - Sessions already in DB: skip entirely (data is already there — no re-upload).
    Analysis updates for existing sessions happen naturally via upsert_session when
    analysis completes.
    """
    client = get_client()
    if not client or not cache:
        return

    # Load which sessions already exist in DB (populates _db_stored_ids)
    if not _db_ids_loaded:
        _load_db_ids()

    with _db_ids_lock:
        already_in_db = set(_db_stored_ids)

    new_count = 0
    for data in cache.values():
        sid = data.get('session_id')
        if not sid:
            continue
        if sid in already_in_db:
            continue  # Full data already stored — skip re-upload
        upsert_session(data)
        new_count += 1

    if new_count:
        logger.info(f"DB backfill complete: {new_count} new sessions inserted")
    else:
        logger.info("DB backfill: all sessions already stored, nothing to upload")
