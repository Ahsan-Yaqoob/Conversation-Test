import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
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
    from app.analyzer import analyze_session

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
    save_session(session_id, session)
    logger.info(
        f"Analyzed {session_id[:8]} → {analysis.get('overall_status')} "
        f"({len(analysis.get('issues', []))} issues)"
    )
    return {'analysis': analysis, 'cached': False}


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
    return {
        'running': is_job_running(),
        'last_run': get_last_run(),
        'db_connected': is_available(),
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
    limit: int = 50,
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
    from app.database import get_all_sessions_db
    sessions_list, total = get_all_sessions_db(
        limit=limit, offset=offset,
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
    """Return aggregate dashboard stats from DB."""
    from app.database import get_stats_db
    return get_stats_db()


@app.get('/api/db/sessions/{session_id}')
async def db_session_detail(session_id: str):
    """Return full session data (including conversation + result JSON) from DB."""
    from app.database import get_session_db
    session = get_session_db(session_id)
    if not session:
        raise HTTPException(status_code=404, detail='Session not found in database')
    return session


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


if __name__ == '__main__':
    import uvicorn
    uvicorn.run('main:app', host='0.0.0.0', port=8000, reload=False)
