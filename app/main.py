"""webui-dl: a small web UI around gallery-dl and yt-dlp, in the spirit of gallery-dl-server.

  * preview the metadata (and final save path) a link will produce before downloading
  * override / add metadata fields per download
  * site profiles that are picked automatically from the link
  * cookies, engine versions and gallery-dl config managed from the browser

Everything user-specific (profiles, cookies, gallery-dl config, settings) lives in CONFIG_DIR.
"""
import asyncio
import base64
import collections
import copy
import glob
import json
import os
import re
import secrets
import shlex
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

CONFIG = Path(os.environ.get("CONFIG_DIR", "/config"))
DOWNLOADS = Path(os.environ.get("DOWNLOAD_DIR", "/downloads"))
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "2"))
APP_USER = os.environ.get("APP_USER", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

APP_DIR = Path(__file__).parent
try:
    APP_VERSION = (APP_DIR / "VERSION").read_text().strip()
except OSError:
    APP_VERSION = "dev"
GIT_SHA = os.environ.get("GIT_SHA", "")[:7]
RUNNER_YTDLP = APP_DIR / "runner_ytdlp.py"
RUNNER_GDL = APP_DIR / "runner_gdl.py"
TAGGER = APP_DIR / "gdl_tagger.py"
VENVS = CONFIG / "venvs"
COOKIES = CONFIG / "cookies"
RUN_DIR = CONFIG / "run"
SETTINGS_FILE = CONFIG / "settings.json"
JOBS_FILE = CONFIG / "jobs.json"
PROFILES_FILE = CONFIG / "profiles.json"
GDL_CONF = CONFIG / "gallery-dl.conf"
LOG_DIR = CONFIG / "logs"
LOG_FILE = LOG_DIR / "webui-dl.log"

for d in (CONFIG, DOWNLOADS, VENVS, COOKIES, RUN_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

ENGINES = {
    "gallery-dl": {
        "pkg": "gallery-dl",
        "spec": "gallery-dl",
        "nightly": ["gallery-dl @ https://github.com/mikf/gallery-dl/archive/master.tar.gz"],
    },
    "yt-dlp": {
        "pkg": "yt-dlp",
        "spec": "yt-dlp[default]",
        "nightly": ["--pre", "yt-dlp[default]"],
    },
}

DEFAULT_SETTINGS = {
    "ytdlp_output": "%(uploader,channel|Unknown)s/%(title)s [%(id)s].%(ext)s",
    "ytdlp_args": "--embed-metadata --embed-thumbnail --embed-chapters",
    "gdl_args": "",
    "preview_limit": 50,
    "embed_overrides": True,
}

# Tags ffmpeg can write that people commonly override.
TAG_FIELDS = ("title", "artist", "album", "album_artist", "genre", "comment", "date",
              "description", "composer", "track")
MEDIA_EXT = ("mp4", "m4v", "mov", "mkv", "webm", "mp3", "m4a", "aac", "ogg", "opus", "flac", "wav")
NAME_RE = r"[A-Za-z0-9._-]{1,64}"

app = FastAPI(title="webui-dl")


# --------------------------------------------------------------------------- auth
@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if APP_PASSWORD:
        hdr = request.headers.get("authorization", "")
        ok = False
        if hdr.lower().startswith("basic "):
            try:
                user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, APP_USER) and secrets.compare_digest(pw, APP_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="webui-dl"'})
    return await call_next(request)


# --------------------------------------------------------------------------- activity log
LOG_KEEP = 3000
LOG_MAX_BYTES = 2_000_000
_log = collections.deque(maxlen=LOG_KEEP)   # (seq, level, text)
_log_seq = 0
_LEVEL_RE = re.compile(r"^\S+ \S+ \[(\w+)\]")


def level_of(line: str) -> str:
    l = line.lower()
    if "[error]" in l or l.startswith("error:") or " error:" in l[:40]:
        return "error"
    if "[warning]" in l or l.startswith("warning:"):
        return "warning"
    if "[debug]" in l:
        return "debug"
    return "info"


def _log_add(text: str, level: str):
    global _log_seq
    _log_seq += 1
    _log.append((_log_seq, level, text))


def applog(msg: str, level: str = "info"):
    for part in str(msg).splitlines() or [""]:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {part}"
        _log_add(line, level)
        try:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
                LOG_FILE.replace(LOG_FILE.with_name(LOG_FILE.name + ".1"))
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def load_log():
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_KEEP:]
    except OSError:
        return
    for line in lines:
        m = _LEVEL_RE.match(line)
        _log_add(line, m.group(1) if m else "info")


# --------------------------------------------------------------------------- helpers
def write_private(path: Path, text: str):
    path.write_text(text)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            s.update(json.loads(SETTINGS_FILE.read_text()))
        except Exception:
            pass
    return s


def venv_python(tool: str) -> Path:
    return VENVS / tool / "bin" / "python"


def engine_ready(tool: str) -> bool:
    return venv_python(tool).exists() and ENGINE_STATE[tool].get("version") is not None


async def run(cmd, timeout=None, stdin: bytes | None = None, cwd=None, env=None):
    proc = await asyncio.create_subprocess_exec(
        *map(str, cmd),
        env={**os.environ, **env} if env else None,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        limit=2**22,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise HTTPException(504, "Timed out while reading metadata")
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def cookie_path(name: str | None, required=True) -> Path | None:
    if not name:
        return None
    if not re.fullmatch(NAME_RE, name):
        raise HTTPException(400, "Invalid cookie set name")
    p = COOKIES / f"{name}.txt"
    if not p.exists():
        if required:
            raise HTTPException(404, f"Cookie set '{name}' not found")
        return None
    return p


def split_args(s: str | None) -> list[str]:
    try:
        return shlex.split(s or "")
    except ValueError as e:
        raise HTTPException(400, f"Could not parse arguments: {e}")


SIMPLE = (str, int, float, bool, type(None))


def clean_meta(d: dict) -> dict:
    """Keep top-level fields that make sense to show and edit."""
    out = {}
    for k, v in (d or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, SIMPLE):
            out[k] = v
        elif isinstance(v, list) and len(v) <= 100 and all(isinstance(x, SIMPLE) for x in v):
            out[k] = v
        elif isinstance(v, dict) and len(json.dumps(v, default=str)) < 2000:
            out[k] = v
    return out


def check_fields(d: dict, what="metadata") -> dict:
    out = {}
    for k, v in (d or {}).items():
        k = str(k).strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            raise HTTPException(400, f"Invalid {what} field name: {k!r}")
        out[k] = v
    return out


def check_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        raise HTTPException(400, "Enter a full http(s) link")
    return url


def rel(path: str | None) -> str | None:
    root = str(DOWNLOADS).rstrip("/") + "/"
    if path and path.startswith(root):
        return path[len(root):]
    return path


# --------------------------------------------------------------------------- engines
ENGINE_STATE = {t: {"version": None, "installing": False, "log": [], "latest": None} for t in ENGINES}


async def installed_version(tool: str):
    py = venv_python(tool)
    if not py.exists():
        return None
    rc, out, _ = await run([py, "-c", f"import importlib.metadata as m;print(m.version({ENGINES[tool]['pkg']!r}))"], 30)
    return out.strip() if rc == 0 else None


def link_engines():
    """Let gallery-dl import the managed yt-dlp (for links it hands off to yt-dlp)."""
    ytdlp_sp = glob.glob(str(VENVS / "yt-dlp" / "lib" / "python*" / "site-packages"))
    gdl_sp = glob.glob(str(VENVS / "gallery-dl" / "lib" / "python*" / "site-packages"))
    if ytdlp_sp and gdl_sp:
        try:
            (Path(gdl_sp[0]) / "webui_dl_ytdlp.pth").write_text(ytdlp_sp[0] + "\n")
        except OSError:
            pass


def pypi_info(pkg: str) -> dict:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json", timeout=20) as r:
        return json.load(r)


def version_key(v: str):
    return [(0, int(x), "") if x.isdigit() else (-1, 0, x) for x in re.split(r"[.\-]", v)]


def same_version(a, b):
    return bool(a and b) and version_key(a) == version_key(b)


async def install_engine(tool: str, version: str = "latest"):
    st = ENGINE_STATE[tool]
    if st["installing"]:
        raise HTTPException(409, "An install is already running for this engine")
    eng = ENGINES[tool]
    st["installing"] = True
    st["log"] = []
    applog(f"Installing {tool} ({version})")

    def log(line):
        st["log"].append(line)
        st["log"] = st["log"][-400:]

    try:
        venv = VENVS / tool
        if not venv_python(tool).exists():
            log(f"$ creating virtualenv {venv}")
            rc, out, err = await run([sys.executable, "-m", "venv", venv], 300)
            log(out + err)
            if rc != 0:
                raise RuntimeError("venv creation failed")
        if version == "latest":
            spec = ["-U", eng["spec"]]
        elif version == "nightly":
            spec = ["-U", *eng["nightly"]]
        else:
            if not re.fullmatch(r"[0-9A-Za-z.\-]+", version):
                raise RuntimeError("invalid version string")
            spec = [f"{eng['spec']}=={version}"]
        cmd = [venv_python(tool), "-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir", *spec]
        log("$ " + " ".join(map(str, cmd)))
        proc = await asyncio.create_subprocess_exec(
            *map(str, cmd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        async for raw in proc.stdout:
            log(raw.decode(errors="replace").rstrip())
        rc = await proc.wait()
        if rc != 0:
            raise RuntimeError(f"pip exited with {rc}")
        st["version"] = await installed_version(tool)
        link_engines()
        log(f"Installed {tool} {st['version']}")
        applog(f"Installed {tool} {st['version']}")
    except Exception as e:
        log(f"ERROR: {e}")
        applog(f"Installing {tool} failed: {e}", "error")
    finally:
        st["installing"] = False


async def refresh_latest(tool):
    try:
        info = await asyncio.to_thread(pypi_info, ENGINES[tool]["pkg"])
        ENGINE_STATE[tool]["latest"] = info["info"]["version"]
    except Exception:
        pass


@app.on_event("startup")
async def startup():
    load_log()
    applog(f"webui-dl v{APP_VERSION}{' (' + GIT_SHA + ')' if GIT_SHA else ''} started")
    load_jobs()
    link_engines()
    for tool in ENGINES:
        ENGINE_STATE[tool]["version"] = await installed_version(tool)
        if ENGINE_STATE[tool]["version"] is None:
            asyncio.create_task(install_engine(tool, "latest"))
        else:
            applog(f"{tool} {ENGINE_STATE[tool]['version']}")
        asyncio.create_task(refresh_latest(tool))


@app.get("/api/log")
async def get_log(after: int = 0):
    lines = [{"n": n, "level": lv, "text": t} for n, lv, t in _log if n > after]
    return {"seq": _log_seq, "lines": lines}


@app.delete("/api/log")
async def clear_log():
    _log.clear()
    try:
        LOG_FILE.write_text("")
    except OSError:
        pass
    applog("Log cleared")
    return {"ok": True}


@app.get("/api/info")
async def info():
    return {"version": APP_VERSION, "commit": GIT_SHA,
            "engines": {t: st["version"] for t, st in ENGINE_STATE.items()}}


@app.get("/api/engines")
async def engines():
    out = {}
    for tool, st in ENGINE_STATE.items():
        out[tool] = {k: st[k] for k in ("version", "installing", "latest")}
        out[tool]["up_to_date"] = same_version(st["version"], st["latest"])
    return out


@app.get("/api/engines/{tool}")
async def engine_detail(tool: str):
    if tool not in ENGINES:
        raise HTTPException(404)
    st = ENGINE_STATE[tool]
    versions = []
    try:
        info = await asyncio.to_thread(pypi_info, ENGINES[tool]["pkg"])
        st["latest"] = info["info"]["version"]
        versions = sorted(info["releases"].keys(), key=version_key, reverse=True)[:40]
    except Exception as e:
        st["log"].append(f"Could not reach PyPI: {e}")
    return {**{k: st[k] for k in ("version", "installing", "latest", "log")}, "versions": versions,
            "up_to_date": same_version(st["version"], st["latest"])}


class InstallReq(BaseModel):
    version: str = "latest"


@app.post("/api/engines/{tool}/install")
async def engine_install(tool: str, req: InstallReq):
    if tool not in ENGINES:
        raise HTTPException(404)
    if ENGINE_STATE[tool]["installing"]:
        raise HTTPException(409, "Install already running")
    asyncio.create_task(install_engine(tool, req.version))
    return {"ok": True}


# --------------------------------------------------------------------------- settings
@app.get("/api/settings")
async def get_settings():
    return load_settings()


@app.put("/api/settings")
async def put_settings(body: dict):
    s = load_settings()
    for k in DEFAULT_SETTINGS:
        if k in body:
            s[k] = body[k]
    s["preview_limit"] = max(1, min(int(s.get("preview_limit") or 50), 500))
    s["embed_overrides"] = bool(s.get("embed_overrides"))
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))
    return s


class TextBody(BaseModel):
    content: str


@app.get("/api/gdlconfig")
async def get_gdlconfig():
    return {"content": GDL_CONF.read_text(errors="replace") if GDL_CONF.exists() else ""}


@app.put("/api/gdlconfig")
async def put_gdlconfig(req: TextBody):
    text = req.content.strip()
    if not text:
        if GDL_CONF.exists():
            GDL_CONF.unlink()
        return {"ok": True, "content": ""}
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("the top level must be a { } object")
    except Exception as e:
        raise HTTPException(400, f"Not valid JSON: {e}")
    write_private(GDL_CONF, text + "\n")
    return {"ok": True}


# --------------------------------------------------------------------------- profiles
# Who posted it: the field differs per site, so try the common ones in order.
POSTER_CHAIN = "author|uploader|username|user[name]|user[username]|user|artist|creator|owner|channel"

# The built-in Default profile: generic, applies to every download, never site specific.
DEFAULT_PROFILE = {
    "id": "default",
    "name": "Default",
    "match": [],
    "engine": "auto",
    "metadata": {},
    # Written into video / audio files downloaded by gallery-dl. Empty values are skipped.
    # Mirrors what yt-dlp's --embed-metadata writes, so files from both engines look alike.
    "embed_tags": {
        "title": "{title}",
        "artist": "{" + POSTER_CHAIN + "}",
        "date": "{date:%Y-%m-%d}",
        "description": "{description|content|caption}",
        "keywords": "{tags}",
        "genre": "{genre}",
        "comment": "{source_url}",
    },
    "fields": ["title", "description", "tags", "date", "upload_date"],
    "user_field": "",
    "gallery_dl": {},
    "ytdlp_args": "",
    "ytdlp_output": "%(uploader,channel|Unknown)s/%(title)s [%(id)s].%(ext)s",
    "notes": "Applies to every download. Site profiles add to it or override it.",
}

PROFILE_FIELDS = {
    "name": "", "match": [], "engine": "auto", "metadata": {}, "embed_tags": {},
    "fields": [], "user_field": "",
    "gallery_dl": {}, "ytdlp_args": "", "ytdlp_output": "", "notes": "",
}


def _load_all() -> list[dict]:
    if PROFILES_FILE.exists():
        try:
            data = json.loads(PROFILES_FILE.read_text())
            return data.get("profiles", []) if isinstance(data, dict) else data
        except Exception:
            pass
    return []


def load_profiles() -> list[dict]:
    """Site profiles only."""
    return [p for p in _load_all() if p.get("id") != "default"]


def default_profile() -> dict:
    saved = next((p for p in _load_all() if p.get("id") == "default"), None)
    return {**copy.deepcopy(DEFAULT_PROFILE), **(saved or {}), "id": "default", "match": []}


def deep_merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def save_profiles(profiles: list[dict], default: dict | None = None):
    keep = default if default is not None else next((p for p in _load_all() if p.get("id") == "default"), None)
    allp = ([keep] if keep else []) + [p for p in profiles if p.get("id") != "default"]
    write_private(PROFILES_FILE, json.dumps({"profiles": allp}, indent=2))


def normalize_profile(p: dict, pid: str | None = None) -> dict:
    if not isinstance(p, dict):
        raise HTTPException(400, "Each profile must be an object")
    out = {k: copy.deepcopy(p.get(k, v)) for k, v in PROFILE_FIELDS.items()}
    out["name"] = str(out["name"]).strip()
    if pid == "default":
        out["name"], out["match"] = "Default", []
    if not out["name"]:
        raise HTTPException(400, "Profile needs a name")
    m = out["match"]
    if isinstance(m, str):
        m = [x for x in re.split(r"[,\s]+", m) if x]
    out["match"] = [str(x).strip().lower() if not str(x).startswith("re:") else str(x).strip() for x in m if str(x).strip()]
    for pat in out["match"]:
        if pat.startswith("re:"):
            try:
                re.compile(pat[3:])
            except re.error as e:
                raise HTTPException(400, f"Bad pattern {pat!r}: {e}")
    if out["engine"] not in ("auto", *ENGINES):
        raise HTTPException(400, "Engine must be auto, gallery-dl or yt-dlp")
    f = out["fields"]
    if isinstance(f, str):
        f = [x for x in re.split(r"[,\s]+", f) if x]
    out["fields"] = [str(x).strip() for x in f if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(x).strip())]
    out["user_field"] = str(out["user_field"] or "").strip()
    out["metadata"] = check_fields(out["metadata"] or {})
    out["embed_tags"] = check_fields(out["embed_tags"] or {}, "tag")
    if not isinstance(out["gallery_dl"], dict):
        raise HTTPException(400, "gallery-dl options must be a JSON object")
    split_args(out["ytdlp_args"])
    out["id"] = pid or p.get("id") or re.sub(r"[^a-z0-9]+", "-", out["name"].lower()).strip("-") or uuid.uuid4().hex[:8]
    return out


def match_profile(url: str) -> dict | None:
    host = (urlparse(url).hostname or "").lower()
    best, score = None, -1
    for p in load_profiles():
        for pat in p.get("match", []):
            s = -1
            if pat.startswith("re:"):
                try:
                    if re.search(pat[3:], url):
                        s = 1000
                except re.error:
                    pass
            elif pat == "*":
                s = 0
            elif host == pat or host.endswith("." + pat):
                s = len(pat)
            if s > score:
                best, score = p, s
    return best


def get_profile(pid: str) -> dict:
    for p in load_profiles():
        if p["id"] == pid:
            return p
    raise HTTPException(404, f"Profile '{pid}' not found")


@app.get("/api/profiles")
async def list_profiles():
    return load_profiles()


@app.get("/api/profiles/default")
async def get_default_profile():
    return {**default_profile(), "builtin": DEFAULT_PROFILE}


@app.put("/api/profiles/default")
async def put_default_profile(body: dict):
    new = normalize_profile({**body, "name": "Default"}, "default")
    save_profiles(load_profiles(), default=new)
    return new


@app.delete("/api/profiles/default")
async def reset_default_profile():
    save_profiles(load_profiles(), default={})
    write_private(PROFILES_FILE, json.dumps({"profiles": load_profiles()}, indent=2))
    return default_profile()


@app.put("/api/profiles/{pid}")
async def put_profile(pid: str, body: dict):
    profiles = load_profiles()
    new = normalize_profile(body, None if pid == "new" else pid)
    if pid == "new":
        base, n = new["id"], 2
        while any(p["id"] == new["id"] for p in profiles):
            new["id"] = f"{base}-{n}"
            n += 1
        profiles.append(new)
    else:
        for i, p in enumerate(profiles):
            if p["id"] == pid:
                profiles[i] = new
                break
        else:
            profiles.append(new)
    save_profiles(profiles)
    return new


@app.delete("/api/profiles/{pid}")
async def delete_profile(pid: str):
    save_profiles([p for p in load_profiles() if p["id"] != pid])
    return {"ok": True}


class MatchReq(BaseModel):
    url: str


@app.post("/api/profiles/match")
async def profile_match(req: MatchReq):
    p = match_profile(req.url)
    return {"id": p["id"], "name": p["name"]} if p else {}


@app.get("/api/profiles/export")
async def export_profiles():
    data = {"profiles": _load_all()}
    if GDL_CONF.exists():
        try:
            data["gallery_dl_config"] = json.loads(GDL_CONF.read_text())
        except Exception:
            pass
    return Response(json.dumps(data, indent=2), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="webui-dl-profiles.json"'})


class ImportReq(BaseModel):
    content: str
    replace: bool = False


@app.post("/api/profiles/import")
async def import_profiles(req: ImportReq):
    try:
        data = json.loads(req.content)
    except Exception as e:
        raise HTTPException(400, f"Not valid JSON: {e}")
    if isinstance(data, list):
        data = {"profiles": data}
    elif isinstance(data, dict) and "name" in data and "profiles" not in data and "extractor" not in data:
        data = {"profiles": [data]}  # a single profile
    if not isinstance(data, dict):
        raise HTTPException(400, "Expected a JSON object")
    raw = data.get("profiles", [])
    new_default = next((p for p in raw if isinstance(p, dict) and p.get("id") == "default"), None)
    incoming = [normalize_profile(p) for p in raw if not (isinstance(p, dict) and p.get("id") == "default")]
    gdl = data.get("gallery_dl_config")
    if not incoming and gdl is None and "extractor" in data:
        gdl = data  # a plain gallery-dl.conf
    profiles = [] if req.replace else load_profiles()
    names = {p["name"].lower(): i for i, p in enumerate(profiles)}
    for p in incoming:
        if p["name"].lower() in names:
            p["id"] = profiles[names[p["name"].lower()]]["id"]
            profiles[names[p["name"].lower()]] = p
        else:
            while any(x["id"] == p["id"] for x in profiles):
                p["id"] += "-2"
            profiles.append(p)
    save_profiles(profiles, default=normalize_profile(new_default, "default") if new_default else None)
    if isinstance(gdl, dict):
        write_private(GDL_CONF, json.dumps(gdl, indent=2) + "\n")
    return {"profiles": len(incoming) + (1 if new_default else 0), "gallery_dl_config": isinstance(gdl, dict)}


# --------------------------------------------------------------------------- cookies
NETSCAPE_HEADER = "# Netscape HTTP Cookie File"


def to_netscape(content: str) -> str:
    text = content.strip()
    if text.startswith("[") or text.startswith("{"):
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("cookies", [data])
        lines = [NETSCAPE_HEADER]
        for c in data:
            domain = c.get("domain", "")
            host_only = c.get("hostOnly", not domain.startswith("."))
            exp = c.get("expirationDate") or c.get("expires") or 0
            try:
                exp = int(float(exp))
            except (TypeError, ValueError):
                exp = 0
            lines.append("\t".join([
                domain, "FALSE" if host_only else "TRUE", c.get("path", "/"),
                "TRUE" if c.get("secure") else "FALSE", str(max(exp, 0)),
                c.get("name", ""), str(c.get("value", "")),
            ]))
        return "\n".join(lines) + "\n"
    lines = text.splitlines()
    if not lines or "netscape" not in lines[0].lower():
        lines.insert(0, NETSCAPE_HEADER)
    return "\n".join(lines) + "\n"


def cookie_summary(p: Path) -> dict:
    domains, count = set(), 0
    for line in p.read_text(errors="replace").splitlines():
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            count += 1
            domains.add(parts[0].lstrip("."))
    return {"name": p.stem, "count": count, "domains": sorted(domains)[:20], "modified": p.stat().st_mtime}


def combined_cookies(run_id: str) -> Path | None:
    """All saved cookie files merged into one jar for a single run.

    Cookies are scoped to their own domains, so each site only ever receives its own cookies.
    When the same cookie appears in several files, the most recently updated file wins.
    """
    jar: dict[tuple, str] = {}
    for p in sorted(COOKIES.glob("*.txt"), key=lambda x: x.stat().st_mtime):
        for line in p.read_text(errors="replace").splitlines():
            body = line[len("#HttpOnly_"):] if line.startswith("#HttpOnly_") else line
            if not body.strip() or body.startswith("#"):
                continue
            parts = body.split("\t")
            if len(parts) >= 7:
                jar[(parts[0].lstrip("."), parts[2], parts[5])] = line
    if not jar:
        return None
    path = RUN_DIR / f"cookies-{run_id}.txt"
    write_private(path, NETSCAPE_HEADER + "\n" + "\n".join(jar.values()) + "\n")
    return path


@app.get("/api/cookies")
async def list_cookies():
    return [cookie_summary(p) for p in sorted(COOKIES.glob("*.txt"))]


@app.get("/api/cookies/{name}")
async def get_cookie(name: str):
    return {"name": name, "content": cookie_path(name).read_text(errors="replace")}


@app.put("/api/cookies/{name}")
async def put_cookie(name: str, req: TextBody):
    if not re.fullmatch(NAME_RE, name):
        raise HTTPException(400, "Name may only contain letters, numbers, dot, dash and underscore")
    try:
        text = to_netscape(req.content)
    except Exception as e:
        raise HTTPException(400, f"Could not parse cookies: {e}")
    p = COOKIES / f"{name}.txt"
    write_private(p, text)
    s = cookie_summary(p)
    if s["count"] == 0:
        p.unlink()
        raise HTTPException(400, "No cookies found. Paste a Netscape cookies.txt or a JSON export.")
    return s


@app.delete("/api/cookies/{name}")
async def delete_cookie(name: str):
    cookie_path(name).unlink()
    return {"ok": True}


# --------------------------------------------------------------------------- request context
class DLReq(BaseModel):
    url: str
    engine: str = "auto"        # auto = use the profile's engine, else try gallery-dl then yt-dlp
    cookie: str = ""            # "-" = send no cookies; otherwise all saved cookie files are used
    profile: str = "auto"       # auto = detect from link, "-" = none, or a profile id
    overrides: dict = {}
    extra_args: str = ""
    debug: bool = False         # verbose engine log in previews


def resolve(req: DLReq) -> dict:
    url = check_url(req.url)
    settings = load_settings()
    if req.profile == "auto":
        prof = match_profile(url)
    elif req.profile in ("", "-", "default"):
        prof = None
    else:
        prof = get_profile(req.profile)
    prof = prof or {}
    base = default_profile()
    engine = req.engine
    if engine == "auto":
        engine = prof.get("engine") if prof.get("engine") not in (None, "", "auto") else base.get("engine") or "auto"
    if engine not in ("auto", *ENGINES):
        raise HTTPException(400, "Unknown engine")
    shown = prof or base
    return {
        "url": url,
        "engine": engine,
        "cookies": req.cookie != "-",
        "profile": {"id": shown["id"], "name": shown["name"],
                    "fields": prof.get("fields") or base.get("fields") or [],
                    "user_field": prof.get("user_field") or base.get("user_field") or "",
                    "site": bool(prof)},
        "auto_profile": req.profile == "auto" and bool(prof),
        "templates": {**(base.get("metadata") or {}), **(prof.get("metadata") or {})},
        "overrides": check_fields(req.overrides),
        "embed_tags": {**(base.get("embed_tags") or {}), **(prof.get("embed_tags") or {})},
        "gdl_fragment": deep_merge(base.get("gallery_dl") or {}, prof.get("gallery_dl") or {}),
        "ytdlp_output": prof.get("ytdlp_output") or base.get("ytdlp_output") or settings["ytdlp_output"],
        "ytdlp_args": split_args(settings["ytdlp_args"]) + split_args(base.get("ytdlp_args")) + split_args(prof.get("ytdlp_args")),
        "gdl_args": split_args(settings["gdl_args"]),
        "extra": split_args(req.extra_args),
        "debug": bool(getattr(req, "debug", False)),
        "settings": settings,
        "warnings": [],
    }


def esc_fmt(v):
    return v.replace("{", "{{").replace("}", "}}") if isinstance(v, str) else v


def tagger_postprocessor() -> dict:
    """gallery-dl runs app/gdl_tagger.py after each file; it writes the tags with ffmpeg."""
    return {"name": "python", "event": "after", "function": f"{TAGGER}:tag",
            "filter": f"extension in {MEDIA_EXT!r}"}


def gdl_tags(ctx: dict) -> dict:
    tags = dict(ctx["embed_tags"])
    if ctx["settings"].get("embed_overrides"):
        for k in ctx["overrides"]:
            if k in TAG_FIELDS and k not in tags:
                tags[k] = "{" + k + "}"
    return tags


def gdl_argv(ctx: dict, mode: str, run_id: str) -> tuple[list[str], list[Path], dict]:
    argv, temp, env = ["-d", str(DOWNLOADS)], [], {}
    if GDL_CONF.exists():
        argv += ["-c", str(GDL_CONF)]
    frag = copy.deepcopy(ctx["gdl_fragment"])
    keywords = {**ctx["templates"], **{k: esc_fmt(v) for k, v in ctx["overrides"].items()}}
    tags = gdl_tags(ctx)
    if ctx.get("debug"):
        argv.append("-v")
    if tags and mode == "download":
        # Attach at the "extractor" level: command-line flags such as --write-metadata replace
        # root-level postprocessors. Keep any extractor-level ones from the user's own config,
        # since a later config file would otherwise replace that list.
        ext = frag.setdefault("extractor", {})
        existing = list(ext.get("postprocessors") or [])
        if not existing and GDL_CONF.exists():
            try:
                existing = list(json.loads(GDL_CONF.read_text()).get("extractor", {}).get("postprocessors") or [])
            except Exception:
                existing = []
        ext["postprocessors"] = existing + [tagger_postprocessor()]
    if tags:
        env = {"WEBUI_EMBED_TAGS": json.dumps(tags), "WEBUI_MEDIA_EXT": json.dumps(MEDIA_EXT),
               "WEBUI_SOURCE_URL": ctx["url"]}
    if frag:
        p = RUN_DIR / f"{run_id}.json"
        write_private(p, json.dumps(frag))
        argv += ["-c", str(p)]
        temp.append(p)
    if keywords:
        argv += ["-o", "keywords=" + json.dumps(keywords)]
        if ctx["templates"]:
            argv += ["-o", "keywords-eval=true"]
    if ctx["cookies"] and (cp := combined_cookies(run_id)):
        argv += ["--cookies", str(cp)]
        temp.append(cp)
    argv += ctx["gdl_args"] + ctx["extra"]
    return argv, temp, env


def find_postprocessors(obj) -> list[str]:
    """Names of postprocessors set anywhere in a gallery-dl config."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "postprocessors" and isinstance(v, list):
                for pp in v:
                    name = pp if isinstance(pp, str) else (pp.get("name") or pp.get("type") or "?") if isinstance(pp, dict) else "?"
                    if isinstance(pp, dict) and pp.get("whitelist"):
                        name += f" (only {', '.join(pp['whitelist'])})"
                    out.append(name)
            else:
                out += find_postprocessors(v)
    return out


def writes_info(ctx: dict, engine: str) -> list[str]:
    """Plain-language list of everything that gets written besides the media file itself."""
    out = []
    if engine == "gallery-dl":
        args = ctx["gdl_args"] + ctx["extra"]
        if "--write-metadata" in args:
            out.append("a .json file next to each download with all metadata")
        if "--write-info-json" in args:
            out.append("an info.json per post / gallery")
        if "--write-tags" in args:
            out.append("a .txt file with tags next to each download")
        pps = find_postprocessors(ctx["gdl_fragment"])
        if GDL_CONF.exists():
            try:
                pps += find_postprocessors(json.loads(GDL_CONF.read_text()))
            except Exception:
                pass
        if pps:
            out.append("gallery-dl config post-processors (they may only apply to some sites): " + ", ".join(dict.fromkeys(pps)))
    else:
        args = ctx["ytdlp_args"] + ctx["extra"]
        if "--write-info-json" in args:
            out.append("a .info.json file with all metadata")
        if "--embed-thumbnail" in args:
            out.append("the thumbnail, embedded in the file")
        if "--write-thumbnail" in args:
            out.append("the thumbnail as a separate image")
        if "--embed-chapters" in args:
            out.append("chapter markers, embedded in the file")
        if "--embed-subs" in args:
            out.append("subtitles, embedded in the file")
        if "--write-subs" in args:
            out.append("subtitle files")
        if "--write-description" in args:
            out.append("a .description text file")
    return out


def ytdlp_embeds(ctx: dict) -> bool:
    args = ctx["ytdlp_args"] + ctx["extra"]
    return any(a in args for a in ("--embed-metadata", "--add-metadata")) and "--no-embed-metadata" not in args


def ytdlp_spec(ctx: dict, mode: str, run_id: str) -> tuple[dict, list[Path]]:
    args, temp = ["-P", str(DOWNLOADS), "-o", ctx["ytdlp_output"]], []
    if mode == "download":
        args += ["--newline", *ctx["ytdlp_args"]]
        if ctx.get("debug"):
            args.append("--verbose")
    if ctx["cookies"] and (cp := combined_cookies(run_id)):
        args += ["--cookies", str(cp)]
        temp.append(cp)
    spec = {"mode": mode, "url": ctx["url"], "args": args + ctx["extra"], "templates": ctx["templates"],
            "overrides": ctx["overrides"], "limit": ctx["settings"].get("preview_limit", 50),
            "verbose": ctx.get("debug", False), "embed": ytdlp_embeds(ctx),
            "embed_tags": gdl_tags(ctx) if ytdlp_embeds(ctx) else {}}
    return spec, temp


def cleanup(paths):
    for p in paths:
        try:
            p.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- preview
MARK = "@@WEBUI@@"


RAW_ITEMS = 20


class PreviewError(RuntimeError):
    def __init__(self, msg, log=None):
        super().__init__(msg)
        self.log = log or []


def log_lines(text: str, limit=400) -> list[str]:
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    return lines[-limit:]


def shared(metas: list[dict]) -> dict:
    if not metas:
        return {}
    first = metas[0]
    return {k: v for k, v in first.items() if all(m.get(k) == v for m in metas[1:])}


async def preview_gdl(ctx):
    if not engine_ready("gallery-dl"):
        raise PreviewError("gallery-dl is not installed yet")
    limit = int(ctx["settings"].get("preview_limit") or 50)
    argv, temp, env = gdl_argv(ctx, "preview", "preview-" + uuid.uuid4().hex[:8])
    try:
        rc, out, err = await run([venv_python("gallery-dl"), RUNNER_GDL, *argv, "--range", f"1-{limit}",
                                  "-s", ctx["url"]], 180, cwd=str(DOWNLOADS), env=env)
    finally:
        cleanup(temp)
    recs, other = [], []
    for line in out.splitlines():
        if line.startswith(MARK):
            try:
                recs.append(json.loads(line[len(MARK):]))
            except json.JSONDecodeError:
                pass
        elif line.strip():
            other.append(line)
    log = log_lines(err) + [f"(gallery-dl exit code {rc})"]
    errors = [l.strip() for l in err.splitlines() if "[error]" in l or "[warning]" in l]
    if not recs:
        msg = next((l for l in reversed(err.splitlines()) if "[error]" in l), None) or \
            next((l for l in reversed(err.splitlines()) if l.strip()), None)
        raise PreviewError(msg or ("gallery-dl found nothing at this link" if rc == 0 else f"exit {rc}"), log)
    items, dirs = [], []
    for r in recs:
        if r["t"] == "dir":
            dirs.append(r["meta"])
        elif r["t"] == "file":
            m = clean_meta(r["meta"])
            items.append({"kind": "file", "url": r["url"], "meta": m, "filename": rel(r.get("path")),
                          "title": m.get("title") or m.get("filename") or r["url"],
                          "tags": r.get("tags"), "media": r.get("media"),
                          "raw": r["meta"] if len(items) < RAW_ITEMS else None})
    if len(items) == 1:
        common = {**(clean_meta(dirs[0]) if dirs else {}), **items[0]["meta"]}
        raw_common = {**(dirs[0] if dirs else {}), **(items[0]["raw"] or {})}
    elif items:
        common = shared([i["meta"] for i in items])
        raw_common = shared([i["raw"] for i in items if i["raw"] is not None])
    else:
        common = clean_meta(dirs[0]) if dirs else {}
        raw_common = dirs[0] if dirs else {}
    return {
        "engine": "gallery-dl",
        "site": common.get("category") or (items[0]["meta"].get("category") if items else None),
        "title": common.get("title") or "",
        "common": common,
        "raw_common": raw_common,
        "items": items,
        "count": len(items),
        "truncated": len(items) >= limit,
        "filename": items[0]["filename"] if len(items) == 1 else None,
        "warnings": errors[-5:],
        "log": log,
        "tag_templates": gdl_tags(ctx),
        "writes": writes_info(ctx, "gallery-dl"),
    }


async def preview_ytdlp(ctx):
    if not engine_ready("yt-dlp"):
        raise PreviewError("yt-dlp is not installed yet")
    spec, temp = ytdlp_spec(ctx, "preview", "preview-" + uuid.uuid4().hex[:8])
    try:
        rc, out, err = await run([venv_python("yt-dlp"), RUNNER_YTDLP], 180, stdin=json.dumps(spec).encode())
    finally:
        cleanup(temp)
    line = next((l for l in reversed(out.splitlines()) if l.startswith("{")), None)
    if not line:
        raise PreviewError((err.strip().splitlines() or [f"exit {rc}"])[-1], log_lines(err))
    res = json.loads(line)
    log = (res.pop("log", None) or []) + log_lines(err)
    if res.get("error"):
        raise PreviewError(res["error"], log)
    res["log"] = log
    res["writes"] = writes_info(ctx, "yt-dlp")
    res["embeds"] = spec["embed"]
    res["tag_templates"] = spec["embed_tags"]
    for obj in [res, *res.get("items", [])]:
        obj["filename"] = rel(obj.get("filename"))
    return res


@app.post("/api/preview")
async def preview(req: DLReq):
    ctx = resolve(req)
    order = ["gallery-dl", "yt-dlp"] if ctx["engine"] == "auto" else [ctx["engine"]]
    errors, logs = {}, {}
    for eng in order:
        try:
            res = await (preview_gdl(ctx) if eng == "gallery-dl" else preview_ytdlp(ctx))
            res["profile"] = ctx["profile"]
            res["auto_profile"] = ctx["auto_profile"]
            res["profile_fields"] = list(ctx["templates"])
            res["warnings"] = ctx["warnings"] + res.get("warnings", [])
            res["log"] = [f"--- {eng} ---", *res.get("log", [])]
            if errors:
                res["log"] = [*(l for e in logs for l in (f"--- {e} (failed) ---", *logs[e])), *res["log"]]
            applog(f"Preview: {ctx['url']} ({eng}, {res.get('count', 0)} item(s)"
                   f"{', profile: ' + ctx['profile']['name'] if ctx['profile'] else ''})")
            if ctx.get("debug"):
                for l in res["log"][-200:]:
                    applog(l, level_of(l) if level_of(l) != "info" else "debug")
            return res
        except PreviewError as e:
            errors[eng], logs[eng] = str(e), e.log
        except RuntimeError as e:
            errors[eng], logs[eng] = str(e), []
    log = [l for e in logs for l in (f"--- {e} ---", *logs[e])]
    applog(f"Preview failed: {ctx['url']}", "error")
    for e, msg in errors.items():
        applog(f"{e}: {msg}", "error")
    for l in log[-200:]:
        applog(l, level_of(l) if level_of(l) != "info" else "debug")
    return JSONResponse({"error": "No metadata found", "details": errors, "log": log, "profile": ctx["profile"]},
                        status_code=422)


# --------------------------------------------------------------------------- jobs
JOBS: dict[str, dict] = {}
PROCS: dict[str, asyncio.subprocess.Process] = {}
SEM = asyncio.Semaphore(MAX_CONCURRENT)
REQ_KEYS = ("url", "engine", "cookie", "profile", "overrides", "extra_args", "debug")


def save_jobs():
    try:
        JOBS_FILE.write_text(json.dumps(list(JOBS.values())[-200:], default=str))
    except Exception:
        pass


def load_jobs():
    if JOBS_FILE.exists():
        try:
            for j in json.loads(JOBS_FILE.read_text()):
                if j.get("status") in ("queued", "running"):
                    j["status"] = "interrupted"
                JOBS[j["id"]] = j
        except Exception:
            pass


def job_log(job, line):
    job["log"].append(line)
    if len(job["log"]) > 500:
        job["log"] = job["log"][-500:]


async def execute(job: dict, ctx: dict, engine: str) -> int:
    env = None
    if engine == "gallery-dl":
        argv, temp, env = gdl_argv(ctx, "download", job["id"])
        confs = [a for i, a in enumerate(argv) if i and argv[i - 1] == "-c"]
        if GDL_CONF.exists():
            applog(f"Configuration file found: {GDL_CONF}")
        if confs:
            applog(f"Loaded gallery-dl configuration file(s): {[str(GDL_CONF) if c == str(GDL_CONF) else 'profile options' for c in confs]}")
        if "--cookies" in argv:
            applog(f"Using cookies from {len(list(COOKIES.glob('*.txt')))} saved file(s)")
        cmd, stdin = [venv_python("gallery-dl"), "-m", "gallery_dl", *argv, ctx["url"]], None
    else:
        spec, temp = ytdlp_spec(ctx, "download", job["id"])
        cmd, stdin = [venv_python("yt-dlp"), RUNNER_YTDLP], json.dumps(spec).encode()
        applog(f"Starting yt-dlp for {ctx['url']}")
        if "--cookies" in spec["args"]:
            applog(f"Using cookies from {len(list(COOKIES.glob('*.txt')))} saved file(s)")
    job["engine_used"] = engine
    job_log(job, f"$ {engine} {ctx['url']}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *map(str, cmd),
            stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(DOWNLOADS),
            limit=2**20,
            env={**os.environ, **env} if env else None,
        )
        PROCS[job["id"]] = proc
        if stdin:
            proc.stdin.write(stdin)
            await proc.stdin.drain()
            proc.stdin.close()
        async for raw in proc.stdout:
            for line in raw.decode(errors="replace").replace("\r", "\n").splitlines():
                line = line.rstrip()
                if not line:
                    continue
                m = re.search(r"\[download\]\s+([\d.]+)%", line)
                if m:
                    job["progress"] = float(m.group(1))
                    job["status_line"] = line
                    continue
                if engine == "gallery-dl" and line.startswith(str(DOWNLOADS)):
                    job["files"] = job.get("files", 0) + 1
                if line.startswith("[download] Destination:"):
                    job["files"] = job.get("files", 0) + 1
                job["status_line"] = rel(line) or line
                job_log(job, line)
                if engine == "gallery-dl" and line.startswith("# " + str(DOWNLOADS)):
                    applog("Already downloaded, skipped: " + line[2:])
                else:
                    applog(line, level_of(line))
        return await proc.wait()
    finally:
        PROCS.pop(job["id"], None)
        cleanup(temp)


async def run_job(job: dict):
    async with SEM:
        if job["status"] == "cancelled":
            return
        job.update(status="running", started=time.time())
        try:
            ctx = resolve(DLReq(**{k: job[k] for k in REQ_KEYS if job.get(k) is not None}))
            if ctx["profile"]:
                applog(f"Using profile: {ctx['profile']['name']}")
            for w in ctx["warnings"]:
                job_log(job, w)
                applog(w, "warning")
            if ctx["engine"] == "auto":
                rc = await execute(job, ctx, "gallery-dl")
                if rc == 64 and job["status"] != "cancelled":
                    job_log(job, "gallery-dl does not support this link, trying yt-dlp")
                    applog("gallery-dl does not support this link, trying yt-dlp")
                    rc = await execute(job, ctx, "yt-dlp")
            else:
                rc = await execute(job, ctx, ctx["engine"])
            if job["status"] != "cancelled":
                job["status"] = "done" if rc == 0 else "error"
                job["returncode"] = rc
                if rc == 0:
                    job["progress"] = 100.0
                    applog("Download process exited successfully")
                else:
                    applog(f"Download process exited with code {rc}: {job['url']}", "error")
            else:
                applog(f"Download cancelled: {job['url']}", "warning")
        except HTTPException as e:
            job["status"] = "error"
            job_log(job, f"ERROR: {e.detail}")
            applog(f"{job['url']}: {e.detail}", "error")
        except Exception as e:
            job["status"] = "error"
            job_log(job, f"ERROR: {e}")
            applog(f"{job['url']}: {e}", "error")
        finally:
            job["finished"] = time.time()
            save_jobs()


def public_job(j, full=False):
    d = {k: v for k, v in j.items() if k != "log"}
    if full:
        d["log"] = j["log"]
    return d


@app.post("/api/jobs")
async def create_job(req: DLReq):
    ctx = resolve(req)
    if ctx["engine"] != "auto" and not engine_ready(ctx["engine"]):
        raise HTTPException(409, f"{ctx['engine']} is not installed yet")
    job = {
        "id": uuid.uuid4().hex[:12],
        **{k: getattr(req, k) for k in REQ_KEYS},
        "url": ctx["url"],
        "profile_name": ctx["profile"]["name"] if ctx["profile"] else None,
        "status": "queued",
        "progress": None,
        "files": 0,
        "created": time.time(),
        "log": [],
        "status_line": "",
    }
    JOBS[job["id"]] = job
    applog(f"Added URL to the download queue: {job['url']}")
    asyncio.create_task(run_job(job))
    save_jobs()
    return public_job(job)


@app.get("/api/jobs")
async def list_jobs():
    return [public_job(j) for j in sorted(JOBS.values(), key=lambda j: j["created"], reverse=True)]


@app.get("/api/jobs/{jid}")
async def get_job(jid: str):
    if jid not in JOBS:
        raise HTTPException(404)
    return public_job(JOBS[jid], full=True)


@app.post("/api/jobs/{jid}/cancel")
async def cancel_job(jid: str):
    job = JOBS.get(jid)
    if not job:
        raise HTTPException(404)
    job["status"] = "cancelled"
    proc = PROCS.get(jid)
    if proc and proc.returncode is None:
        proc.terminate()
    save_jobs()
    return {"ok": True}


@app.post("/api/jobs/{jid}/retry")
async def retry_job(jid: str):
    old = JOBS.get(jid)
    if not old:
        raise HTTPException(404)
    return await create_job(DLReq(**{k: old[k] for k in REQ_KEYS if old.get(k) is not None}))


@app.delete("/api/jobs/{jid}")
async def delete_job(jid: str):
    job = JOBS.get(jid)
    if job and job["status"] in ("queued", "running"):
        raise HTTPException(409, "Cancel the job first")
    JOBS.pop(jid, None)
    save_jobs()
    return {"ok": True}


@app.post("/api/jobs/clear")
async def clear_jobs():
    for jid in [k for k, j in JOBS.items() if j["status"] not in ("queued", "running")]:
        JOBS.pop(jid)
    save_jobs()
    return {"ok": True}


# gallery-dl-server compatible endpoint, handy for bookmarklets / shortcuts
@app.post("/gallery-dl/q")
@app.post("/q")
async def quick_queue(url: str = Form(...), engine: str = Form("auto")):
    return await create_job(DLReq(url=url, engine=engine))


@app.get("/settings")
@app.get("/")
async def index():
    return FileResponse(APP_DIR / "static" / "index.html")
