"""Writes metadata tags into downloaded video / audio files for gallery-dl.

Loaded by gallery-dl's "python" post-processor (event "after") and imported by the
preview runner, so the preview shows exactly what will be written.

Configuration comes from environment variables set by webui-dl:
  WEBUI_EMBED_TAGS  {"tag": "gallery-dl format string", ...}
  WEBUI_MEDIA_EXT   ["mp4", "mkv", ...]  file types that can hold tags
  WEBUI_SOURCE_URL  the link that was queued, available as {source_url}

Rules:
  * a tag whose value comes out empty is skipped (nothing like "None" is written)
  * lists such as tags are written as "a, b, c"
  * the file's modification time is kept
"""
import json
import os
import subprocess
import sys

from gallery_dl import formatter

EMBED_TAGS = json.loads(os.environ.get("WEBUI_EMBED_TAGS") or "{}")
MEDIA_EXT = tuple(json.loads(os.environ.get("WEBUI_MEDIA_EXT") or "[]"))
SOURCE_URL = os.environ.get("WEBUI_SOURCE_URL", "")
_SIMPLE = (str, int, float, bool)
_fmts = {}


class JoinedList(list):
    """A list that prints as 'a, b, c' but can still be indexed ({tags[0]}) and joined ({tags:J/})."""

    def __str__(self):
        return ", ".join(str(x) for x in self)

    def __format__(self, spec):
        return format(str(self), spec)


def _prepare(kwdict):
    data = {}
    for k, v in kwdict.items():
        if isinstance(v, list) and v and all(isinstance(x, _SIMPLE) for x in v):
            v = JoinedList(v)
        data[k] = v
    data.setdefault("source_url", SOURCE_URL)
    return data


def _usable(text):
    t = text.strip()
    if not t or t in ("None", "[]", "{}", "()"):
        return False
    if t[0] in "{[" and t[-1] in "}]":  # a raw dict / list that wasn't meant to be printed
        return False
    return True


def render(kwdict, templates=None):
    """{tag: value} for every tag whose template produces a real value."""
    data = _prepare(kwdict)
    out = {}
    for tag, tmpl in (templates if templates is not None else EMBED_TAGS).items():
        try:
            fn = _fmts.get(tmpl)
            if fn is None:
                fn = _fmts[tmpl] = formatter.parse(str(tmpl), "").format_map
            value = str(fn(data))
        except Exception:
            continue
        if _usable(value):
            out[tag] = value.replace("\0", "").strip()
    return out


def _log(level, msg):
    print(f"[webui-dl][{level}] {msg}", file=sys.stderr, flush=True)


def tag(kwdict):
    """gallery-dl post-processor entry point."""
    proxy = kwdict.get("_path")
    path = getattr(proxy, "realpath", None) or getattr(proxy, "path", None) or (str(proxy) if proxy else "")
    if not path or not os.path.isfile(path):
        return
    ext = path.rsplit(".", 1)[-1].lower()
    if MEDIA_EXT and ext not in MEDIA_EXT:
        return
    values = render(kwdict)
    if not values:
        return
    tmp = f"{path}.webui-tags.{ext}"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path, "-map", "0", "-codec", "copy"]
    for k, v in values.items():
        cmd += ["-metadata", f"{k}={v}"]
    cmd.append(tmp)
    try:
        st = os.stat(path)
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0 and os.path.getsize(tmp) > 0:
            os.replace(tmp, path)
            os.utime(path, (st.st_atime, st.st_mtime))
            _log("info", f"Wrote tags ({', '.join(values)}) into {os.path.basename(path)}")
        else:
            _log("warning", f"Could not write tags into {os.path.basename(path)}: {r.stderr.strip()[-300:]}")
    except Exception as e:
        _log("warning", f"Could not write tags into {os.path.basename(path)}: {e}")
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
