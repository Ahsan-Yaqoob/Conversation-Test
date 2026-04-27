import os
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


def _auto_analyze_worker(session_id: str, session_data: dict):
    """Background worker: analyze one session using the shared concurrency-safe runner."""
    from app.analyzer import run_analysis_safe
    run_analysis_safe(session_id, session_data)


def run_scrape_and_analyze():
    """
    Fetch job — pulls latest sessions from the API, saves to cache/DB,
    and auto-analyzes any newly completed sessions in background threads.
    """
    global _is_running, _last_run

    with _running_lock:
        if _is_running:
            logger.info("Job already running, skipping.")
            return
        _is_running = True

    try:
        from scraper.api_fetch import fetch_new_sessions
        from app.cache import get_cached_ids, get_cache_snapshot, save_session, save_latest_order

        logger.info("── Fetch job started ──────────────────────────────")
        cached_ids = get_cached_ids()
        cache_snapshot = get_cache_snapshot()
        sessions = fetch_new_sessions(cached_ids, cache_snapshot)

        # Save ordered list for display
        order = [s['session_id'] for s in sessions]
        save_latest_order(order)
        logger.info(f"Latest order saved: {[sid[:8] for sid in order]}")

        new_count = 0
        to_analyze = []  # sessions to auto-analyze after saving
        _cached_created_sync = []  # collect small updates and apply in batch

        for session in sessions:
            session_id = session['session_id']
            if session.get('is_cached'):
                # Defer syncing session_created_at for cached sessions — collect and batch upsert later
                created_at = session.get('created_at')
                if created_at:
                    _cached_created_sync.append({'session_id': session_id, 'session_created_at': created_at})
                continue
            # Merge: preserve existing analysis + dismissed state + original scraped_at
            from app.cache import get_session
            existing = get_session(session_id) or {}
            if existing.get('analysis'):
                session['analysis'] = existing['analysis']
            # Also preserve dismissed_issues so dismiss state survives re-scrape
            if existing.get('dismissed_issues') is not None:
                session['dismissed_issues'] = existing['dismissed_issues']
            # Preserve original scraped_at so old sessions don't float to top on re-scrape
            if existing.get('scraped_at') and not session.get('scrape_error'):
                session['scraped_at'] = existing['scraped_at']
            save_session(session_id, session)
            new_count += 1
            logger.info(f"Saved session {session_id[:8]}…")

            # Queue for auto-analysis only if completed, has data, and not yet analyzed locally
            if (session.get('status', '').lower() == 'completed'
                    and not session.get('analysis')
                    and (session.get('conversation') or session.get('result_json'))):
                to_analyze.append((session_id, session))

        _last_run = datetime.now(timezone.utc).isoformat()

        # Guard: skip re-analysis for sessions already analyzed in DB (e.g. after ephemeral cache loss)
        if to_analyze:
            try:
                from app.database import get_client
                db_client = get_client()
                if db_client:
                    # Fetch analysis_status for all candidate sessions, filter in Python
                    rows = db_client.table('sessions') \
                        .select('session_id,analysis_status') \
                        .in_('session_id', [sid for sid, _ in to_analyze]) \
                        .execute().data or []
                    already_analyzed = {
                        r['session_id'] for r in rows
                        if r.get('analysis_status') not in (None, '')
                    }
                    if already_analyzed:
                        logger.info(f"Skipping re-analysis for {len(already_analyzed)} already-analyzed sessions: {[s[:8] for s in already_analyzed]}")
                        to_analyze = [(sid, sess) for sid, sess in to_analyze if sid not in already_analyzed]
            except Exception as e:
                logger.warning(f"DB analysis-check skipped: {e}")

        logger.info(f"── Fetch complete. {new_count} saved, {len(to_analyze)} queued for auto-analysis. ──")

        # Apply any collected small created_at syncs in a single upsert to reduce DB requests
        if _cached_created_sync:
            try:
                from app.database import get_client
                client = get_client()
                if client:
                    logger.info(f"Batch-upserting {len(_cached_created_sync)} cached created_at values")
                    client.table('sessions').upsert(_cached_created_sync, on_conflict='session_id').execute()
            except Exception as e:
                logger.warning(f"Batch created_at sync failed: {e}")

        # Spawn one background thread per session to analyze (concurrency-safe via lock in analyzer)
        for sid, sess in to_analyze:
            t = threading.Thread(
                target=_auto_analyze_worker,
                args=(sid, sess),
                daemon=True,
            )
            t.start()

    except Exception as e:
        logger.error(f"Scrape job error: {e}", exc_info=True)
    finally:
        _is_running = False


def _get_scheduler_interval_minutes() -> float:
    raw_interval = os.getenv('CRON_INTERVAL_MINUTES')
    if raw_interval is None:
        raise ValueError('CRON_INTERVAL_MINUTES is not set.')

    try:
        interval = float(raw_interval)
    except ValueError as exc:
        raise ValueError(
            f"CRON_INTERVAL_MINUTES must be a positive number, got {raw_interval!r}."
        ) from exc

    if interval <= 0:
        raise ValueError(
            f"CRON_INTERVAL_MINUTES must be greater than 0, got {raw_interval!r}."
        )

    return interval


def start_scheduler():
    interval = _get_scheduler_interval_minutes()

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
