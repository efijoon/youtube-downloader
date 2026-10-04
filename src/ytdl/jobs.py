"""In-memory registry for merged-download jobs."""
from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .extractor import ExtractionError, Extractor


@dataclass
class Job:
    id: str
    url: str
    height: int
    status: str = 'queued'  # queued | running | done | error
    progress: float | None = None
    file_path: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None


class JobManager:
    def __init__(self, extractor: Extractor, workers: int):
        self.extractor = extractor
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='ytdl-dl')

    def submit(self, url: str, height: int) -> Job:
        job = Job(id=uuid.uuid4().hex, url=url, height=height)
        with self._lock:
            self.jobs[job.id] = job
        self._pool.submit(self._run, job)
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def _run(self, job: Job):
        job.status = 'running'

        def on_progress(p):
            job.progress = None if p is None else round(min(p, 1.0), 4)

        try:
            job.file_path = self.extractor.download_merged(job.url, job.height, on_progress)
            job.progress = 1.0
            job.status = 'done'
        except ExtractionError as e:
            job.status, job.error = 'error', e.message
        except Exception as e:
            job.status, job.error = 'error', f'{type(e).__name__}: {e}'
        finally:
            job.finished_at = time.time()

    def shutdown(self):
        self._pool.shutdown(wait=False, cancel_futures=True)
