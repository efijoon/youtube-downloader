"""yt-dlp wrapper: format extraction and merged 1080p downloads.

Runs synchronously; callers execute it in worker threads.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from urllib.parse import parse_qs, urlparse

import yt_dlp
from yt_dlp.extractor.youtube import YoutubeIE
from yt_dlp.utils import DownloadError

from . import potoken  # noqa: F401  (registers the browser PO token provider)
from .config import Settings

URL_NOTE = ('Stream URLs are bound to the IP address of this server and expire '
            '(see expires_at). 1080p video and audio are separate streams; '
            'download both and mux them, or use POST /download.')


class ExtractionError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def video_id_from_url(url: str) -> str:
    if not YoutubeIE.suitable(url):
        raise ExtractionError(400, f'Not a YouTube video URL: {url}')
    return YoutubeIE.get_temp_id(url) or YoutubeIE._match_id(url)


def _classify(err: DownloadError) -> ExtractionError:
    msg = str(err).removeprefix('ERROR: ')
    lower = msg.lower()
    if 'sign in to confirm' in lower or 'not a bot' in lower:
        return ExtractionError(503, f'YouTube is challenging this session; re-run `ytdl login` or use headed mode. ({msg})')
    if 'age' in lower and 'restrict' in lower:
        return ExtractionError(451, msg)
    if any(s in lower for s in ('private video', 'members-only', 'join this channel', 'video unavailable', 'removed')):
        return ExtractionError(403, msg)
    return ExtractionError(502, msg)


class Extractor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._cache: dict[str, tuple[float, dict]] = {}
        self._cache_lock = threading.Lock()

    @contextmanager
    def _ydl(self, extra: dict | None = None):
        # yt-dlp writes cookies back on close. Give it a private copy so it
        # never clobbers the file the browser keeps fresh.
        fd, cookie_copy = tempfile.mkstemp(prefix='ytdl-cookies-', suffix='.txt')
        os.close(fd)
        shutil.copyfile(self.settings.cookies_file, cookie_copy)
        params = {
            'cookiefile': cookie_copy,
            'js_runtimes': {self.settings.js_runtime: {}},
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'noprogress': True,
            **(extra or {}),
        }
        try:
            with yt_dlp.YoutubeDL(params) as ydl:
                yield ydl
        finally:
            os.unlink(cookie_copy)

    # --- info ----------------------------------------------------------------

    def get_info(self, url: str, max_height: int = 1080) -> dict:
        vid = video_id_from_url(url)
        key = f'{vid}:{max_height}'
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < self.settings.info_cache_seconds:
                return hit[1]

        try:
            with self._ydl({'skip_download': True}) as ydl:
                info = ydl.sanitize_info(ydl.extract_info(f'https://www.youtube.com/watch?v={vid}', download=False))
        except DownloadError as e:
            raise _classify(e) from e

        result = normalize(info, max_height)
        with self._cache_lock:
            self._cache[key] = (time.time(), result)
        return result

    # --- download ------------------------------------------------------------

    def download_merged(self, url: str, height: int = 1080,
                        on_progress: Callable[[float | None], None] | None = None) -> str:
        vid = video_id_from_url(url)
        out_dir = self.settings.download_dir

        def hook(d):
            if on_progress and d.get('status') == 'downloading':
                total = d.get('total_bytes') or d.get('total_bytes_estimate')
                on_progress(d['downloaded_bytes'] / total if total else None)

        params = {
            'format': (f'bv*[height<={height}][vcodec^=avc1]+ba[ext=m4a]/'
                       f'bv*[height<={height}]+ba/b[height<={height}]'),
            'format_sort': [f'res:{height}', 'vcodec:avc1', 'acodec:m4a'],
            'merge_output_format': 'mp4',
            'outtmpl': str(out_dir / f'%(id)s_{height}p.%(ext)s'),
            'paths': {'temp': str(out_dir / '.tmp')},
            'progress_hooks': [hook],
            'overwrites': False,
        }
        try:
            with self._ydl(params) as ydl:
                info = ydl.extract_info(f'https://www.youtube.com/watch?v={vid}', download=True)
        except DownloadError as e:
            raise _classify(e) from e
        downloads = info.get('requested_downloads') or []
        path = downloads[0].get('filepath') if downloads else None
        if not path or not os.path.exists(path):
            raise ExtractionError(502, 'yt-dlp finished without producing a file')
        return path


# --- normalization -----------------------------------------------------------

def _expires_at(url: str | None) -> int | None:
    if not url:
        return None
    qs = parse_qs(urlparse(url).query)
    exp = qs.get('expire')
    return int(exp[0]) if exp and exp[0].isdigit() else None


def _kind(f: dict) -> str:
    has_v = f.get('vcodec') not in (None, 'none')
    has_a = f.get('acodec') not in (None, 'none')
    if has_v and has_a:
        return 'muxed'
    return 'video' if has_v else 'audio'


def _format_entry(f: dict) -> dict:
    return {
        'format_id': f['format_id'],
        'kind': _kind(f),
        'quality': f'{f["height"]}p' if f.get('height') else f.get('format_note'),
        'height': f.get('height'),
        'width': f.get('width'),
        'fps': f.get('fps'),
        'ext': f.get('ext'),
        'vcodec': None if f.get('vcodec') == 'none' else f.get('vcodec'),
        'acodec': None if f.get('acodec') == 'none' else f.get('acodec'),
        'abr': f.get('abr'),
        'tbr': f.get('tbr'),
        'filesize': f.get('filesize') or f.get('filesize_approx'),
        'protocol': f.get('protocol'),
        'url': f.get('url'),
        'http_headers': f.get('http_headers') or {},
        'expires_at': _expires_at(f.get('url')),
    }


def _usable(f: dict) -> bool:
    # Drop storyboards, SABR-only entries without a URL, and DRM formats.
    return bool(f.get('url')) and f.get('ext') != 'mhtml' and not f.get('has_drm')


def pick_best_pair(formats: list[dict], max_height: int) -> dict | None:
    https = [f for f in formats if f['protocol'] == 'https']
    videos = [f for f in https if f['kind'] == 'video' and (f['height'] or 0) <= max_height]
    audios = [f for f in https if f['kind'] == 'audio']
    if not videos or not audios:
        return None
    video = max(videos, key=lambda f: (f['height'] or 0, (f['vcodec'] or '').startswith('avc1'),
                                       f['fps'] or 0, f['tbr'] or 0))
    # mp4 video pairs with m4a audio; webm/vp9 pairs with opus.
    want_ext = 'm4a' if video['ext'] == 'mp4' else 'webm'
    audio = max(audios, key=lambda f: (f['ext'] == want_ext, f['abr'] or f['tbr'] or 0))
    return {'quality': video['quality'], 'video': video, 'audio': audio}


def normalize(info: dict, max_height: int) -> dict:
    formats = [_format_entry(f) for f in info.get('formats') or [] if _usable(f)]
    formats = [f for f in formats if f['kind'] == 'audio' or (f['height'] or 0) <= max_height]
    formats.sort(key=lambda f: (f['kind'], -(f['height'] or 0), -(f['tbr'] or 0)))
    return {
        'video': {
            'id': info.get('id'),
            'title': info.get('title'),
            'channel': info.get('channel') or info.get('uploader'),
            'channel_id': info.get('channel_id'),
            'duration': info.get('duration'),
            'thumbnail': info.get('thumbnail'),
            'upload_date': info.get('upload_date'),
            'view_count': info.get('view_count'),
            'is_live': info.get('is_live'),
            'age_limit': info.get('age_limit'),
        },
        'best': pick_best_pair(formats, max_height),
        'qualities': sorted({f['height'] for f in formats if f['height']}, reverse=True),
        'formats': formats,
        'note': URL_NOTE,
    }
