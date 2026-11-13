"""Looks at a page that neither engine understands and prints a short summary of where its
video might come from: media links, players, iframes, scripts, API paths and the text around
the id in the link. Runs inside the yt-dlp environment so it uses the same cookies and
browser headers. Usage: python probe.py URL [cookies.txt]
"""
import html
import json
import re
import sys
from urllib.parse import urljoin, urlparse

import yt_dlp

MAX_EACH = 12


def short(s, n=160):
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n] + "..."


def uniq(seq, n=MAX_EACH):
    out = []
    for x in seq:
        if x and x not in out:
            out.append(x)
        if len(out) >= n:
            break
    return out


def main():
    url = sys.argv[1]
    opts = {"quiet": True, "no_warnings": True}
    if len(sys.argv) > 2 and sys.argv[2]:
        opts["cookiefile"] = sys.argv[2]
    out = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            r = ydl.urlopen(url.split("#")[0])
            body = r.read().decode("utf-8", "replace")
            status, final = getattr(r, "status", "?"), r.url
        except Exception as e:
            print(json.dumps({"lines": [f"could not load the page: {e}"]}))
            return
    page = body.replace("\\/", "/")
    host = urlparse(final).netloc
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    out.append(f"page: HTTP {status}, {len(body):,} characters, {final}")
    if title:
        out.append("title: " + short(html.unescape(title.group(1)), 100))
    low = page.lower()
    signed_in = sum(low.count(w) for w in ("logout", "log out", "sign out", "signout", "my account"))
    signed_out = sum(low.count(w) for w in ("login", "log in", "sign in", "signin", "join now", "sign up"))
    out.append(f"sign-in words: signed-in {signed_in}, signed-out {signed_out}")

    media = re.findall(r"""(?:https?:)?//[^\s"'<>()]+?\.(?:m3u8|mpd|mp4|webm|m4v|mov)(?:\?[^\s"'<>()]*)?""", page)
    media += [urljoin(final, m) for m in re.findall(r"""["'](/[^\s"'<>]+?\.(?:m3u8|mpd|mp4|webm))""", page)]
    for m in uniq(media):
        out.append("media: " + short(m.split("?")[0] + ("?..." if "?" in m else ""), 200))
    for m in uniq(re.findall(r"<iframe[^>]+src=[\"']([^\"']+)", page, re.I)):
        out.append("iframe: " + short(urljoin(final, m), 200))
    for m in uniq(re.findall(r"<(?:video|source)[^>]*>", page, re.I)):
        out.append("tag: " + short(m, 200))
    players = re.findall(r"(jwplayer|video\.js|videojs|plyr|flowplayer|hls\.js|shaka|dash\.js|vimeo|wistia|brightcove|bunny|mux\.com|cloudflarestream|vidyard|kaltura|theoplayer|clappr|fluidplayer)", low)
    if players:
        out.append("players mentioned: " + ", ".join(uniq(players)))
    scripts = [urljoin(final, s) for s in re.findall(r"<script[^>]+src=[\"']([^\"']+)", page, re.I)]
    for s in uniq([s for s in scripts if not re.search(r"google|gtag|facebook|analytics|hotjar|cookie|consent|recaptcha", s)]):
        out.append("script: " + short(s.split("?")[0], 200))
    for m in uniq(re.findall(r"""["'`]((?:https?://[^"'`\s]+)?/(?:api|ajax|graphql|v\d|wp-json|player|stream|video|media|play)[/?][^"'`\s<>]{0,120})""", page, re.I), 15):
        out.append("endpoint: " + short(m, 200))
    for name in ("__NEXT_DATA__", "__NUXT__", "__INITIAL_STATE__", "__APOLLO_STATE__", "__PRELOADED_STATE__", "dataLayer"):
        if name in page:
            out.append(f"data blob: {name}")
    attrs = re.findall(r"\s(data-[\w-]*(?:video|play|src|stream|media|file|id)[\w-]*)=[\"']([^\"']{0,150})", page, re.I)
    for k, v in uniq(attrs, 15):
        out.append(f"attribute: {k}={short(v, 120)}")
    frag = urlparse(url).fragment
    ids = uniq(re.findall(r"\d{3,}", urlparse(url).path + " " + frag), 3)
    for i in ids:
        hits = [m.start() for m in re.finditer(re.escape(i), page)][:4]
        out.append(f"id {i}: found {page.count(i)} time(s)")
        for h in hits:
            out.append(f"  near {i}: " + short(page[max(0, h - 90):h + 110], 220))
    print(json.dumps({"lines": out, "host": host}))


if __name__ == "__main__":
    main()
