"""Stealth Chrome session bound to a persistent, logged-in Google profile.

The browser is the identity layer: it owns the profile, keeps Google's rotating
cookies fresh, and mints PO tokens with BotGuard running in a real Chrome.
"""
from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import random
import time
from importlib import resources
from pathlib import Path

from patchright.async_api import BrowserContext, Page, async_playwright

from .config import Settings

log = logging.getLogger(__name__)

YT_HOME = 'https://www.youtube.com/'
LOGIN_URL = 'https://accounts.google.com/ServiceLogin?service=youtube&continue=https%3A%2F%2Fwww.youtube.com%2F'
LOGOUT_URL = 'https://accounts.google.com/Logout?continue=https%3A%2F%2Fwww.youtube.com%2F'
COOKIE_URLS = ['https://www.youtube.com', 'https://accounts.google.com', 'https://www.google.com']
AUTH_COOKIES = {'SAPISID', '__Secure-3PAPISID'}

# Runs in the page's main world, where YouTube's `ytcfg` lives.
_YTCFG_JS = """() => {
  const c = window.ytcfg;
  if (!c || !c.get) return null;
  return {
    loggedIn: !!c.get('LOGGED_IN'),
    dataSyncId: c.get('DATASYNC_ID') || null,
    visitorData: c.get('VISITOR_DATA') || null,
    sessionIndex: c.get('SESSION_INDEX') ?? null,
  };
}"""

# Asks InnerTube for the account menu to find out which account is signed in.
_ACCOUNT_JS = """async () => {
  const sapisid = (document.cookie.match(/(?:^|; )(?:__Secure-3PAPISID|SAPISID)=([^;]+)/) || [])[1];
  if (!sapisid || !window.ytcfg) return null;
  const ts = Math.floor(Date.now() / 1000);
  const digest = await crypto.subtle.digest('SHA-1', new TextEncoder().encode(`${ts} ${sapisid} https://www.youtube.com`));
  const hash = [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
  const res = await fetch('/youtubei/v1/account/account_menu?prettyPrint=false', {
    method: 'POST',
    credentials: 'include',
    headers: {
      'content-type': 'application/json',
      'authorization': `SAPISIDHASH ${ts}_${hash}`,
      'x-origin': 'https://www.youtube.com',
      'x-goog-authuser': String(ytcfg.get('SESSION_INDEX') ?? 0),
    },
    body: JSON.stringify({ context: ytcfg.get('INNERTUBE_CONTEXT') }),
  });
  if (!res.ok) return null;
  const text = JSON.stringify(await res.json());
  const email = (text.match(/[\\w.+-]+@[\\w-]+\\.[\\w.]+/) || [])[0] || null;
  const name = (text.match(/"accountName":\\{"simpleText":"([^"]+)"/) || [])[1] || null;
  return { email, name };
}"""


class ProfileLockedError(RuntimeError):
    pass


class NotLoggedInError(RuntimeError):
    pass


class ProfileLock:
    """Only one Chrome process may own a user-data-dir at a time."""

    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def acquire(self):
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise ProfileLockedError(
                f'Browser profile is in use by another ytdl process (lock: {self.path})')
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd

    def release(self):
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


class BrowserSession:
    def __init__(self, settings: Settings, headless: bool | None = None):
        self.settings = settings
        self.headless = settings.headless if headless is None else headless
        self.loop: asyncio.AbstractEventLoop | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.account: dict | None = None
        self.cookies_exported_at: float | None = None
        self._pw = None
        self._lock = ProfileLock(settings.lock_file)
        self._page_lock = asyncio.Lock()
        self._refresh_task: asyncio.Task | None = None
        self._minter_js = resources.files('ytdl').joinpath('assets/minter.js').read_text()

    # --- lifecycle -----------------------------------------------------------

    async def start(self, url: str = YT_HOME):
        self.loop = asyncio.get_running_loop()
        self._lock.acquire()
        try:
            self._pw = await async_playwright().start()
            # Patchright stealth guidance: real Chrome, persistent profile, no
            # viewport emulation, no custom UA/headers, no extra stealth plugins.
            self.context = await self._pw.chromium.launch_persistent_context(
                user_data_dir=str(self.settings.profile_dir),
                channel='chrome',
                headless=self.headless,
                no_viewport=True,
            )
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            await self.page.goto(url, wait_until='domcontentloaded')
        except BaseException:
            await self.stop()
            raise

    async def stop(self):
        if self._refresh_task:
            self._refresh_task.cancel()
            self._refresh_task = None
        try:
            if self.context:
                await self.context.close()
            if self._pw:
                await self._pw.stop()
        finally:
            self.context = self.page = self._pw = None
            self._lock.release()

    def start_refresh_loop(self):
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def _refresh_loop(self):
        interval = self.settings.cookie_refresh_minutes * 60
        while True:
            await asyncio.sleep(interval * random.uniform(0.85, 1.15))
            try:
                await self.refresh()
            except Exception:
                log.exception('Cookie refresh failed')

    # --- session state -------------------------------------------------------

    async def has_auth_cookies(self) -> bool:
        cookies = await self.context.cookies(['https://www.youtube.com'])
        return any(c['name'] in AUTH_COOKIES for c in cookies)

    async def ytcfg(self) -> dict | None:
        try:
            return await self.page.evaluate(_YTCFG_JS, isolated_context=False)
        except Exception:
            return None

    async def is_logged_in(self) -> bool:
        if not await self.has_auth_cookies():
            return False
        if not self.page.url.startswith(YT_HOME):
            return True  # cookies are enough until we are back on youtube.com
        cfg = await self.ytcfg()
        return bool(cfg and cfg['loggedIn'])

    async def fetch_account(self) -> dict | None:
        try:
            self.account = await self.page.evaluate(_ACCOUNT_JS, isolated_context=False)
        except Exception:
            log.debug('Could not read account info', exc_info=True)
        return self.account

    async def ensure_logged_in(self):
        if not await self.is_logged_in():
            raise NotLoggedInError('Browser profile is not logged in to YouTube. Run `ytdl login` first.')

    async def refresh(self):
        """Revisit YouTube like a person would so Google rotates cookies inside
        the real browser, then hand yt-dlp a fresh copy."""
        async with self._page_lock:
            await self.page.goto(YT_HOME, wait_until='domcontentloaded')
            await self.humanize()
        await self.ensure_logged_in()
        await self.export_cookies()

    async def humanize(self):
        page = self.page
        await page.wait_for_timeout(random.randint(1200, 2500))
        size = await page.evaluate('() => [innerWidth, innerHeight]')
        for _ in range(random.randint(2, 4)):
            await page.mouse.move(random.randint(50, size[0] - 50), random.randint(80, size[1] - 50),
                                  steps=random.randint(8, 20))
            await page.wait_for_timeout(random.randint(200, 700))
        await page.mouse.wheel(0, random.randint(300, 900))
        await page.wait_for_timeout(random.randint(800, 1600))

    async def export_cookies(self) -> Path:
        cookies = await self.context.cookies(COOKIE_URLS)
        lines = ['# Netscape HTTP Cookie File', '# Exported by ytdl from the live browser profile', '']
        for c in cookies:
            domain = c['domain']
            lines.append('\t'.join([
                domain,
                'TRUE' if domain.startswith('.') else 'FALSE',
                c['path'],
                'TRUE' if c['secure'] else 'FALSE',
                str(int(c['expires'])) if c['expires'] and c['expires'] > 0 else '0',
                c['name'],
                c['value'],
            ]))
        target = self.settings.cookies_file
        tmp = target.with_suffix('.tmp')
        tmp.write_text('\n'.join(lines) + '\n')
        os.chmod(tmp, 0o600)
        tmp.replace(target)
        self.cookies_exported_at = time.time()
        return target

    # --- PO tokens -----------------------------------------------------------

    async def mint_po_token(self, content_binding: str) -> tuple[str, int]:
        """Mint a WebPO token with BotGuard inside the logged-in youtube.com tab.

        Runs in an isolated world: it shares the page's origin and cookies but is
        invisible to YouTube's own scripts and not subject to the page's CSP.
        """
        async with self._page_lock:
            if not self.page.url.startswith(YT_HOME):
                await self.page.goto(YT_HOME, wait_until='domcontentloaded')
            result = await self.page.evaluate(
                """async ([src, binding]) => {
                  if (!globalThis.__ytdlMinter) (0, eval)(src);
                  return await globalThis.__ytdlMinter.mint(binding);
                }""",
                [self._minter_js, content_binding],
            )
        return result['token'], int(result['expiresAt'])

    def mint_po_token_sync(self, content_binding: str, timeout: float = 60) -> tuple[str, int]:
        """Called from yt-dlp worker threads."""
        if not self.loop or not self.context:
            raise RuntimeError('Browser session is not running')
        fut = asyncio.run_coroutine_threadsafe(self.mint_po_token(content_binding), self.loop)
        return fut.result(timeout)

    async def status(self) -> dict:
        cfg = await self.ytcfg() or {}
        return {
            'logged_in': await self.is_logged_in(),
            'account': self.account,
            'data_sync_id': cfg.get('dataSyncId'),
            'cookies_age_s': round(time.time() - self.cookies_exported_at) if self.cookies_exported_at else None,
            'headless': self.headless,
        }
