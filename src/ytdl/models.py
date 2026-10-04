from pydantic import BaseModel, Field


class InfoRequest(BaseModel):
    url: str
    max_height: int = Field(1080, ge=144, le=4320)


class DownloadRequest(BaseModel):
    url: str
    height: int = Field(1080, ge=144, le=4320)


class JobOut(BaseModel):
    id: str
    url: str
    height: int
    status: str
    progress: float | None
    error: str | None = None
    file_url: str | None = None
