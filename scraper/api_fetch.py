"""
API-based session fetcher — replaces the Playwright scraper.

Endpoints used:
  GET /admin-quotation-sessions-xzycode10          → list all sessions (metadata)
  GET /admin-quotation-sessions-xzycode10/{sid}    → full session (conversation, result, api_data)

Auth header: xi-apeit-key
All detail requests are fetched in parallel for speed.
"""

import os
import asyncio
import logging
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_BASE_URL = os.getenv(
    'SESSIONS_API_URL',
    'https://noneroded-unmanipulative-julia.ngrok-free.dev/admin-quotation-sessions-xzycode10',
).rstrip('/')
_API_KEY = os.getenv('xi-apeit-key', '')
_TIMEOUT = 30


def _headers() -> dict:
    return {
        'xi-apeit-key': _API_KEY,
        'ngrok-skip-browser-warning': '1',
        'Accept': 'application/json',
    }


def _services_list(services) -> list:
    """Normalise services to a list of strings regardless of source format."""
    if not services:
        return []
    if isinstance(services, list):
        return [str(s).strip() for s in services if s]
    if isinstance(services, str):
        return [s.strip() for s in services.split() if s.strip()]
    return []


def _normalize(item: dict, detail: dict | None) -> dict:
    """Build a normalized session dict from list metadata + optional full detail."""
    session_id = item['session_id']
    status = (item.get('status') or '').lower()
    msg_count = item.get('msg_count') or 0

    if detail is None:
        return {
            'session_id': session_id,
            'status': status,
            'msg_count': msg_count,
            'services': [],
            'conversation': [],
            'result_json': None,
            'reference_data': {},
            'scraped_at': datetime.now(timezone.utc).isoformat(),
            'created_at': item.get('created_at'),
            'is_cached': False,
            'fetch_error': 'detail fetch failed',
        }

    raw_conv = detail.get('conversation') or []
    conversation = [
        {
            'role': (msg.get('role') or 'unknown').strip(),
            'text': (msg.get('content') or msg.get('text') or '').strip(),
        }
        for msg in raw_conv
    ]

    detail_result = detail.get('result') or {}
    services = _services_list(
        detail_result.get('services') or (item.get('result') or {}).get('services')
    )

    logger.info(
        f"Fetched {session_id[:8]}… status={status} msgs={msg_count} services={services}"
    )

    return {
        'session_id': session_id,
        'status': status,
        'msg_count': msg_count,
        'services': services,
        'conversation': conversation,
        'result_json': detail_result or None,
        'reference_data': detail.get('api_data') or detail.get('reference_data') or {},
        'scraped_at': datetime.now(timezone.utc).isoformat(),
        'created_at': detail.get('created_at') or item.get('created_at'),
        'is_cached': False,
    }


def fetch_session_list() -> list[dict]:
    """Fetch the full list of sessions (metadata only) — single request."""
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.get(_BASE_URL, headers=_headers())
        resp.raise_for_status()
        data = resp.json()
    sessions = data.get('sessions') or data.get('data') or []
    logger.info(f"API returned {len(sessions)} sessions")
    return sessions


async def _fetch_detail_async(client: httpx.AsyncClient, session_id: str) -> dict | None:
    """Fetch one session's full detail asynchronously."""
    try:
        resp = await client.get(f'{_BASE_URL}/{session_id}', headers=_headers())
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Detail fetch failed for {session_id[:8]}: {e}")
        return None


async def _fetch_all_details_async(session_ids: list[str]) -> dict[str, dict | None]:
    """Fetch all session details in parallel. Returns {session_id: detail_or_None}."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        tasks = [_fetch_detail_async(client, sid) for sid in session_ids]
        results = await asyncio.gather(*tasks)
    return dict(zip(session_ids, results))


def fetch_new_sessions(cached_ids: set | None = None, cache_snapshot: dict | None = None) -> list[dict]:
    """
    Fetch all sessions from the API, returning normalized session dicts.

    Skip logic (no detail fetch needed):
    - completed + has data → always skip
    - active/unknown + msg_count unchanged since last fetch → skip (nothing new)
    - active + status just became completed → must re-fetch
    - brand new session_id → always fetch
    """
    if cached_ids is None:
        cached_ids = set()
    if cache_snapshot is None:
        cache_snapshot = {}

    session_list = fetch_session_list()

    cached_results = []
    to_fetch_items = []

    for item in session_list:
        session_id = item.get('session_id')
        if not session_id:
            continue

        api_status = (item.get('status') or '').lower()
        api_msg_count = item.get('msg_count') or 0

        # Completed + already have data → skip entirely
        # But if previous fetch had error or missing blobs, do NOT skip
        if session_id in cached_ids:
            snap = cache_snapshot.get(session_id, {})
            if not snap.get('has_data') or snap.get('fetch_error'):
                # Force re-fetch if missing data or had fetch error
                pass
            else:
                cached_results.append({
                    'session_id': session_id,
                    'is_cached': True,
                    'created_at': item.get('created_at'),
                })
                continue

        # Active session we've seen before — skip if nothing changed
        if session_id in cache_snapshot:
            snap = cache_snapshot[session_id]
            snap_status = snap.get('status', '')
            snap_msgs = snap.get('msg_count', 0)
            snap_has_data = snap.get('has_data', False)
            # Still active, same message count → nothing new
            if api_status != 'completed' and snap_status != 'completed' \
                    and api_msg_count == snap_msgs and snap_has_data:
                cached_results.append({
                    'session_id': session_id,
                    'is_cached': True,
                    'created_at': item.get('created_at'),
                })
                continue

        to_fetch_items.append(item)

    logger.info(
        f"{len(cached_results)} skipped (no change), "
        f"{len(to_fetch_items)} need detail fetch (parallel)"
    )

    # Fetch all uncached/changed session details in parallel
    new_results = []
    if to_fetch_items:
        ids_to_fetch = [item['session_id'] for item in to_fetch_items]
        details_map = asyncio.run(_fetch_all_details_async(ids_to_fetch))
        for item in to_fetch_items:
            detail = details_map.get(item['session_id'])
            new_results.append(_normalize(item, detail))

    # Preserve original API order (newest first)
    id_order = {item['session_id']: i for i, item in enumerate(session_list)}
    all_results = cached_results + new_results
    all_results.sort(key=lambda s: id_order.get(s['session_id'], 9999))

    # Mark sessions with fetch_error for retry on next fetch
    for s in all_results:
        if s.get('fetch_error') or (not s.get('conversation') and not s.get('result_json')):
            s['is_cached'] = False
    return all_results
