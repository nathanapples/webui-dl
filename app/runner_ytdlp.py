"""Runs inside the yt-dlp virtualenv. Reads a JSON spec on stdin.

spec = {"mode": "preview" | "download", "url": str, "args": [yt-dlp CLI args],
        "templates": {field: "{other|fallback} text"},   # from a profile
        "overrides": {field: value},                      # literal values typed in the UI
        "limit": int}

Templates are rendered against each item's metadata, then literal overrides are applied
on top. Both happen before the file name is built and before --embed-metadata runs, so
they change the file name and the embedded tags.
"""
import json
import re
import sys

import yt_dlp
from yt_dlp.postprocessor.common import PostProcessor

LIST_FIELDS = {"tags", "categories", "artists", "genres", "composers", "album_artists", "cast"}
INT_FIELDS = {"track_number", "disc_number", "release_year", "season_number", "episode_number"}
EMBED_FIELDS = ("title", "artist", "album", "album_artist", "genre", "date", "description",
                "comment", "track", "composer", "synopsis")
SIMPLE = (str, int, float, bool, type(None))
TPL = re.compile(r"\{([^{}]+)\}")


def render(tpl, info):
    """Small template language matching gallery-dl's basics: {field} and {a|b|c} fallbacks."""
    if not isinstance(tpl, str):
        return tpl
    s = tpl.replace("{{", "\x00").replace("}}", "\x01")

    def rep(m):
        for name in m.group(1).split("|"):
            v = info.get(name.strip())
            if v not in (None, "", []):
                return ", ".join(map(str, v)) if isinstance(v, list) else str(v)
        return ""

    return TPL.sub(rep, s).replace("\x00", "{").replace("\x01", "}")


def coerce(ov):
    out = {}
    for k, v in (ov or {}).items():
        if k in LIST_FIELDS and isinstance(v, str):
            v = [x.strip() for x in v.split(",") if x.strip()]
        elif k in INT_FIELDS and isinstance(v, str) and v.strip().isdigit():
            v = int(v)
        out[k] = v
        # yt-dlp's metadata embedder prefers meta_* fields; set them so the value wins there too.
        if k in EMBED_FIELDS:
            out[f"meta_{k}"] = ", ".join(map(str, v)) if isinstance(v, list) else v
        if k in ("upload_date", "release_date"):
            out["meta_date"] = v
    return out


EMBED_TAGS = {}   # profile's "file tag = template" lines, set from the spec
SOURCE_URL = ""


def effective(info, templates, overrides):
    vals = {k: render(t, info) for k, t in (templates or {}).items()}
    vals.update(overrides or {})
    out = coerce(vals)
    ctx = {**info, **{k: v for k, v in out.items() if not k.startswith("meta_")},
           "source_url": SOURCE_URL or info.get("webpage_url") or ""}
    # Tags also go into the "keywords" tag (yt-dlp itself only puts them into genre).
    tags = ctx.get("tags")
    if tags and "meta_keywords" not in info:
        out["meta_keywords"] = ", ".join(map(str, tags)) if isinstance(tags, (list, tuple)) else str(tags)
    # The profile's file tags. A value that comes out empty leaves yt-dlp's own tag alone.
    for tag, tmpl in EMBED_TAGS.items():
        value = render(tmpl, ctx).strip()
        if value and value not in ("None", "[]"):
            out[f"meta_{tag}"] = value
    return out


def clean(d):
    out = {}
    for k, v in (d or {}).items():
        if k.startswith("_") or k.startswith("meta_") or k in ("formats", "thumbnails", "http_headers"):
            continue
        if isinstance(v, SIMPLE):
            out[k] = v
        elif isinstance(v, list) and len(v) <= 100 and all(isinstance(x, SIMPLE) for x in v):
            out[k] = v
    return out


HEAVY = ("formats", "automatic_captions", "heatmap", "requested_formats", "requested_downloads")


def rawify(d):
    return {k: v for k, v in (d or {}).items() if k not in HEAVY}


class Collector:
    """yt-dlp logger that keeps messages for the preview log."""
    def __init__(self, verbose=False):
        self.lines, self.verbose = [], verbose

    def debug(self, msg):
        if self.verbose or not msg.startswith("[debug] "):
            self.lines.append(msg)

    def info(self, msg):
        self.lines.append(msg)

    def warning(self, msg):
        self.lines.append("WARNING: " + msg)

    def error(self, msg):
        self.lines.append(msg)


def embedded_tags(ydl, info):
    """The exact tags yt-dlp's --embed-metadata would write for this item."""
    try:
        from yt_dlp.postprocessor.ffmpeg import FFmpegMetadataPP
        out = {}
        for opt in FFmpegMetadataPP(ydl)._get_metadata_opts(info):
            if opt[0] == "-metadata":
                k, _, v = opt[1].partition("=")
                out[k] = v
        return out
    except Exception as e:
        return {"(error)": str(e)}


class NfoPP(PostProcessor):
    """Writes a .nfo next to the finished video from the tags now inside it."""
    def __init__(self, artist_as):
        super().__init__(None)
        self.artist_as = artist_as

    def run(self, info):
        path = info.get("filepath")
        try:
            import nfo
            if path and path.rsplit(".", 1)[-1].lower() in nfo.VIDEO_EXT:
                r = nfo.write_for(path, self.artist_as, overwrite=True)
                if r["status"] == "written":
                    self.to_screen(f"Wrote {r['nfo']}")
        except Exception as e:
            self.report_warning(f"Could not write .nfo: {e}")
        return [], info


class OverridePP(PostProcessor):
    def __init__(self, templates, overrides):
        super().__init__(None)
        self.templates, self.overrides = templates, overrides

    def run(self, info):
        info.update(effective(info, self.templates, self.overrides))
        return [], info


def preview(spec, opts):
    tpl, ov = spec.get("templates") or {}, spec.get("overrides") or {}
    limit = int(spec.get("limit") or 50)
    log = Collector(bool(spec.get("verbose")))
    spec["_log"] = log
    opts.update(
        quiet=False, no_warnings=False, noprogress=True, skip_download=True, logger=log,
        verbose=bool(spec.get("verbose")),
        extract_flat="in_playlist", playlistend=limit, postprocessors=[], ignoreerrors=False,
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(spec["url"], download=False)
        if not info:
            raise ValueError("No metadata returned for this link")
        info = ydl.sanitize_info(info)
        is_list = info.get("_type") in ("playlist", "multi_video") or "entries" in info
        entries = [e for e in (info.get("entries") or []) if e] if is_list else []
        top_vals = effective(info, tpl, ov)
        top = clean({**info, **top_vals})
        items = []
        for e in entries:
            ctx = {**info, **e, "playlist": info.get("title"), "playlist_id": info.get("id")}
            vals = effective(ctx, tpl, ov)
            fn_info = {**ctx, **vals}
            fn_info.setdefault("ext", "NA")
            try:
                fn = ydl.prepare_filename(fn_info)
            except Exception:
                fn = None
            em = clean({**e, **vals})
            items.append({"kind": "entry", "url": e.get("url") or e.get("webpage_url"),
                          "title": em.get("title") or e.get("id"), "filename": fn, "meta": em,
                          "tags": embedded_tags(ydl, fn_info) if spec.get("embed") else None, "media": True,
                          "raw": rawify(e) if len(items) < 20 else None})
        filename = None
        if not is_list:
            try:
                filename = ydl.prepare_filename({**info, **top_vals})
            except Exception:
                pass
        return {
            "engine": "yt-dlp",
            "site": info.get("extractor_key") or info.get("extractor"),
            "title": top.get("title") or "",
            "thumbnail": info.get("thumbnail"),
            "common": top,
            "raw_common": rawify({**info, "entries": None} if is_list else info),
            "items": items,
            "count": len(items) if is_list else 1,
            "truncated": is_list and len(items) >= limit,
            "filename": filename,
            "tags": embedded_tags(ydl, {**info, **top_vals}) if (spec.get("embed") and not is_list) else None,
            "warnings": [],
            "log": log.lines[-400:],
        }


def main():
    global EMBED_TAGS, SOURCE_URL
    spec = json.load(sys.stdin)
    EMBED_TAGS = spec.get("embed_tags") or {}
    SOURCE_URL = spec.get("url") or ""
    opts = yt_dlp.parse_options([*spec.get("args", []), spec["url"]]).ydl_opts
    if spec["mode"] == "preview":
        try:
            print(json.dumps(preview(spec, opts), default=str))
        except Exception as e:
            msg = str(e).replace("\x1b[0;31mERROR:\x1b[0m ", "").replace("ERROR: ", "")
            lg = spec.get("_log")
            print(json.dumps({"error": msg, "log": lg.lines[-400:] if lg else []}))
        return 0
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.add_post_processor(OverridePP(spec.get("templates"), spec.get("overrides")), when="pre_process")
        if spec.get("write_nfo"):
            ydl.add_post_processor(NfoPP(spec["write_nfo"]), when="after_move")
        return ydl.download([spec["url"]])


if __name__ == "__main__":
    sys.exit(main())
