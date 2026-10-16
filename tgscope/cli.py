"""tgscope <channel> — analyse a public Telegram channel from its web preview."""

from __future__ import annotations

import argparse
import re
import sys
import webbrowser
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from . import __version__
from .analyze import analyze
from .fetch import ChannelError, fetch_channel, normalize_channel
from .report import render_html, render_json, render_text


def parse_tz(value: str) -> tuple[tzinfo, str]:
    if match := re.fullmatch(r"(?:UTC)?([+-]\d{1,2})(?::?(\d\d))?", value, re.IGNORECASE):
        hours = int(match.group(1))
        minutes = int(match.group(2) or 0) * (1 if hours >= 0 else -1)
        offset = timedelta(hours=hours, minutes=minutes)
        label = f"UTC{match.group(1)}" + (f":{match.group(2)}" if match.group(2) else "")
        return timezone(offset), label
    if value.upper() == "UTC":
        return UTC, "UTC"
    try:
        return ZoneInfo(value), value
    except (ZoneInfoNotFoundError, ValueError):
        raise argparse.ArgumentTypeError(
            f"Не понимаю часовой пояс {value!r}. Примеры: +3, UTC-5, Europe/Moscow"
        ) from None


def parse_since(value: str) -> datetime:
    if match := re.fullmatch(r"(\d+)d", value):
        return datetime.now(UTC) - timedelta(days=int(match.group(1)))
    try:
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "Дата в виде 2026-01-31 или 30d (последние 30 дней)"
        ) from None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tgscope",
        description="Анализ публичного Telegram-канала: когда пишет, откуда репостит, куда ведёт.",
    )
    p.add_argument("channel", help="имя канала: durov, @durov или https://t.me/durov")
    p.add_argument("-n", "--limit", type=int, default=300, help="сколько последних постов (300)")
    p.add_argument("--since", type=parse_since, help="не раньше даты: 2026-01-31 или 30d")
    p.add_argument(
        "--tz",
        type=parse_tz,
        default=(UTC, "UTC"),
        help="часовой пояс для карты активности: +3, Europe/Moscow (UTC)",
    )
    p.add_argument(
        "-o",
        "--out",
        "--html",
        dest="html",
        nargs="?",
        const="",
        metavar="ФАЙЛ",
        help="сохранить HTML-отчёт (по умолчанию reports/<канал>.html) и открыть",
    )
    p.add_argument("--json", metavar="ФАЙЛ", help="сохранить все данные в JSON")
    p.add_argument("--delay", type=float, default=1.0, help="пауза между запросами, с (1.0)")
    p.add_argument("--timeout", type=float, default=20, help="сетевой таймаут, с (20)")
    p.add_argument("--no-open", action="store_true", help="не открывать HTML в браузере")
    p.add_argument("--version", action="version", version=f"tgscope {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tz, tz_label = args.tz
    try:
        channel = normalize_channel(args.channel)
        info, posts = fetch_channel(
            channel,
            limit=max(args.limit, 1),
            since=args.since,
            delay=max(args.delay, 0.3),
            timeout=max(args.timeout, 1),
            progress=lambda n: print(f"\rЗагружено постов: {n}", end="", file=sys.stderr),
        )
        print("\r\033[K", end="", file=sys.stderr)
    except ChannelError as error:
        print(f"\r\033[K{error}", file=sys.stderr)
        return 1
    except httpx.HTTPError as error:
        print(f"\r\033[KНе удалось связаться с t.me: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nОстановлено.", file=sys.stderr)
        return 130

    if not posts:
        print(f"У @{channel} нет постов в веб-версии за выбранный период.", file=sys.stderr)
        return 1

    report = analyze(info, posts, tz, tz_label)
    print(render_text(report))

    if args.json:
        _write(Path(args.json), render_json(report))
    if args.html is not None:
        path = Path(args.html or f"reports/{channel}.html")
        _write(path, render_html(report))
        if not args.no_open:
            webbrowser.open(path.resolve().as_uri())
    return 0


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"Сохранено: {path}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
