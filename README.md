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

**Default profile.** Every download starts from the built-in Default profile (Settings > Profiles > Default). It is generic, not tied to any site, and you can edit or reset it. Out of the box it writes these tags into every video / audio file gallery-dl downloads, skipping any that have no value:

| Tag | From |
|---|---|
| title | `{title}` |
| artist | who posted it: `author`, `uploader`, `username`, `user`, ... (first that exists) |
| date | `{date:%Y-%m-%d}` |
| description | `{description}` |
| keywords | `{tags}` written as "a, b, c" (`keywords` is the tag-list field MP4, MKV, WebM and MP3 all support) |
| genre | `{genre}` |
| comment | the link you downloaded |

yt-dlp downloads use yt-dlp's own `--embed-metadata` tags (title, artist, date, description, link, genre) and also write tags to `keywords`. The Default profile also holds the yt-dlp output template.

**Site profiles** are picked automatically when a link matches their sites, and add to or override the Default. Each can set:

- **Sites**: domains such as `example.com` (subdomains included), or `re:` followed by a regex for the whole link
- **Engine**
- **Info fields** and **User field**: which fields the Metadata panel lists, and which one is "Posted by"
- **Metadata**: `field = value` lines; `{field}` or `{a|b}` pull from the link's own metadata
- **Tags** written into video/audio files, as `tag = template` lines (an empty value turns a Default tag off)
- **gallery-dl options**: any JSON in the same format as `gallery-dl.conf`
- **yt-dlp** extra arguments and output template

**Import file** and **Paste text** accept a profiles export, a single profile, a list of profiles, or a whole `gallery-dl.conf`. **Export** downloads everything as one JSON file. No site profiles ship with the app; profiles, cookies and your gallery-dl config live only in the `/config` folder.

## Where files are saved

Open **Metadata**, then **Saved to › Edit** to build the folder and file name by dragging fields from the current link (tap to add on a phone). `/ folder` starts a new folder; the extension is added for you. You can also paste a pattern such as `{category}/{uploader}/{title}`, where `{a|b}` uses the first field that exists, `{date:%Y-%m-%d}` formats a date and `{title[:100]}` shortens.

- Saved to a **site profile**, the pattern always wins for that site.
- Saved to the **Default profile**, it applies to every site that has no naming of its own (site rules in your gallery-dl config still win; the builder tells you when that happens).
- **Reset to app default** goes back to gallery-dl's own per-site names and the built-in yt-dlp template.

Everything is stored in `/config`, so updating the app never changes your naming.

## Settings

Cookies: every saved cookie file is used for every download (each site only receives its own cookies), so there is nothing to select.

Settings is its own page (top right): Profiles, Cookies, Engines (update or pin gallery-dl / yt-dlp), gallery-dl config (your global `gallery-dl.conf`), and General (output template, default arguments, preview limit).

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
