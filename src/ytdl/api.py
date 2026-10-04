"""HTTP API. Started via `ytdl start`."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyHeader

from . import potoken
from .browser import BrowserSession
from .config import get_settings
from .extractor import ExtractionError, Extractor, video_id_from_url
from .jobs import Job, JobManager
from .models import DownloadRequest, InfoRequest, JobOut

log = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    headless = os.environ.get('YTDL_HEADLESS_OVERRIDE')
    session = BrowserSession(settings, headless=None if headless is None else headless == '1')
    await session.start()
    try:
        await session.ensure_logged_in()
        await session.humanize()
        await session.export_cookies()
        await session.fetch_account()
        session.start_refresh_loop()
        potoken.set_session(session)

        extractor = Extractor(settings)
        app.state.session = session
        app.state.extractor = extractor
        app.state.jobs = JobManager(extractor, settings.max_concurrent_downloads)
        app.state.extract_sem = asyncio.Semaphore(settings.max_concurrent_extractions)
        account = (session.account or {}).get('email') or 'unknown account'
        log.info('ytdl ready as %s (headless=%s)', account, session.headless)
        yield
    finally:
        potoken.set_session(None)
        if jobs := getattr(app.state, 'jobs', None):
            jobs.shutdown()
        await session.stop()


app = FastAPI(title='ytdl', version='0.1.0', lifespan=lifespan)
api_key_header = APIKeyHeader(name='X-API-Key', auto_error=False)


def require_key(request: Request, key: str | None = Security(api_key_header)):
    if settings.api_key:
        if not key or not secrets.compare_digest(key, settings.api_key):
            raise HTTPException(401, 'Invalid or missing X-API-Key')
    elif request.client and request.client.host not in ('127.0.0.1', '::1', 'localhost'):
        raise HTTPException(401, 'Set YTDL_API_KEY to allow non-local clients')


@app.exception_handler(ExtractionError)
async def extraction_error_handler(_: Request, exc: ExtractionError):
    return JSONResponse(status_code=exc.status, content={'detail': exc.message})


def _job_out(job: Job) -> JobOut:
    return JobOut(
        id=job.id, url=job.url, height=job.height, status=job.status,
        progress=job.progress, error=job.error,
        file_url=f'/files/{job.id}' if job.status == 'done' else None,
    )


@app.get('/health')
async def health(request: Request):
    return {'ok': True, **await request.app.state.session.status()}


@app.post('/info', dependencies=[Depends(require_key)])
async def info(body: InfoRequest, request: Request):
    state = request.app.state
    async with state.extract_sem:
        return await asyncio.to_thread(state.extractor.get_info, body.url, body.max_height)


@app.post('/download', dependencies=[Depends(require_key)], response_model=JobOut, status_code=202)
async def download(body: DownloadRequest, request: Request):
    video_id_from_url(body.url)  # validate before queueing
    return _job_out(request.app.state.jobs.submit(body.url, body.height))


@app.get('/jobs/{job_id}', dependencies=[Depends(require_key)], response_model=JobOut)
async def job_status(job_id: str, request: Request):
    job = request.app.state.jobs.get(job_id)
    if not job:
        raise HTTPException(404, 'Unknown job')
    return _job_out(job)


@app.get('/files/{job_id}', dependencies=[Depends(require_key)])
async def job_file(job_id: str, request: Request):
    job = request.app.state.jobs.get(job_id)
    if not job:
        raise HTTPException(404, 'Unknown job')
    if job.status != 'done' or not job.file_path or not os.path.exists(job.file_path):
        raise HTTPException(409, f'File not ready (status: {job.status})')
    return FileResponse(job.file_path, media_type='video/mp4', filename=os.path.basename(job.file_path))
