"""Build Kodi-style .nfo files (as read by Stash's nfoSceneParser, Jellyfin and Kodi)
from the metadata tags inside a video file.

Tag -> .nfo mapping
  title                        -> <title>
  description / synopsis       -> <plot>   (comment too, when it is plain text)
  date (2026-10-08, 20261008)  -> <premiered>   (a bare year -> <year>)
  artist / album_artist        -> <actor><name> (or <studio>, see artist_as)
  keywords / genre / tags      -> one <tag> each ("a, b" and "['a', 'b']" are both split)
  comment / purl (a link)      -> <url>

Only standard library + ffprobe, so it runs in the app and in both engine environments.
"""
import ast
import json
import os
import re
import subprocess
from xml.sax.saxutils import escape

VIDEO_EXT = ("mp4", "m4v", "mov", "mkv", "webm", "avi", "wmv", "flv", "mpg", "mpeg", "ts")


def read_tags(path: str) -> dict:
    """Container tags of a media file, keys lower-cased."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_entries", "format_tags", path],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-300:] or f"ffprobe exit {r.returncode}")
    tags = (json.loads(r.stdout or "{}").get("format") or {}).get("tags") or {}
    return {str(k).lower(): str(v).strip() for k, v in tags.items() if str(v).strip()}


def _is_url(s: str) -> bool:
    return bool(re.match(r"^https?://\S+$", s.strip()))


def _split_list(s: str) -> list[str] | None:
    """Values like "['a', 'b']" or "['a'],['b', 'c']" written by older setups. None if not a list."""
    t = s.strip()
    if not t.startswith("["):
        return None
    try:
        val = ast.literal_eval("(" + t + ",)")
    except Exception:
        found = re.findall(r"'([^']*)'|\"([^\"]*)\"", t)
        return [a or b for a, b in found] or None
    out = []

    def walk(x):
        if isinstance(x, (list, tuple, set)):
            for y in x:
                walk(y)
        elif x not in (None, ""):
            out.append(str(x))
    walk(val)
    return out


def _split_words(s: str) -> list[str]:
    lst = _split_list(s)
    if lst is not None:
        return lst
    return [p.strip() for p in re.split(r"\s*[,;]\s*", s) if p.strip()]


def _date(s: str) -> tuple[str, str]:
    """('premiered', 'YYYY-MM-DD') or ('year', 'YYYY') or ('', '')."""
    t = s.strip()
    m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", t)
    if m:
        return "premiered", f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^(\d{4})$", t)
    return ("year", t) if m else ("", "")


def build(tags: dict, artist_as: str = "performer") -> dict:
    """Pick the .nfo fields out of a file's tags."""
    out = {"title": "", "plot": "", "date_kind": "", "date": "", "people": [], "studio": "", "tags": [], "url": ""}
    out["title"] = tags.get("title", "")
    comment = tags.get("comment", "")
    plot = tags.get("description") or tags.get("synopsis") or ""
    url = tags.get("purl") or ""
    tag_values = []
    for key in ("keywords", "tags", "genre"):
        if tags.get(key):
            tag_values += _split_words(tags[key])
    if comment:
        if _is_url(comment):
            url = url or comment
        elif _split_list(comment) is not None:
            tag_values += _split_list(comment)
        elif not plot:
            plot = comment
    out["plot"], out["url"] = plot, url
    out["date_kind"], out["date"] = _date(tags.get("date", "") or tags.get("creation_time", "")[:10])
    names = [n for n in _split_words(tags.get("artist") or tags.get("album_artist") or "") if n]
    if artist_as == "studio":
        out["studio"] = names[0] if names else ""
    elif artist_as == "performer":
        out["people"] = names
    seen = set()
    out["tags"] = [t for t in tag_values if not (t.lower() in seen or seen.add(t.lower()))]
    return out


def useful(fields: dict) -> bool:
    return any(fields.get(k) for k in ("title", "plot", "date", "people", "studio", "tags", "url"))


def to_xml(fields: dict) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', "<movie>"]
    if fields.get("title"):
        lines.append(f"  <title>{escape(fields['title'])}</title>")
    if fields.get("plot"):
        lines.append(f"  <plot>{escape(fields['plot'])}</plot>")
    if fields.get("date"):
        lines.append(f"  <{fields['date_kind']}>{escape(fields['date'])}</{fields['date_kind']}>")
    if fields.get("studio"):
        lines.append(f"  <studio>{escape(fields['studio'])}</studio>")
    for i, name in enumerate(fields.get("people") or []):
        lines.append(f"  <actor>\n    <name>{escape(name)}</name>\n    <order>{i}</order>\n  </actor>")
    for t in fields.get("tags") or []:
        lines.append(f"  <tag>{escape(t)}</tag>")
    if fields.get("url"):
        lines.append(f"  <url>{escape(fields['url'])}</url>")
    lines.append("</movie>")
    return "\n".join(lines) + "\n"


def nfo_path(video: str) -> str:
    return os.path.splitext(video)[0] + ".nfo"


def write_for(video: str, artist_as: str = "performer", overwrite: bool = False, dry_run: bool = False) -> dict:
    """Create the .nfo for one video. Returns {"status": written|exists|empty|error, ...}."""
    target = nfo_path(video)
    if os.path.exists(target) and not overwrite:
        return {"status": "exists", "nfo": target}
    try:
        fields = build(read_tags(video), artist_as)
    except Exception as e:
        return {"status": "error", "error": str(e)}
    if not useful(fields):
        return {"status": "empty"}
    xml = to_xml(fields)
    if not dry_run:
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(xml)
        os.replace(tmp, target)
        try:
            st = os.stat(video)
            os.utime(target, (st.st_atime, st.st_mtime))
        except OSError:
            pass
    return {"status": "written", "nfo": target, "xml": xml, "fields": fields}


def find_videos(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.rsplit(".", 1)[-1].lower() in VIDEO_EXT and not name.startswith("."):
                yield os.path.join(dirpath, name)
