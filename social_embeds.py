"""Rich previews for social media links -- X/Twitter, Reddit, Instagram and
TikTok -- so people can see a post without opening it. Discord's own
previews for these sites are often missing or bare, and never play
Instagram or TikTok videos.

Each site needs a different way in, none of them needing an API key:
  - X/Twitter: FxTwitter's public API (api.fxtwitter.com).
  - TikTok: tikwm's public API, which also gives a watermark-free video.
  - Instagram: instagram.com itself, whose post pages carry full Open
    Graph tags (author, caption, likes, image, video) for link-preview
    crawlers like Discord's.
  - Reddit: vxreddit, a preview mirror. Reddit now sends logged-out
    visitors to a login page and its own preview tags carry no author or
    stats, so reddit.com is only the fallback.

Instagram and Reddit only show those tags to crawlers they recognise, so
requests to them identify as Discord's link-preview bot -- the same fetch
Discord makes when the link is posted.

bot.py turns a Post into the embed (and, for videos small enough to
upload, a playable attachment).
"""
import datetime
import html
import logging
import re
import threading
import time
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; DJ-Shinx-Bot/1.0; +https://github.com/Ale0902/DJ-Shinx-main)"
PREVIEW_CRAWLER_AGENT = "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"
FETCH_TIMEOUT = 10

# At most this many links in one message get a preview.
MAX_LINKS_PER_MESSAGE = 3

CACHE_TTL_SECONDS = 600


@dataclass
class Post:
    platform: str                   # key into PLATFORMS
    url: str                        # the post, for "Open in ..."
    author: str                     # "Adam Schefter (@AdamSchefter)", or a Reddit post's title
    author_url: str | None = None
    avatar: str | None = None
    subtitle: str | None = None     # e.g. "r/okbuddycinephile • u/someone", already linked
    text: str | None = None
    image: str | None = None
    video: str | None = None        # direct .mp4, attached when small enough
    more_media: int = 0             # media beyond the first, e.g. the rest of a carousel
    stats: list[tuple[str, int]] = field(default_factory=list)
    posted: int | None = None       # unix time
    posted_date_only: bool = False  # Instagram only gives the day
    sensitive: bool = False


# name, colour, icon for each platform. Icons come from Google's favicon
# service, which always answers with a PNG -- some of these sites' own
# icons are .ico or SVG, which Discord won't show in an embed.
PLATFORMS = {
    'x': ("X", 0x0F1419, "x.com"),  # pure black (0) reads as "no colour" to Discord
    'reddit': ("Reddit", 0xFF4500, "reddit.com"),
    'instagram': ("Instagram", 0xE1306C, "instagram.com"),
    'tiktok': ("TikTok", 0x25F4EE, "tiktok.com"),
}


def platform_name(post: Post) -> str:
    return PLATFORMS[post.platform][0]


def platform_color(post: Post) -> int:
    return PLATFORMS[post.platform][1]


def platform_icon(post: Post) -> str:
    return f"https://www.google.com/s2/favicons?domain={PLATFORMS[post.platform][2]}&sz=64"


#=========================--FINDING LINKS--==================================#

# A link wrapped in <...> is one its poster asked Discord not to preview,
# and one inside ||...|| is a spoiler -- both are left alone.
_SKIPPED_LINK_RE = re.compile(r"<https?://[^>\s]+>|\|\|.*?\|\|", re.S)

_LINK_PATTERNS = [
    ('x', re.compile(
        r"https?://(?:www\.|mobile\.)?(?:twitter|x|fxtwitter|vxtwitter|fixupx|fixvx)\.com/"
        r"(?P<user>[A-Za-z0-9_]{1,15})/status(?:es)?/(?P<id>\d+)")),
    ('reddit', re.compile(
        r"https?://(?:(?:www|old|new|np|m)\.)?reddit\.com/r/(?P<sub>[A-Za-z0-9_]+)/(?:comments|s)/(?P<id>[A-Za-z0-9]+)"
        r"|https?://redd\.it/(?P<short>[A-Za-z0-9]+)")),
    ('instagram', re.compile(
        r"https?://(?:www\.)?instagram\.com/(?:[A-Za-z0-9_.]+/)?(?P<kind>p|reel|reels|tv)/(?P<code>[A-Za-z0-9_-]+)")),
    ('tiktok', re.compile(
        r"https?://(?:(?:www|m)\.)?tiktok\.com/@[A-Za-z0-9_.]+/(?:video|photo)/\d+"
        r"|https?://(?:vm|vt)\.tiktok\.com/[A-Za-z0-9]+"
        r"|https?://(?:www\.)?tiktok\.com/t/[A-Za-z0-9]+")),
]


def find_links(text: str) -> list[tuple[str, re.Match]]:
    """(platform, match) for each supported post link in a message, in the
    order they appear, without repeats, at most MAX_LINKS_PER_MESSAGE."""
    text = _SKIPPED_LINK_RE.sub(" ", text)
    found = []
    for platform, pattern in _LINK_PATTERNS:
        found.extend((m.start(), platform, m) for m in pattern.finditer(text))
    seen, links = set(), []
    for _, platform, match in sorted(found, key=lambda f: f[0]):
        if match.group(0) not in seen:
            seen.add(match.group(0))
            links.append((platform, match))
    return links[:MAX_LINKS_PER_MESSAGE]


#=========================--FETCHING--=======================================#

_cache: dict[str, tuple[float, Post | None]] = {}
_cache_lock = threading.Lock()


def _get(url: str, *, crawler: bool = False, **kwargs) -> requests.Response | None:
    agent = PREVIEW_CRAWLER_AGENT if crawler else USER_AGENT
    try:
        response = requests.get(url, headers={'User-Agent': agent}, timeout=FETCH_TIMEOUT, **kwargs)
    except requests.RequestException as e:
        logger.debug(f"social_embeds: {url} failed: {e}")
        return None
    if response.status_code != 200:
        logger.debug(f"social_embeds: {url} returned HTTP {response.status_code}")
        return None
    return response


def _meta_tags(page: str) -> dict[str, str]:
    """Open Graph / Twitter card tags, first value of each."""
    tags = {}
    for tag in re.findall(r"<meta\s[^>]*>", page):
        name = re.search(r'(?:property|name)="((?:og|twitter|article):[^"]+)"', tag)
        content = re.search(r'content="([^"]*)"', tag)
        if name and content:
            tags.setdefault(name.group(1), html.unescape(content.group(1)))
    return tags


def _fetch_x(match: re.Match) -> Post | None:
    response = _get(f"https://api.fxtwitter.com/{match['user']}/status/{match['id']}")
    tweet = (response.json() if response else {}).get('tweet')
    if not tweet:
        return None
    author = tweet.get('author') or {}
    media = tweet.get('media') or {}
    photos, videos = media.get('photos') or [], media.get('videos') or []
    first_video = videos[0] if videos else None
    return Post(
        platform='x',
        url=f"https://x.com/{author.get('screen_name', match['user'])}/status/{match['id']}",
        author=f"{author.get('name', match['user'])} (@{author.get('screen_name', match['user'])})",
        author_url=f"https://x.com/{author.get('screen_name', match['user'])}",
        avatar=author.get('avatar_url'),
        text=tweet.get('text') or None,
        image=photos[0]['url'] if photos else (first_video or {}).get('thumbnail_url'),
        video=(first_video or {}).get('url'),
        more_media=max(len(media.get('all') or []) - 1, 0),
        stats=[(emoji, tweet.get(key)) for emoji, key in
               (("💬", 'replies'), ("🔁", 'retweets'), ("❤️", 'likes'), ("👁️", 'views'))
               if tweet.get(key) is not None],
        posted=tweet.get('created_timestamp'),
        sensitive=bool(tweet.get('possibly_sensitive')),
    )


def _fetch_reddit(match: re.Match) -> Post | None:
    if match['short'] or '/s/' in match.group(0):
        # redd.it and share (/s/) links only say which post once followed.
        response = _get(match.group(0), crawler=True)
        canonical = re.search(r"/r/([A-Za-z0-9_]+)/comments/([A-Za-z0-9]+)", response.url if response else "")
        if not canonical:
            return None
        sub, post_id = canonical.groups()
    else:
        sub, post_id = match['sub'], match['id']
    url = f"https://www.reddit.com/r/{sub}/comments/{post_id}/"

    response = _get(f"https://vxreddit.com/r/{sub}/comments/{post_id}/", crawler=True)
    tags = _meta_tags(response.text) if response else {}
    # og:site_name reads e.g. "u/someone on r/sub - ⬆️ 11257 | 💬 933"
    byline = re.match(r"u/(\S+) on r/(\S+) - ⬆️ (\d+) \| 💬 (\d+)", tags.get('og:site_name', ''))
    if tags.get('og:title') and byline:
        user, sub, score, comments = byline.groups()
        return Post(
            platform='reddit', url=url, author=tags['og:title'], author_url=url,
            subtitle=f"[r/{sub}](https://www.reddit.com/r/{sub}/) • [u/{user}](https://www.reddit.com/user/{user}/)",
            text=tags.get('og:description') or None,
            image=tags.get('og:image') or None,
            stats=[("⬆️", int(score)), ("💬", int(comments))],
        )

    # Reddit's own tags: the title and a generated preview card, nothing more.
    response = _get(url, crawler=True)
    tags = _meta_tags(response.text) if response else {}
    title = re.sub(r"^From the \S+ community on Reddit: ", "", tags.get('og:title', ''))
    if not title:
        return None
    return Post(
        platform='reddit', url=url, author=title, author_url=url,
        subtitle=f"[r/{sub}](https://www.reddit.com/r/{sub}/)",
        image=tags.get('og:image') or None,
    )


def _fetch_instagram(match: re.Match) -> Post | None:
    kind = 'reel' if match['kind'] in ('reel', 'reels') else match['kind']
    url = f"https://www.instagram.com/{kind}/{match['code']}/"
    response = _get(url, crawler=True)
    tags = _meta_tags(response.text) if response else {}
    if not tags.get('og:image'):
        return None  # private, deleted, or Instagram didn't serve the tags

    # twitter:title: "Name (@handle) • Instagram video"
    who = re.match(r"(.*?) \(@([A-Za-z0-9_.]+)\)", tags.get('twitter:title', ''))
    name, handle = who.groups() if who else (None, None)
    # og:description: '3,695 likes, 70 comments - handle on June 16, 2015: "caption". '
    summary = re.match(
        r'(?:([\d,.KMkm]+) likes?, )?(?:([\d,.KMkm]+) comments? - )?([A-Za-z0-9_.]+) on ([A-Z][a-z]+ \d{1,2}, \d{4}): "(.*)"\.?\s*$',
        tags.get('og:description', ''), re.S,
    )
    posted, caption, stats = None, None, []
    if summary:
        likes, comments, handle_from_summary, day, caption = summary.groups()
        handle = handle or handle_from_summary
        stats = [(emoji, _count(value)) for emoji, value in (("❤️", likes), ("💬", comments)) if value]
        try:
            posted = int(datetime.datetime.strptime(day, "%B %d, %Y").replace(tzinfo=datetime.timezone.utc).timestamp())
        except ValueError:
            pass
    if not caption:
        caption = (re.search(r'on Instagram: "(.*)"$', tags.get('og:title', ''), re.S) or [None, None])[1]

    author = f"{name} (@{handle})" if name and handle else (f"@{handle}" if handle else "Instagram")
    return Post(
        platform='instagram', url=url, author=author,
        author_url=f"https://www.instagram.com/{handle}/" if handle else None,
        text=caption or None,
        image=tags['og:image'],
        video=tags.get('og:video:secure_url') or tags.get('og:video') or None,
        stats=stats, posted=posted, posted_date_only=True,
    )


def _count(text: str) -> int:
    """"3,695" -> 3695, "1.2M" -> 1200000."""
    text = text.replace(',', '').strip()
    scale = {'k': 1_000, 'm': 1_000_000}.get(text[-1:].lower(), 1)
    try:
        return int(float(text.rstrip('KMkm')) * scale)
    except ValueError:
        return 0


def _fetch_tiktok(match: re.Match) -> Post | None:
    response = _get("https://www.tikwm.com/api/", params={'url': match.group(0)})
    payload = response.json() if response else {}
    data = payload.get('data') if payload.get('code') == 0 else None
    if not data:
        return None
    author = data.get('author') or {}
    handle = author.get('unique_id', '')
    images = data.get('images') or []  # a photo post instead of a video
    return Post(
        platform='tiktok',
        url=f"https://www.tiktok.com/@{handle}/{'photo' if images else 'video'}/{data.get('id')}",
        author=f"{author.get('nickname') or handle} (@{handle})",
        author_url=f"https://www.tiktok.com/@{handle}",
        avatar=author.get('avatar'),
        text=data.get('title') or None,
        image=images[0] if images else (data.get('origin_cover') or data.get('cover')),
        video=None if images else data.get('play'),
        more_media=max(len(images) - 1, 0),
        stats=[(emoji, data.get(key)) for emoji, key in
               (("▶️", 'play_count'), ("❤️", 'digg_count'), ("💬", 'comment_count'), ("🔁", 'share_count'))
               if data.get(key) is not None],
        posted=data.get('create_time'),
    )


_FETCHERS = {'x': _fetch_x, 'reddit': _fetch_reddit, 'instagram': _fetch_instagram, 'tiktok': _fetch_tiktok}


def fetch(platform: str, match: re.Match) -> Post | None:
    """The post a link points to, or None if it can't be previewed
    (deleted, private, or the source is down). Cached for a while, so a
    link posted twice, or in several servers, is only fetched once."""
    key = match.group(0)
    with _cache_lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]
    try:
        post = _FETCHERS[platform](match)
    except Exception as e:  # a source changing its format must not break the listener
        logger.warning(f"social_embeds: couldn't preview {key}: {e}")
        post = None
    with _cache_lock:
        now = time.monotonic()
        for stale in [k for k, (at, _) in _cache.items() if now - at >= CACHE_TTL_SECONDS]:
            del _cache[stale]
        _cache[key] = (now, post)
    return post


def download_video(url: str, max_bytes: int) -> bytes | None:
    """The video's bytes if it's no bigger than max_bytes (Discord's upload
    limit), else None -- the embed then shows its cover image instead."""
    try:
        with requests.get(url, headers={'User-Agent': PREVIEW_CRAWLER_AGENT}, timeout=FETCH_TIMEOUT, stream=True) as response:
            if response.status_code != 200:
                return None
            if int(response.headers.get('Content-Length') or 0) > max_bytes:
                return None
            data = bytearray()
            for chunk in response.iter_content(64 * 1024):
                data.extend(chunk)
                if len(data) > max_bytes:
                    return None
            return bytes(data) or None
    except requests.RequestException as e:
        logger.debug(f"social_embeds: video download failed: {e}")
        return None


#=========================--FORMATTING--=====================================#

def short_count(n: int) -> str:
    """452796 -> "452.8K", 2200 -> "2.2K", 65 -> "65"."""
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= limit:
            return f"{n / limit:.1f}".rstrip('0').rstrip('.') + suffix
    return str(n)


def description(post: Post, media_hidden: bool = False) -> str:
    """The embed's body: byline, text, stats, then a small footer line with
    the post time (shown in each reader's own timezone) and a link."""
    parts = []
    if post.subtitle:
        parts.append(post.subtitle)
    if post.text:
        text = post.text if len(post.text) <= 1500 else post.text[:1500].rstrip() + "…"
        parts.append(text)
    if media_hidden:
        parts.append("*⚠️ Marked sensitive — media hidden outside age-restricted channels. Open the post to view it.*")
    if post.more_media:
        parts.append(f"*+{post.more_media} more*")
    if post.stats:
        parts.append("  ".join(f"{emoji} **{short_count(n)}**" for emoji, n in post.stats))
    footer = [platform_name(post)]
    if post.posted:
        footer.append(f"Posted <t:{post.posted}:{'D' if post.posted_date_only else 'f'}>")
    footer.append(f"[Open in {platform_name(post)}]({post.url})")
    parts.append("-# " + " • ".join(footer))
    return "\n\n".join(parts)
