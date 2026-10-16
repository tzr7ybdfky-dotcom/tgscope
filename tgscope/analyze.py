"""Turn a list of posts into numbers a person can read."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from statistics import median
from urllib.parse import urlparse

from .parse import ChannelInfo, Post, tme_channel

MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z][A-Za-z0-9_]{3,31})\b")
HASHTAG_RE = re.compile(r"(?<!\w)#(\w{2,64})")
NIGHT_HOURS = 5  # length of the quiet window used to guess the author's night


@dataclass
class Gap:
    after: int
    before: int

    @property
    def missing(self) -> int:
        return self.before - self.after - 1


@dataclass
class Report:
    channel: ChannelInfo
    posts: list[Post]
    tz_label: str
    first: datetime | None = None
    last: datetime | None = None
    per_day: float = 0.0
    heatmap: list[list[int]] = field(default_factory=lambda: [[0] * 24 for _ in range(7)])
    by_hour: list[int] = field(default_factory=lambda: [0] * 24)
    median_views: int | None = None
    top_posts: list[Post] = field(default_factory=list)
    forwards: Counter[str] = field(default_factory=Counter)
    forward_urls: dict[str, str] = field(default_factory=dict)
    mentioned_channels: Counter[str] = field(default_factory=Counter)
    domains: Counter[str] = field(default_factory=Counter)
    hashtags: Counter[str] = field(default_factory=Counter)
    authors: Counter[str] = field(default_factory=Counter)
    media: Counter[str] = field(default_factory=Counter)
    edited: int = 0
    gaps: list[Gap] = field(default_factory=list)
    quiet_window: tuple[int, int] | None = None
    utc_offset_guess: int | None = None
    longest_pause: tuple[datetime, datetime] | None = None

    @property
    def missing_ids(self) -> int:
        return sum(g.missing for g in self.gaps)


def analyze(channel: ChannelInfo, posts: list[Post], tz: tzinfo, tz_label: str) -> Report:
    report = Report(channel=channel, posts=posts, tz_label=tz_label)
    dated = [p for p in posts if p.date]
    if dated:
        report.first = min(p.date for p in dated if p.date)
        report.last = max(p.date for p in dated if p.date)
        days = max((report.last - report.first).total_seconds() / 86400, 1)
        report.per_day = len(dated) / days

    utc_hours = [0] * 24
    for post in dated:
        assert post.date
        local = post.date.astimezone(tz)
        report.heatmap[local.weekday()][local.hour] += 1
        report.by_hour[local.hour] += 1
        utc_hours[post.date.astimezone(UTC).hour] += 1

    views = [p.views for p in posts if p.views is not None]
    report.median_views = round(median(views)) if views else None
    report.top_posts = sorted(
        (p for p in posts if p.views is not None), key=lambda p: p.views or 0, reverse=True
    )[:5]

    own = channel.username.lower()
    for post in posts:
        if post.forwarded_from:
            report.forwards[post.forwarded_from] += 1
            if post.forwarded_url:
                report.forward_urls.setdefault(post.forwarded_from, post.forwarded_url)
        for link in post.links:
            name = tme_channel(link)
            if name and name != own:
                report.mentioned_channels[name] += 1
            elif not name:
                host = (urlparse(link).hostname or "").removeprefix("www.")
                if host and host not in {"t.me", "telegram.me"}:
                    report.domains[host] += 1
        for mention in MENTION_RE.findall(post.text):
            if mention.lower() != own:
                report.mentioned_channels[mention.lower()] += 1
        report.hashtags.update(tag.lower() for tag in HASHTAG_RE.findall(post.text))
        if post.author:
            report.authors[post.author] += 1
        report.media.update(post.media or ["text"])
        report.edited += post.edited

    ids = [p.id for p in posts]
    report.gaps = [Gap(a, b) for a, b in zip(ids, ids[1:], strict=False) if b - a > 1]

    if len(dated) >= 30:
        report.quiet_window, report.utc_offset_guess = guess_night(utc_hours)

    ordered = sorted(dated, key=lambda p: p.date or datetime.min)
    pauses = [(a.date, b.date) for a, b in zip(ordered, ordered[1:], strict=False)]
    if pauses:
        start, end = max(pauses, key=lambda pair: pair[1] - pair[0])  # type: ignore[operator]
        if start and end and end - start > timedelta(days=2):
            report.longest_pause = (start, end)
    return report


def guess_night(utc_hours: list[int]) -> tuple[tuple[int, int], int]:
    """Find the quietest NIGHT_HOURS-long window (UTC) and assume it is ~01:00–06:00 local.

    When several windows are equally quiet, take the middle of the longest run of them,
    so a long empty stretch is centred instead of picking its first hour.
    """
    sums = [sum(utc_hours[(s + i) % 24] for i in range(NIGHT_HOURS)) for s in range(24)]
    quietest = min(sums)
    best_run: tuple[int, int] = (0, 0)  # (length, start)
    for start in range(24):
        if sums[start] != quietest or sums[(start - 1) % 24] == quietest:
            continue
        length = 1
        while length < 24 and sums[(start + length) % 24] == quietest:
            length += 1
        best_run = max(best_run, (length, start))
    length, run_start = best_run if best_run[0] else (24, 0)
    best_start = (run_start + (length - 1) // 2) % 24
    center_utc = (best_start + NIGHT_HOURS / 2) % 24
    offset = (round(3.5 - center_utc) + 12) % 24 - 12
    return (best_start, (best_start + NIGHT_HOURS) % 24), offset
