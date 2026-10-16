"""Parse the public web preview of a Telegram channel (https://t.me/s/<channel>)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

TME_HOSTS = {"t.me", "telegram.me", "www.t.me", "telegram.dog"}
MEDIA_CLASSES = {
    "photo": "tgme_widget_message_photo_wrap",
    "video": "tgme_widget_message_video_player",
    "roundvideo": "tgme_widget_message_roundvideo_player",
    "voice": "tgme_widget_message_voice_player",
    "document": "tgme_widget_message_document_wrap",
    "poll": "tgme_widget_message_poll",
    "sticker": "tgme_widget_message_sticker_wrap",
}


@dataclass
class ChannelInfo:
    username: str
    title: str = ""
    description: str = ""
    subscribers: int | None = None
    verified: bool = False
    counters: dict[str, str] = field(default_factory=dict)


@dataclass
class Post:
    id: int
    date: datetime | None
    text: str = ""
    views: int | None = None
    edited: bool = False
    author: str | None = None
    forwarded_from: str | None = None
    forwarded_url: str | None = None
    reply_to: str | None = None
    links: list[str] = field(default_factory=list)
    media: list[str] = field(default_factory=list)
    reactions: int = 0


def parse_count(value: str | None) -> int | None:
    """'9.31M' -> 9310000, '13.7K' -> 13700, '538' -> 538."""
    if not value:
        return None
    value = value.strip().replace(" ", "").replace(",", ".")
    match = re.fullmatch(r"([\d.]+)([KMB]?)", value, re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[match.group(2).upper()]
    return round(number * multiplier)


def tme_channel(url: str) -> str | None:
    """Return the channel username a t.me link points to, or None."""
    parsed = urlparse(url if "//" in url else f"https://{url}")
    if parsed.hostname not in TME_HOSTS:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if parts and parts[0] == "s":
        parts = parts[1:]
    if not parts or parts[0] in {"joinchat", "addstickers", "share", "proxy", "iv"}:
        return None
    name = parts[0].lstrip("+@")
    if parts[0].startswith("+") or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", name):
        return None
    return name.lower()


def parse_channel(html: str, username: str) -> ChannelInfo:
    soup = BeautifulSoup(html, "html.parser")
    info = ChannelInfo(username=username)
    header = soup.select_one(".tgme_channel_info")
    if header is None:
        return info
    title = header.select_one(".tgme_channel_info_header_title")
    info.title = title.get_text(" ", strip=True) if title else ""
    info.verified = header.select_one(".verified-icon") is not None
    description = header.select_one(".tgme_channel_info_description")
    info.description = description.get_text("\n", strip=True) if description else ""
    for counter in header.select(".tgme_channel_info_counter"):
        value = counter.select_one(".counter_value")
        kind = counter.select_one(".counter_type")
        if value and kind:
            info.counters[kind.get_text(strip=True)] = value.get_text(strip=True)
    for key in ("subscribers", "subscriber", "members"):
        if key in info.counters:
            info.subscribers = parse_count(info.counters[key])
            break
    return info


def _href(tag: Tag | None) -> str | None:
    if tag is None:
        return None
    href = tag.get("href")
    return href if isinstance(href, str) else None


def _parse_post(node: Tag) -> Post | None:
    data_post = node.get("data-post")
    if not isinstance(data_post, str) or "/" not in data_post:
        return None
    try:
        post_id = int(data_post.rsplit("/", 1)[1])
    except ValueError:
        return None

    time_tag = node.select_one(".tgme_widget_message_date time[datetime]")
    date = None
    if time_tag is not None:
        try:
            date = datetime.fromisoformat(str(time_tag["datetime"]))
        except ValueError:
            date = None

    text_tag = node.select_one(".tgme_widget_message_text")
    text = text_tag.get_text("\n", strip=True) if text_tag else ""

    links: list[str] = []
    if text_tag is not None:
        links += [h for a in text_tag.select("a[href]") if (h := _href(a))]
    links += [h for a in node.select("a.tgme_widget_message_link_preview") if (h := _href(a))]

    forwarded_from = forwarded_url = None
    fwd = node.select_one(".tgme_widget_message_forwarded_from_name")
    if fwd is not None:
        forwarded_from = fwd.get_text(" ", strip=True) or "(скрытый источник)"
        forwarded_url = _href(fwd)

    author_tag = node.select_one(".tgme_widget_message_from_author")
    meta = node.select_one(".tgme_widget_message_meta")
    reactions = 0
    for reaction in node.select(".tgme_widget_message_reactions .tgme_reaction"):
        # The emoji sits inside the same span, so take only the trailing number.
        if match := re.search(r"[\d.,]+[KMB]?$", reaction.get_text(strip=True), re.IGNORECASE):
            reactions += parse_count(match.group()) or 0

    return Post(
        id=post_id,
        date=date,
        text=text,
        views=parse_count(
            v.get_text(strip=True) if (v := node.select_one(".tgme_widget_message_views")) else None
        ),
        edited=bool(meta and "edited" in meta.get_text()),
        author=author_tag.get_text(strip=True) if author_tag else None,
        forwarded_from=forwarded_from,
        forwarded_url=forwarded_url,
        reply_to=_href(node.select_one("a.tgme_widget_message_reply")),
        links=list(dict.fromkeys(links)),
        media=[kind for kind, cls in MEDIA_CLASSES.items() if node.select_one(f".{cls}")],
        reactions=reactions,
    )


def parse_posts(html: str) -> list[Post]:
    soup = BeautifulSoup(html, "html.parser")
    posts = [
        p for node in soup.select(".tgme_widget_message[data-post]") if (p := _parse_post(node))
    ]
    return sorted(posts, key=lambda p: p.id)


def is_public_preview(html: str) -> bool:
    """False when t.me shows a bare 'join channel' page (preview disabled or no such channel)."""
    return "tgme_channel_info" in html or "tgme_widget_message" in html
