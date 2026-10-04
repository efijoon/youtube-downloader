"""ytdl command line: `login`, `logout`, `status`, `start`."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time

import typer

from .browser import LOGIN_URL, LOGOUT_URL, YT_HOME, BrowserSession, ProfileLockedError
from .config import get_settings

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help='YouTube 1080p downloader backed by a logged-in stealth Chrome profile.')


@app.command()
def login(timeout: int = typer.Option(600, help='Seconds to wait for you to finish signing in.')):
    """Open Chrome so you can sign in to Google; the session is saved to the profile."""
    asyncio.run(_login(timeout))


async def _login(timeout: int):
    settings = get_settings()
    session = BrowserSession(settings, headless=False)
    try:
        await session.start(LOGIN_URL)
    except ProfileLockedError as e:
        raise typer.Exit(_fail(str(e)))
    try:
        typer.echo('Sign in to the Google account in the Chrome window (2FA included).')
        typer.echo('This window closes automatically once YouTube sees you as signed in...')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if session.page.is_closed():
                raise typer.Exit(_fail('Browser window was closed before login completed.'))
            if await session.has_auth_cookies() and session.page.url.startswith(YT_HOME):
                break
            await asyncio.sleep(2)
        else:
            raise typer.Exit(_fail('Timed out waiting for login.'))

        await session.page.goto(YT_HOME, wait_until='domcontentloaded')
        await session.humanize()
        await session.ensure_logged_in()
        await session.export_cookies()
        account = await session.fetch_account() or {}
        typer.secho(f'Logged in as {account.get("email") or account.get("name") or "(unknown)"}. '
                    f'Profile saved to {settings.profile_dir}', fg='green')
    finally:
        await session.stop()


@app.command()
def logout(
    keep_profile: bool = typer.Option(False, '--keep-profile',
                                      help='Only sign out; keep the rest of the Chrome profile data.'),
):
    """Sign out of Google in the saved profile and delete the profile and exported cookies."""
    settings = get_settings()
    if not settings.profile_dir.exists():
        typer.echo('No saved profile; nothing to log out.')
        return

    async def run():
        session = BrowserSession(settings, headless=True)
        try:
            await session.start(LOGOUT_URL)
        except ProfileLockedError as e:
            raise typer.Exit(_fail(f'{e}. Stop the API first.'))
        try:
            # Google's logout endpoint ends the session server-side, so copies of
            # the cookies (e.g. data/cookies.txt) stop working too.
            await session.page.wait_for_timeout(3000)
            signed_out = not await session.has_auth_cookies()
        finally:
            await session.stop()
        return signed_out

    signed_out = asyncio.run(run())
    settings.cookies_file.unlink(missing_ok=True)
    if not keep_profile:
        shutil.rmtree(settings.profile_dir, ignore_errors=True)
    if signed_out:
        typer.secho('Signed out of Google.', fg='green')
    else:
        typer.secho('Google sign-out could not be confirmed; local session data was removed anyway.', fg='yellow')
    typer.echo('Kept the Chrome profile.' if keep_profile else f'Deleted {settings.profile_dir}.')


@app.command()
def status(headless: bool = typer.Option(True, help='Run the check without showing a window.')):
    """Show whether the saved profile is logged in."""
    async def run():
        session = BrowserSession(get_settings(), headless=headless)
        try:
            await session.start()
        except ProfileLockedError as e:
            raise typer.Exit(_fail(str(e)))
        try:
            await session.fetch_account()
            typer.echo(json.dumps(await session.status(), indent=2, ensure_ascii=False))
        finally:
            await session.stop()
    asyncio.run(run())


@app.command()
def start(
    host: str = typer.Option(None, help='Bind address (default from YTDL_HOST).'),
    port: int = typer.Option(None, help='Port (default from YTDL_PORT).'),
    headless: bool = typer.Option(None, '--headless/--no-headless', help='Override YTDL_HEADLESS.'),
):
    """Start the HTTP API using the saved browser profile."""
    import uvicorn

    settings = get_settings()
    if headless is not None:
        os.environ['YTDL_HEADLESS_OVERRIDE'] = '1' if headless else '0'
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    uvicorn.run('ytdl.api:app', host=host or settings.host, port=port or settings.port)


def _fail(msg: str) -> int:
    typer.secho(msg, fg='red', err=True)
    return 1


def main():
    app()
