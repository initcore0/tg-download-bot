"""URL helpers: extraction from message text, platform detection, normalization.

Implemented by Agent A (M1). Public signatures are FROZEN.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# http(s) URLs. We deliberately keep the character class permissive, then trim
# trailing punctuation that is far more likely to be prose than part of the URL.
_URL_RE = re.compile(r"https?://[^\s<>\"'\\]+", re.IGNORECASE)

# Trailing characters that usually belong to the surrounding sentence, not the URL.
_TRAILING_PUNCT = ".,;:!?'\""
_CLOSERS = {")": "(", "]": "[", "}": "{"}

# Tracking / non-identifying query parameters stripped during normalization.
_DROP_PARAMS_EXACT = {
    "si",
    "feature",
    "igsh",
    "igshid",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "ref_url",
    "referrer",
    "share_id",
    "source",
    "spm",
    "s",
    "t",  # youtube/twitter start-time; irrelevant for identity
    "_r",
    "_t",
    "is_from_webapp",
    "sender_device",
    "web_id",
    "app",
    "cxt",
    "twclid",
    "rdid",
    "share_url",
}
_DROP_PARAM_PREFIXES = ("utm_",)

_PLATFORM_BY_HOST: dict[str, str] = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "youtube-nocookie.com": "youtube",
    "tiktok.com": "tiktok",
    "vt.tiktok.com": "tiktok",
    "vm.tiktok.com": "tiktok",
    "instagram.com": "instagram",
    "instagr.am": "instagram",
    "ddinstagram.com": "instagram",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "t.co": "twitter",
    "fxtwitter.com": "twitter",
    "vxtwitter.com": "twitter",
    "twitch.tv": "twitch",
    "clips.twitch.tv": "twitch",
    "pinterest.com": "pinterest",
    "pin.it": "pinterest",
    "reddit.com": "reddit",
    "old.reddit.com": "reddit",
    "redd.it": "reddit",
}


def _host_of(url: str) -> str:
    """Lowercased hostname without a leading ``www.`` (empty when unparseable)."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host.removeprefix("www.")


def _trim_trailing(url: str) -> str:
    """Strip sentence punctuation and unbalanced closing brackets from a URL tail."""
    while url:
        last = url[-1]
        if last in _TRAILING_PUNCT:
            url = url[:-1]
            continue
        opener = _CLOSERS.get(last)
        if opener is not None and url.count(last) > url.count(opener):
            url = url[:-1]
            continue
        break
    return url


def extract_urls(text: str) -> list[str]:
    """Return all http(s) URLs found in `text`, in order of appearance."""
    if not text:
        return []
    urls: list[str] = []
    for match in _URL_RE.finditer(text):
        candidate = _trim_trailing(match.group(0))
        # Require a real host after the scheme separator.
        bare_scheme = candidate.lower().rstrip("/") in ("http:/", "https:/")
        if candidate and not bare_scheme and _host_of(candidate):
            urls.append(candidate)
    return urls


def detect_platform(url: str) -> str:
    """Map a URL to a slug: youtube|tiktok|instagram|twitter|twitch|pinterest|reddit|other."""
    host = _host_of(url)
    if not host:
        return "other"
    if host in _PLATFORM_BY_HOST:
        return _PLATFORM_BY_HOST[host]
    # Match subdomains: m.youtube.com, www.clips.twitch.tv, pinterest.co.uk ...
    for known, slug in _PLATFORM_BY_HOST.items():
        if host.endswith("." + known):
            return slug
    # Country TLD variants such as pinterest.de / pinterest.co.uk.
    root = host.split(".")[0]
    for known, slug in _PLATFORM_BY_HOST.items():
        if known.split(".")[0] == root and slug in {"pinterest", "youtube", "tiktok"}:
            return slug
    return "other"


def _clean_query(query: str, *, keep: set[str] | None = None) -> str:
    """Drop tracking params, keeping order; `keep` forces retention of listed keys."""
    keep = keep or set()
    pairs = [
        (k, v)
        for k, v in parse_qsl(query, keep_blank_values=True)
        if k in keep
        or (
            k.lower() not in _DROP_PARAMS_EXACT
            and not any(k.lower().startswith(p) for p in _DROP_PARAM_PREFIXES)
        )
    ]
    return urlencode(pairs)


def normalize_url(url: str) -> str:
    """Canonical form used as the future dedup/audit key.

    Lowercase host, drop fragments and tracking params (utm_*, si, feature, igsh, ...),
    expand youtu.be/<id> to youtube.com/watch?v=<id>, strip trailing slash.
    """
    if not url:
        return ""
    raw = url.strip()
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw

    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if not host:
        return raw
    host = host.removeprefix("www.")

    path = parts.path or ""
    query = parts.query

    # youtu.be/<id> -> youtube.com/watch?v=<id>
    if host == "youtu.be":
        video_id = path.strip("/").split("/")[0]
        if video_id:
            existing = _clean_query(query)
            host = "youtube.com"
            path = "/watch"
            merged = [("v", video_id)]
            merged.extend(parse_qsl(existing, keep_blank_values=True))
            query = urlencode(merged)
        else:
            host = "youtube.com"
            path = ""
            query = _clean_query(query)
    else:
        if host == "youtube-nocookie.com":
            host = "youtube.com"
        # `v` must survive on youtube watch URLs even though short params are dropped.
        keep = {"v"} if host == "youtube.com" else set()
        query = _clean_query(query, keep=keep)

    # Preserve a non-default port if present.
    netloc = host
    if parts.port:
        default = {"http": 80, "https": 443}.get(scheme)
        if parts.port != default:
            netloc = f"{host}:{parts.port}"

    if path != "/":
        path = path.rstrip("/")

    normalized = urlunsplit((scheme, netloc, path, query, ""))
    if normalized.endswith("/") and not path.endswith("//"):
        # urlunsplit keeps a lone "/" path; drop it for a stable key.
        normalized = normalized.rstrip("/")
    return normalized


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for addresses an outbound fetch should never reach (SSRF guard)."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        # IPv4-mapped IPv6 (e.g. ::ffff:169.254.169.254) must be unwrapped and rechecked.
        or (getattr(ip, "ipv4_mapped", None) is not None and _is_blocked_ip(ip.ipv4_mapped))
    )


def is_safe_public_url(url: str, *, resolver=socket.getaddrinfo) -> bool:
    """Reject URLs that resolve to private/link-local/loopback/reserved addresses.

    Prevents the yt-dlp generic extractor from being turned into a request forwarder
    against internal services (cloud metadata at 169.254.169.254, LAN hosts, localhost).
    Only http/https with a resolvable, public host passes. DNS failures fail closed.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme.lower() not in ("http", "https"):
        return False
    host = parts.hostname
    if not host:
        return False

    # A literal IP in the URL: check it directly.
    try:
        return not _is_blocked_ip(ipaddress.ip_address(host))
    except ValueError:
        pass  # hostname, not an IP — resolve it below.

    try:
        infos = resolver(host, parts.port or (443 if parts.scheme == "https" else 80),
                         proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return False  # unresolvable -> fail closed
    if not infos:
        return False

    for info in infos:
        sockaddr = info[4]
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return False
        if _is_blocked_ip(ip):
            return False  # any resolved address in a blocked range -> reject
    return True
