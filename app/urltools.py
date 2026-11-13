"""Link clean-up shared by both engines (standard library only)."""
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Query parameters that are sign-in or access keys. They are removed before a link is written
# into a file, a .nfo or the metadata, so a saved file never carries a working key.
SECRET = {"token", "access_token", "auth", "authorization", "key", "apikey", "api_key", "secure", "signature",
          "sig", "policy", "key-pair-id", "expires", "exp", "hash", "hmac", "jwt", "session", "sid",
          "x-amz-signature", "x-amz-credential", "x-amz-security-token", "x-goog-signature", "st", "e"}


def clean_url(url: str) -> str:
    try:
        u = urlparse(url)
    except Exception:
        return url
    if not u.query:
        return url
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k.lower() not in SECRET]
    return urlunparse(u._replace(query=urlencode(q)))
