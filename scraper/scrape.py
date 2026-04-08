import os
import sys
import json
import asyncio
import logging
from datetime import datetime, timezone
from playwright.async_api import async_playwright, Page, BrowserContext
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

# On Linux (Render), force browsers to live inside the project so they survive
# between build and runtime. On Windows (local dev), use the default venv path.
if sys.platform != 'win32' and 'PLAYWRIGHT_BROWSERS_PATH' not in os.environ:
    _BROWSERS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'ms-playwright')
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = _BROWSERS_PATH

logger = logging.getLogger(__name__)

SITE_API_KEY = os.getenv('SITE_API_KEY', '')
TARGET_URL = os.getenv(
    'TARGET_URL'
)


def parse_reference_html(html: str) -> dict:
    """Parse reference data HTML tables into structured dict."""
    soup = BeautifulSoup(html, 'html.parser')
    result = {}
    for section in soup.find_all('div', class_='qs-ref-section'):
        title_el = section.find('div', class_='qs-ref-title')
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        table = section.find('table')
        if table:
            tbody = table.find('tbody')
            if not tbody:
                continue
            headers = [th.get_text(strip=True) for th in table.find_all('th')]
            rows = []
            for tr in tbody.find_all('tr'):
                cells = [td.get_text(strip=True) for td in tr.find_all('td')]
                if cells:
                    rows.append(dict(zip(headers, cells)) if headers else cells)
            result[title] = rows
        else:
            badges = section.find_all('span', class_='qs-ref-badge')
            if badges:
                result[title] = [b.get_text(strip=True) for b in badges]
    return result


async def _navigate_to_sessions(context: BrowserContext) -> Page:
    """Create a fresh page, authenticate, and navigate to the Quotation Sessions list."""
    page = await context.new_page()
    await page.goto(TARGET_URL, wait_until='domcontentloaded', timeout=60000)
    await page.wait_for_selector('#apiKeyInput', timeout=30000)
    await page.fill('#apiKeyInput', SITE_API_KEY)
    await page.keyboard.press('Enter')
    await page.wait_for_selector('button.tab[data-tab="quotation-sessions"]', timeout=30000)
    await page.click('button.tab[data-tab="quotation-sessions"]')
    await page.wait_for_timeout(800)
    await page.click('#qsRefresh')
    await page.wait_for_selector('#qsTableBody tr', timeout=20000)
    await page.wait_for_timeout(800)
    return page


async def _is_page_alive(page: Page) -> bool:
    """Check whether the page is still usable."""
    try:
        await page.evaluate('() => document.title')
        return True
    except Exception:
        return False


async def _close_modal(page: Page):
    """Close the open modal by clicking its X button and waiting for it to hide."""
    try:
        await page.locator('button.modal-close').first.click(timeout=3000)
        await page.locator('#qsConvPane').wait_for(state='hidden', timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(400)


async def scrape_sessions(cached_ids: set = None) -> list:
    """
    Scrape first 10 quotation sessions.
    Sessions already in cached_ids are returned with is_cached=True.
    """
    if cached_ids is None:
        cached_ids = set()

    sessions = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            extra_http_headers={'ngrok-skip-browser-warning': '1'}
        )

        try:
            logger.info("Navigating to admin panel…")
            page = await _navigate_to_sessions(context)
            logger.info("Auth successful")

            # ── Collect basic row info (first 10) ─────────────────────────────
            rows = await page.query_selector_all('#qsTableBody tr')
            rows = rows[:10]

            row_data = []
            for row in rows:
                try:
                    sid_el = await row.query_selector('.session-id')
                    if not sid_el:
                        continue
                    session_id = await sid_el.get_attribute('title')

                    status_el = await row.query_selector('.status-badge')
                    status = (await status_el.inner_text()).strip() if status_el else 'unknown'

                    msg_count_el = await row.query_selector('.msg-count')
                    msg_count_raw = (await msg_count_el.inner_text()).strip() if msg_count_el else '0'
                    msg_count = int(msg_count_raw) if msg_count_raw.isdigit() else 0

                    svc_els = await row.query_selector_all('td:nth-child(3) span[title]')
                    services = []
                    for el in svc_els:
                        t = await el.get_attribute('title')
                        if t:
                            services.append(t)

                    row_data.append({
                        'session_id': session_id,
                        'status': status,
                        'msg_count': msg_count,
                        'services': services,
                    })
                except Exception as e:
                    logger.warning(f"Error reading row: {e}")
                    continue

            logger.info(
                f"Found {len(row_data)} rows. "
                f"{sum(1 for r in row_data if r['session_id'] in cached_ids)} already cached."
            )

            # ── Click each uncached session modal ──────────────────────────────
            for rd in row_data:
                session_id = rd['session_id']

                if session_id in cached_ids:
                    sessions.append({'session_id': session_id, 'is_cached': True})
                    continue

                # Recover dead page before attempting each session
                if not await _is_page_alive(page):
                    logger.warning("Page is dead — recreating…")
                    try:
                        await page.close()
                    except Exception:
                        pass
                    try:
                        page = await _navigate_to_sessions(context)
                    except Exception as e:
                        logger.error(f"Could not recover page: {e}")
                        sessions.append({**rd, 'is_cached': False, 'scrape_error': str(e),
                                         'scraped_at': datetime.now(timezone.utc).isoformat()})
                        continue

                try:
                    # Use JS click — avoids selector timing issues
                    clicked = await page.evaluate(f'''() => {{
                        const spans = document.querySelectorAll('#qsTableBody .session-id');
                        for (const span of spans) {{
                            if (span.getAttribute('title') === '{session_id}') {{
                                span.closest('tr').click();
                                return true;
                            }}
                        }}
                        return false;
                    }}''')

                    if not clicked:
                        raise Exception(f"Row not found in DOM for {session_id}")

                    # Wait for modal to open AND confirm correct session content is loaded
                    await page.wait_for_selector('#qsConvPane', state='visible', timeout=8000)
                    # Wait for modal info to show this specific session ID
                    await page.wait_for_function(
                        f'''() => {{
                            const el = document.querySelector('#qsModalInfo .modal-info-value');
                            return el && el.textContent.trim() === '{session_id}';
                        }}''',
                        timeout=8000
                    )
                    await page.wait_for_timeout(300)

                    # ── Conversation ─────────────────────────────────────────
                    conversation = await page.evaluate('''() => {
                        const msgs = [];
                        document.querySelectorAll('#qsConvPane .message-item').forEach(msg => {
                            const roleEl = msg.querySelector('.message-role');
                            const textEl = msg.querySelector('.message-text');
                            const role = roleEl
                                ? roleEl.innerText.replace(/[🤖👤]/g, '').trim()
                                : 'Unknown';
                            const text = textEl ? textEl.innerText.trim() : '';
                            msgs.push({ role, text });
                        });
                        return msgs;
                    }''')

                    # ── Result JSON ──────────────────────────────────────────
                    result_json = None
                    try:
                        await page.locator('#qsTabResult').click()
                        # Wait for result JSON content to be populated
                        await page.wait_for_function(
                            "() => (document.getElementById('qsResultJson')?.innerText || '').trim().startsWith('{')",
                            timeout=5000
                        )
                        raw = await page.locator('#qsResultJson').inner_text(timeout=3000)
                        if raw and raw.strip():
                            result_json = json.loads(raw.strip())
                    except Exception as e:
                        logger.warning(f"Result JSON error for {session_id[:8]}: {e}")

                    # ── Reference Data ───────────────────────────────────────
                    reference_data = {}
                    try:
                        await page.locator('#qsTabApiData').click()
                        await page.wait_for_timeout(400)
                        ref_html = await page.locator('#qsApiDataPane').inner_html(timeout=3000)
                        reference_data = parse_reference_html(ref_html)
                    except Exception as e:
                        logger.warning(f"Reference data error for {session_id[:8]}: {e}")

                    # ── Close modal (X button, not Escape) ───────────────────
                    await _close_modal(page)

                    sessions.append({
                        **rd,
                        'is_cached': False,
                        'conversation': conversation,
                        'result_json': result_json,
                        'reference_data': reference_data,
                        'scraped_at': datetime.now(timezone.utc).isoformat(),
                    })
                    logger.info(f"Scraped session {session_id[:8]}…")

                except Exception as e:
                    logger.error(f"Error scraping {session_id[:8]}: {e}")
                    try:
                        await _close_modal(page)
                    except Exception:
                        pass
                    sessions.append({
                        **rd,
                        'is_cached': False,
                        'scrape_error': str(e),
                        'scraped_at': datetime.now(timezone.utc).isoformat(),
                    })

        except Exception as e:
            logger.error(f"Fatal scrape error: {e}", exc_info=True)
            raise
        finally:
            await browser.close()

    return sessions


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    result = asyncio.run(scrape_sessions())
    print(json.dumps(result, indent=2, default=str))

