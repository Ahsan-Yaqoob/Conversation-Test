import os
import asyncio
import logging
import threading
from datetime import datetime, timezone
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_scheduler = BackgroundScheduler()
_running_lock = threading.Lock()
_is_running = False
_last_run: str | None = None


def is_job_running() -> bool:
    return _is_running


def get_last_run() -> str | None:
    return _last_run


def run_scrape_and_analyze():
    """
    Scrape-only job — saves raw session data to cache.
    AI analysis is triggered manually per session via the frontend.
    """
    global _is_running, _last_run

    with _running_lock:
        if _is_running:
            logger.info("Job already running, skipping.")
            return
        _is_running = True

    try:
        from scraper.scrape import scrape_sessions
        from app.cache import get_cached_ids, save_session, save_latest_order

        logger.info("── Scrape job started ──────────────────────────────")
        cached_ids = get_cached_ids()
        sessions = asyncio.run(scrape_sessions(cached_ids))

        # Save ordered list for display
        order = [s['session_id'] for s in sessions]
        save_latest_order(order)
        logger.info(f"Latest order saved: {[sid[:8] for sid in order]}")

        new_count = 0
        for session in sessions:
            if session.get('is_cached'):
                continue
            session_id = session['session_id']
            # Merge: preserve existing analysis if already done
            from app.cache import get_session
            existing = get_session(session_id) or {}
            if existing.get('analysis'):
                session['analysis'] = existing['analysis']
            save_session(session_id, session)
            new_count += 1
            logger.info(f"Saved session {session_id[:8]}…")

        _last_run = datetime.now(timezone.utc).isoformat()
        logger.info(f"── Scrape complete. {new_count} sessions saved/updated. ──")

    except Exception as e:
        logger.error(f"Scrape job error: {e}", exc_info=True)
    finally:
        _is_running = False


def start_scheduler():
    interval = int(os.getenv('CRON_INTERVAL_MINUTES', '5'))

    _scheduler.add_job(
        run_scrape_and_analyze,
        'interval',
        minutes=interval,
        id='scrape_job',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info(f"Scheduler started — running every {interval} minute(s).")

    # Backfill existing JSON cache into Supabase (best-effort, non-blocking)
    def _backfill():
        try:
            from app.cache import get_all_sessions
            from app.database import backfill_from_cache
            backfill_from_cache(get_all_sessions())
        except Exception as e:
            logger.warning(f"DB backfill skipped: {e}")

    threading.Thread(target=_backfill, daemon=True).start()

    # Run scrape immediately on startup
    t = threading.Thread(target=run_scrape_and_analyze, daemon=True)
    t.start()


def stop_scheduler():
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")
