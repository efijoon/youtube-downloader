# ytdl

A YouTube 1080p downloader API. A stealth Chrome (patchright) holds a persistent,
logged-in Google profile. It keeps the cookies fresh and mints PO tokens with
BotGuard running in the real browser. yt-dlp does the format extraction and the merging.

## Setup
```bash
uv sync
uv run patchright install chrome   # only if Google Chrome isn't installed
cp .env.example .env               # set YTDL_API_KEY if exposing beyond localhost
```
Requires `ffmpeg` and a JS runtime for yt-dlp (`bun` by default; set `YTDL_JS_RUNTIME`).

## Usage
```bash
uv run ytdl login                  # sign in once in the Chrome window (2FA ok)
uv run ytdl status                 # check the saved profile
uv run ytdl logout                 # sign out of Google and delete the profile (--keep-profile to keep it)
uv run ytdl start [--no-headless] [--host 0.0.0.0 --port 8000]
```
Use a secondary Google account. If extraction starts failing in headless mode,
run with `--no-headless`.

## API
All endpoints except `/health` take `X-API-Key` when `YTDL_API_KEY` is set.

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/health` | | login state, account, cookie age |
| POST | `/info` | `{"url", "max_height": 1080}` | video details, `best` (1080p video+audio pair), `qualities`, all `formats` with `url`, `quality`, `expires_at` |
| POST | `/download` | `{"url", "height": 1080}` | job (202) |
| GET | `/jobs/{id}` | | `status`, `progress`, `file_url` |
| GET | `/files/{id}` | | merged `.mp4` |

1080p is served as separate video-only and audio-only streams. Stream URLs only
work from this server's IP and expire after about 6h. Callers on other machines
should use `/download` + `/files`.

```python
import requests, time
H = {'X-API-Key': '...'}
info = requests.post('http://127.0.0.1:8000/info', json={'url': 'https://youtu.be/<id>'}, headers=H).json()
print(info['best']['video']['quality'], info['best']['video']['url'])

job = requests.post('http://127.0.0.1:8000/download', json={'url': 'https://youtu.be/<id>'}, headers=H).json()
while (j := requests.get(f"http://127.0.0.1:8000/jobs/{job['id']}", headers=H).json())['status'] not in ('done', 'error'):
    time.sleep(2)
open('video.mp4', 'wb').write(requests.get(f"http://127.0.0.1:8000{j['file_url']}", headers=H).content)
```

## Layout
- `src/ytdl/browser.py`: patchright session, profile lock, cookie export and refresh, in-browser PO token minting
- `src/ytdl/potoken.py`: yt-dlp PO token provider that calls the browser (the bgutil plugin is installed as a fallback)
- `src/ytdl/extractor.py`: yt-dlp wrapper, format normalization, merged downloads
- `src/ytdl/api.py`, `jobs.py`, `cli.py`: API, download jobs, CLI
- `minter/`: source of `src/ytdl/assets/minter.js` (`cd minter && bun install && bun run build`)
