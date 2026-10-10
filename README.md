# webui-dl

A small self-hosted web UI for downloading from the web with gallery-dl and yt-dlp, in the spirit of gallery-dl-server, with:

- **Metadata preview before download.** Paste a link and it reads the metadata (without downloading) and shows every field that will be used, plus the final file name for yt-dlp.
- **Metadata overrides.** Edit any field or add new ones (title, artist, album, tags, date…). Overrides apply to every item in a gallery or playlist.
  - yt-dlp: overrides go into the info dict before the file name is built and before `--embed-metadata` runs, so they change both the file name and the embedded tags.
  - gallery-dl: overrides are passed as `keywords`, so they appear in the sidecar `.json` and in any `{field}` used by your filename/directory formats.
- **Cookies from the UI.** Paste or upload a Netscape `cookies.txt` or a JSON export (Cookie-Editor style, converted automatically). All saved cookie files are used automatically.
- **Engine versions from the UI.** Each engine lives in its own virtualenv under `/config/venvs`. Update to latest, pin any PyPI version, or install nightly/master. No container rebuild needed.
- Queue with live progress, cancel and retry, and a timestamped activity log (kept in `/config/logs`) like gallery-dl-server's.

## Install

Create a `docker-compose.yml` anywhere you like with just this:

```yaml
services:
  webui-dl:
    image: ghcr.io/nathanapples/webui-dl:latest
    container_name: webui-dl
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=America/Denver
    volumes:
      - /apps/webui-dl:/config
      - /path/to/downloads:/downloads
    ports:
      - 7341:7341
    restart: unless-stopped
```

Then `docker compose up -d` and open `http://<host>:7341`. On first start gallery-dl and yt-dlp are installed into the config folder (about a minute; progress shows in the Engines tab).

| Variable | Default | Purpose |
|---|---|---|
| `PUID` / `PGID` | 1000 | User and group the app runs as; downloads are owned by them |
| `TZ` | Etc/UTC | Time zone |
| `UMASK` | (system) | Optional, e.g. `002` for group-writable files |
| `PORT` | 7341 | Port the app listens on inside the container |
| `MAX_CONCURRENT` | 2 | Downloads that run at once |
| `APP_USER` / `APP_PASSWORD` | unset | Set a password to require a login |

Everything the app keeps (engines, cookies, settings, job history) lives in the `/config` folder. Make sure the downloads folder is writable by `PUID`.

To update the app: `docker compose pull && docker compose up -d`. gallery-dl and yt-dlp are updated from the Engines tab, no image update needed.

## Development

The GitHub Action in `.github/workflows/docker.yml` builds and publishes `ghcr.io/nathanapples/webui-dl` (amd64 and arm64) on every push to `main`.

Run without Docker:

```bash
pip install -r requirements.txt
CONFIG_DIR=./config DOWNLOAD_DIR=./downloads uvicorn app.main:app --port 7341
```

## Profiles

A profile bundles settings for a site and is picked automatically when a link matches it. Each profile can set:

- **Sites**: domains such as `example.com` (subdomains included), or `re:` followed by a regex for the whole link
- **Engine**
- **Info fields** and **User field**: which fields the Metadata panel lists, and which one the "user" bubble shows
- **Metadata**: `field = value` lines; `{field}` or `{a|b}` pull from the link's own metadata
- **Write tags into video/audio files** for gallery-dl downloads (ffmpeg), as `tag = template` lines
- **gallery-dl options**: any JSON in the same format as `gallery-dl.conf`
- **yt-dlp** extra arguments and output template

Manage them under Settings > Profiles. **Import file** accepts a profiles export, a plain list of profiles, or a whole `gallery-dl.conf`. **Export** downloads everything as one JSON file you can edit and import again.

Profiles, cookies and your gallery-dl config are stored only in the `/config` folder (owner-only permissions). They are never part of the image or this repository.

## Settings

Cookies: every saved cookie file is used for every download (each site only receives its own cookies), so there is nothing to select.

Everything is under the single **Settings** button: Profiles, Cookies, Engines (update or pin gallery-dl / yt-dlp), gallery-dl config (your global `gallery-dl.conf`), and General (output template, default arguments, preview limit).

Engine **Auto** tries gallery-dl first, then yt-dlp. When gallery-dl hands a link off to yt-dlp, it uses the app's yt-dlp, so both stay on the version you choose in Engines.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/preview` | `{url, profile, engine, cookie, overrides, extra_args}` returns metadata and save path |
| GET/PUT/DELETE | `/api/profiles/{id}` | manage profiles (`/api/profiles/import`, `/api/profiles/export`) |
| POST | `/api/jobs` | queue a download (same body) |
| GET | `/api/jobs`, `/api/jobs/{id}` | list / detail with log |
| POST | `/api/jobs/{id}/cancel`, `/retry` | |
| GET/PUT/DELETE | `/api/cookies/{name}` | manage cookie sets |
| GET | `/api/engines/{tool}` | installed / latest / available versions |
| POST | `/api/engines/{tool}/install` | `{version: "latest" \| "nightly" \| "x.y.z"}` |
| POST | `/q` or `/gallery-dl/q` | form field `url`, gallery-dl-server compatible quick queue |

## Security

There is no login unless you set `APP_PASSWORD`. The app runs downloaders with arguments you type, so keep it on your LAN or behind a reverse proxy with auth. Cookie files are stored with owner-only permissions in `/config/cookies`.
