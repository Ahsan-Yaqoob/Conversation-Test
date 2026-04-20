import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(name)s — %(message)s',
)
logger = logging.getLogger(__name__)

# ── Stats cache ───────────────────────────────────────────────────────────────
_stats_cache: dict = {}
_stats_cache_ts: float = 0.0
_stats_lock = threading.Lock()
_STATS_TTL = 30  # seconds


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.scheduler import start_scheduler, stop_scheduler
    from app.database import check_table
    t = threading.Thread(target=start_scheduler, daemon=True)
    t.start()
    check_table()   # logs warning if migration hasn't been run yet
    yield
    stop_scheduler()


app = FastAPI(title='Quotation Session Analyzer', lifespan=lifespan)
app.mount('/static', StaticFiles(directory=str(BASE_DIR / 'static')), name='static')
templates = Jinja2Templates(directory=str(BASE_DIR / 'templates'))


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get('/', response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name='index.html')


@app.get('/api/sessions')
async def get_sessions():
    """Return the 10 most recently scraped sessions in order, enriched from cache."""
    from app.cache import get_all_sessions, get_latest_order

    cache = get_all_sessions()
    order = get_latest_order()

    # Fallback: if no order file yet, sort all cached sessions by scraped_at
    if not order and cache:
        def sort_key(item):
            return item[1].get('scraped_at', '') or ''
        sorted_items = sorted(cache.items(), key=sort_key, reverse=True)
        order = [sid for sid, _ in sorted_items[:10]]

    sessions_list = []
    for sid in order:
        if sid in cache:
            sessions_list.append(cache[sid])
        else:
            sessions_list.append({
                'session_id': sid,
                'status': 'loading',
                'analysis': None,
            })

    return {'sessions': sessions_list, 'total': len(sessions_list)}


@app.post('/api/analyze/{session_id}')
async def analyze_one(session_id: str):
    """Run Gemini analysis for a single session. Returns cached result if already analyzed."""
    from app.cache import get_session, save_session
    from app.analyzer import analyze_session, is_analyzing

    # If auto-analysis is already running for this session, return 202
    if is_analyzing(session_id):
        return JSONResponse(
            status_code=202,
            content={'in_progress': True, 'message': 'Analysis is already running for this session.'},
        )

    session = get_session(session_id)
    if not session:
        return {'error': 'Session not found', 'session_id': session_id}

    # Return existing analysis only if it's a successful result (ok or warning)
    # Error results are allowed to be retried
    existing = session.get('analysis', {})
    if existing and existing.get('overall_status') in ('ok', 'warning'):
        return {'analysis': existing, 'cached': True}

    # Refuse to analyze sessions with no data
    if not session.get('conversation') and not session.get('result_json'):
        return {'error': 'No conversation or result data to analyze', 'session_id': session_id}

    analysis = analyze_session(session)
    analysis['analyzed_at'] = datetime.now(timezone.utc).isoformat()
    session['analysis'] = analysis
    # reset_dismissed=True clears stale dismissed_issues from any prior analysis
    save_session(session_id, session, reset_dismissed=True)
    logger.info(
        f"Analyzed {session_id[:8]} → {analysis.get('overall_status')} "
        f"({len(analysis.get('issues', []))} issues)"
    )
    return {'analysis': analysis, 'cached': False}


@app.get('/api/sessions/{session_id}')
async def get_session_by_id(session_id: str):
    """Return a single session from cache (for polling analysis completion)."""
    from app.cache import get_session
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail='Session not found')
    return session


@app.post('/api/refresh')
async def refresh():
    """Trigger an immediate scrape + analyze cycle (non-blocking)."""
    from app.scheduler import is_job_running, run_scrape_and_analyze

    if is_job_running():
        return {'message': 'Already running', 'started': False}

    t = threading.Thread(target=run_scrape_and_analyze, daemon=True)
    t.start()
    return {'message': 'Refresh started', 'started': True}


@app.get('/api/status')
async def status():
    """Return whether the scrape job is currently running."""
    from app.scheduler import is_job_running, get_last_run
    from app.database import is_available
    from app.analyzer import _analyzing_ids
    return {
        'running': is_job_running(),
        'last_run': get_last_run(),
        'db_connected': is_available(),
        'analyzing': list(_analyzing_ids),
    }


# ── DB API endpoints ──────────────────────────────────────────────────────────

@app.get('/api/db/setup')
async def db_setup_sql():
    """Return the SQL migration to run in Supabase SQL Editor (one-time setup)."""
    from pathlib import Path
    sql_file = Path(__file__).parent / 'migrations' / '001_create_sessions.sql'
    sql = sql_file.read_text() if sql_file.exists() else '-- migration file not found'
    return {
        'sql_editor_url': 'https://supabase.com/dashboard/project/dqjtorcujhauozenfvch/sql/new',
        'sql': sql,
    }


@app.get('/api/db/sessions')
async def db_sessions(
    limit: int = 25,
    offset: int = 0,
    search: str = '',
    status: str = '',          # analysis status filter: ok|warning|error|pending
    date: str = '',            # today|week
    session_status: str = '',  # completed|active
):
    """
    Paginated, filterable session list from Supabase.
    Params: limit, offset, search, status, date (today|week), session_status
    """
    from app.database import list_sessions
    sessions_list, total = list_sessions(
        offset=offset, limit=limit,
        search=search, status_filter=status,
        date_filter=date, session_status=session_status,
    )
    return {
        'sessions': sessions_list,
        'total': total,
        'limit': limit,
        'offset': offset,
        'has_more': (offset + limit) < total,
    }


@app.get('/api/stats')
async def stats():
    """Return aggregate dashboard stats from DB, cached for 30s."""
    import time
    from app.database import get_stats_db
    global _stats_cache, _stats_cache_ts

    # Serve from cache if still fresh
    with _stats_lock:
        if _stats_cache and (time.monotonic() - _stats_cache_ts) < _STATS_TTL:
            return _stats_cache

    # Run the blocking DB call in a thread so we don't block the event loop
    loop = asyncio.get_event_loop()
    fresh = await loop.run_in_executor(None, get_stats_db)

    # Only update cache if we got real data (non-empty response)
    if fresh:
        with _stats_lock:
            _stats_cache = fresh
            _stats_cache_ts = time.monotonic()
        return fresh

    # DB failed — return cached data (even if stale) so cards don't blank
    with _stats_lock:
        if _stats_cache:
            return _stats_cache

    return {}  # genuinely no data yet


@app.get('/api/db/sessions/{session_id}')
async def db_session_detail(session_id: str):
    """Return full session data (conversation + result JSON). Merges DB + cache."""
    from app.database import get_session_db
    from app.cache import get_session as get_cache_session

    db_row = get_session_db(session_id)
    cached = get_cache_session(session_id)

    if not db_row and not cached:
        raise HTTPException(status_code=404, detail='Session not found')

    # Start with DB row (has flat analysis columns), fill missing data from cache
    result = dict(db_row) if db_row else {}
    if cached:
        # Cache has conversation / result_json / reference_data that DB may lack
        for field in ('conversation', 'result_json', 'reference_data'):
            if not result.get(field):
                result[field] = cached.get(field)
        # Also pull analysis if DB hasn't written it yet
        if not result.get('analysis_status') and cached.get('analysis'):
            a = cached['analysis']
            result['analysis_status']  = a.get('overall_status')
            result['analysis_summary'] = a.get('summary')
            result['analysis_issues']  = a.get('issues')
            result['extractor_rating'] = a.get('extractor_rating')
            result['rating_reason']    = a.get('rating_reason')
            result['analyzed_at']      = a.get('analyzed_at')

    # Always include dismissed_issues (defaults to empty list)
    if 'dismissed_issues' not in result:
        result['dismissed_issues'] = []

    return result


@app.get('/api/db/sessions/{session_id}/analysis')
async def db_session_analysis(session_id: str):
    """
    Return just the AI analysis and extractor rating for a session.
    Useful for external integrations.
    """
    from app.database import get_session_db
    session = get_session_db(session_id)
    if not session:
        raise HTTPException(status_code=404, detail='Session not found in database')
    return {
        'session_id':       session_id,
        'analysis_status':  session.get('analysis_status'),
        'analysis_summary': session.get('analysis_summary'),
        'analysis_issues':  session.get('analysis_issues') or [],
        'extractor_rating': session.get('extractor_rating'),
        'rating_reason':    session.get('rating_reason'),
        'analyzed_at':      session.get('analyzed_at'),
    }


@app.post('/api/export-prompts')
async def export_prompts(request: Request):
    """Overwrite chat.json in the project root with selected conversation prompts."""
    payload = await request.json()
    prompts = payload.get('prompts')
    if not isinstance(prompts, list):
        raise HTTPException(status_code=400, detail='prompts must be a list')

    out_file = BASE_DIR / 'chat.json'
    out_file.write_text(__import__('json').dumps(prompts, indent=2), encoding='utf-8')
    return {
        'ok': True,
        'count': len(prompts),
        'path': str(out_file),
    }



@app.patch('/api/db/sessions/{session_id}/issues/dismiss-all')
async def dismiss_all_endpoint(session_id: str, restore: bool = False):
    """
    Dismiss all active issues at once, or restore all dismissed ones.
    Pass ?restore=true to undo all dismissals.
    """
    from app.database import SessionBusyError, SessionNotFoundError, dismiss_all_issues
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None, lambda: dismiss_all_issues(session_id, restore)
        )
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail='Session not found')
    except SessionBusyError:
        raise HTTPException(
            status_code=503,
            detail='Database is busy. Please wait a moment and try again.',
        )

    global _stats_cache_ts
    with _stats_lock:
        _stats_cache_ts = 0.0

    return {
        'session_id':       session_id,
        'restored':         restore,
        'analysis_status':  result['analysis_status'],
        'orig_rating':      result['orig_rating'],
        'effective_rating': result['effective_rating'],
        'dismissed_issues': result['dismissed_issues'],
    }


@app.patch('/api/db/sessions/{session_id}/issues/{issue_index}')
async def dismiss_issue_endpoint(session_id: str, issue_index: int, restore: bool = False):
    """
    Dismiss or restore a single analysis issue by its index.
    Recomputes effective analysis_status and extractor_rating.
    Pass ?restore=true to undo a dismissal.
    """
    from app.database import SessionBusyError, SessionNotFoundError, dismiss_issue
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None, lambda: dismiss_issue(session_id, issue_index, restore)
        )
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail='Session not found')
    except SessionBusyError:
        raise HTTPException(
            status_code=503,
            detail='Database is busy. Please wait a moment and try again.',
        )

    # Invalidate stats cache so next load reflects updated status
    global _stats_cache_ts
    with _stats_lock:
        _stats_cache_ts = 0.0

    return {
        'session_id':       session_id,
        'issue_index':      issue_index,
        'restored':         restore,
        'analysis_status':  result['analysis_status'],
        'orig_rating':      result['orig_rating'],
        'effective_rating': result['effective_rating'],
        'dismissed_issues': result['dismissed_issues'],
    }


if __name__ == '__main__':
    import uvicorn
    uvicorn.run('main:app', host='0.0.0.0', port=8000, reload=False)
