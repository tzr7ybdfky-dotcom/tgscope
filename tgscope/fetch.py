"""Download posts page by page from https://t.me/s/<channel>?before=<id>."""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from .parse import ChannelInfo, Post, is_public_preview, parse_channel, parse_posts

DEFAULT_LIMIT = 3000
MAX_WAIT = 300  # longer server-requested pauses end the run instead of hanging silently
USER_AGENT = "Mozilla/5.0 (compatible; tgscope/0.1; +public channel preview analysis)"


class ChannelError(RuntimeError):
    pass


def normalize_channel(value: str) -> str:
    value = value.strip()
    value = re.sub(r"^(https?://)?(www\.)?(t\.me|telegram\.me)/(s/)?", "", value)
    value = value.lstrip("@").split("/")[0].split("?")[0]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", value):
        raise ChannelError(f"Это не похоже на имя публичного канала: {value!r}")
    return value


def fetch_channel(
    channel: str,
    limit: int = DEFAULT_LIMIT,
    since: datetime | None = None,
    delay: float = 1.0,
    timeout: float = 20,
    client: httpx.Client | None = None,
    progress: Callable[[int], None] | None = None,
) -> tuple[ChannelInfo, list[Post]]:
    """Walk the channel backwards from the newest post until `limit` or `since` is reached."""
    own_client = client is None
    client = client or httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True
    )
    try:
        url = f"https://t.me/s/{channel}"
        response = _get(client, url)
        if not is_public_preview(response.text):
            raise ChannelError(
                f"У @{channel} нет публичной веб-версии: канала нет, он закрытый "
                "или владелец отключил предпросмотр."
            )
        info = parse_channel(response.text, channel)
        posts: dict[int, Post] = {}
        page = parse_posts(response.text)
        while page:
            for post in page:
                posts.setdefault(post.id, post)
            if progress:
                progress(len(posts))
            oldest = page[0]
            if len(posts) >= limit or (since and oldest.date and oldest.date < since):
                break
            if oldest.id <= 1:
                break
            time.sleep(delay)
            page = parse_posts(_get(client, url, params={"before": oldest.id}).text)
            page = [p for p in page if p.id < oldest.id]
        result = sorted(posts.values(), key=lambda p: p.id, reverse=True)
        if since:
            result = [p for p in result if p.date is None or p.date >= since]
        return info, sorted(result[:limit], key=lambda p: p.id)
    finally:
        if own_client:
            client.close()


def _get(client: httpx.Client, url: str, params: dict[str, int] | None = None) -> httpx.Response:
    for attempt in range(4):
        response = client.get(url, params=params)
        if response.status_code == 429 or response.status_code >= 500:
            wait = retry_after(response.headers.get("Retry-After"), default=2 ** (attempt + 1))
            if wait > MAX_WAIT:
                raise ChannelError(
                    f"Telegram просит подождать {wait / 60:.0f} мин. Попробуйте позже."
                )
            print(f"\nTelegram просит подождать {wait:.0f} с…", file=sys.stderr)
            time.sleep(wait)
            continue
        response.raise_for_status()
        return response
    raise ChannelError("Telegram слишком долго ограничивает запросы, попробуйте позже.")


def retry_after(value: str | None, default: float) -> float:
    """Seconds to wait from a Retry-After header: delta-seconds or an HTTP date."""
    if not value:
        return default
    try:
        return max(float(value), 0)
    except ValueError:
        pass
    try:
        return max((parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds(), 0)
    except (TypeError, ValueError):
        return default
