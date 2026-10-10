"""Runs inside the gallery-dl virtualenv for previews.

Usage: python runner_gdl.py <gallery-dl args...> -s URL

Runs gallery-dl in simulate mode and reports, for every file, the metadata and the
exact path it would be saved to (after config, profiles and overrides are applied).
Each record is printed on its own line prefixed with MARK.
"""
import json
import os
import sys

import gallery_dl
from gallery_dl import formatter, job

# Tags the app will write into video/audio files with ffmpeg (tag -> gallery-dl format string)
EMBED_TAGS = json.loads(os.environ.get("WEBUI_EMBED_TAGS") or "{}")
MEDIA_EXT = tuple(json.loads(os.environ.get("WEBUI_MEDIA_EXT") or "[]"))
_fmts = {}


def render_tags(kwdict):
    out = {}
    for tag, tmpl in EMBED_TAGS.items():
        try:
            if tmpl not in _fmts:
                _fmts[tmpl] = formatter.parse(tmpl).format_map
            out[tag] = _fmts[tmpl](kwdict)
        except Exception as e:
            out[tag] = f"(error: {e})"
    return out

MARK = "@@WEBUI@@"


def _safe(v, depth=0):
    """JSON-friendly copy that never fails (cycles, odd types, very deep data)."""
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if depth > 6:
        return str(v)
    if isinstance(v, dict):
        return {str(k): _safe(x, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_safe(x, depth + 1) for x in v]
    return str(v)


def emit(obj):
    try:
        line = json.dumps(obj, default=str)
    except Exception:
        try:
            line = json.dumps(_safe(obj))
        except Exception as e:
            line = json.dumps({"t": obj.get("t"), "url": obj.get("url"), "path": obj.get("path"),
                               "meta": {"_preview_error": str(e)}})
    sys.stdout.write(MARK + line + "\n")
    sys.stdout.flush()


_orig_directory = job.SimulationJob.handle_directory


def handle_directory(self, kwdict):
    _orig_directory(self, kwdict)
    try:
        self.pathfmt.set_directory(kwdict)
    except Exception as e:
        self.log.warning("preview: could not build directory: %s", e)
    try:
        emit({"t": "dir", "meta": kwdict})
    except Exception as e:
        self.log.warning("preview: %s", e)


def handle_url(self, url, kwdict):
    ext = kwdict.get("extension") or "jpg"
    try:
        kwdict["extension"] = self.pathfmt.extension_map(ext, ext)
    except Exception:
        pass
    path = None
    try:
        path = (self.pathfmt.directory or "") + self.pathfmt.build_filename(kwdict)
    except Exception as e:
        self.log.warning("preview: could not build file name: %s", e)
    ext = str(kwdict.get("extension") or "").lower()
    media = ext in MEDIA_EXT if MEDIA_EXT else None
    tags = render_tags(kwdict) if (EMBED_TAGS and media) else None
    try:
        emit({"t": "file", "url": url, "path": path, "meta": kwdict, "tags": tags, "media": media})
    except Exception as e:
        self.log.warning("preview: %s", e)


job.SimulationJob.handle_directory = handle_directory
job.SimulationJob.handle_url = handle_url

if __name__ == "__main__":
    sys.exit(gallery_dl.main())
