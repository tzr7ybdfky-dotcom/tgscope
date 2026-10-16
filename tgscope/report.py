"""Render a Report as terminal text, JSON or a self-contained HTML page."""

from __future__ import annotations

import html
import json
from collections import Counter
from datetime import datetime
from typing import Any

from .analyze import Report

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
BARS = " ▁▂▃▄▅▆▇█"


def fmt_num(value: int | float | None) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 10_000:
        return f"{value / 1_000:.0f}K"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:.0f}" if isinstance(value, int) or value >= 10 else f"{value:.1f}"


def fmt_date(value: datetime | None) -> str:
    return value.strftime("%d.%m.%Y") if value else "—"


def offset_label(offset: int) -> str:
    return f"UTC{offset:+d}" if offset else "UTC"


def findings(r: Report) -> list[str]:
    """Plain-language observations, each one explainable from the numbers."""
    out: list[str] = []
    if r.quiet_window and r.utc_offset_guess is not None:
        start, end = r.quiet_window
        out.append(
            f"Почти не публикует с {start:02d}:00 до {end:02d}:00 UTC. Если это ночь, "
            f"автор живёт примерно в {offset_label(r.utc_offset_guess)}. "
            + ("Постов мало, гипотеза слабая. " if len(r.posts) < 150 else "Это гипотеза: ")
            + "отложенные посты и команда из нескольких человек её ломают."
        )
    share = sum(r.forwards.values()) / len(r.posts) if r.posts else 0
    if share >= 0.05:
        name, count = r.forwards.most_common(1)[0]
        out.append(f"{share:.0%} постов — репосты. Чаще всего из «{name}» ({count} шт.).")
    if r.missing_ids:
        out.append(
            f"В нумерации постов {r.missing_ids} пропусков. Это удалённые посты, "
            "служебные сообщения или фото из альбомов — по отдельности не различить."
        )
    if r.longest_pause:
        a, b = r.longest_pause
        out.append(f"Самая долгая пауза: {(b - a).days} дн. ({fmt_date(a)} → {fmt_date(b)}).")
    if len(r.authors) > 1:
        out.append(
            "Посты подписаны разными людьми: "
            + ", ".join(f"{n} ({c})" for n, c in r.authors.most_common(5))
            + "."
        )
    if r.posts and r.edited / len(r.posts) > 0.3:
        out.append(f"Отредактировано {r.edited / len(r.posts):.0%} постов — много.")
    if r.domains:
        host, count = r.domains.most_common(1)[0]
        if count >= max(3, len(r.posts) * 0.15):
            out.append(f"Часто ведёт на один сайт: {host} ({count} ссылок).")
    return out


# ---------------------------------------------------------------- terminal


def render_text(r: Report) -> str:
    c = r.channel
    lines = [
        f"@{c.username} — {c.title or 'без названия'}" + (" ✔" if c.verified else ""),
        f"Подписчики: {fmt_num(c.subscribers)}   Постов в выборке: {len(r.posts)}   "
        f"Период: {fmt_date(r.first)} – {fmt_date(r.last)}   ~{r.per_day:.1f} в день",
        f"Медиана просмотров: {fmt_num(r.median_views)}",
        "",
        f"Активность по часам ({r.tz_label}):",
        "  " + _sparkline(r.by_hour),
        "  " + "".join(str(h // 10) if h % 6 == 0 else " " for h in range(24)),
        "  " + "".join(str(h % 10) if h % 6 == 0 else " " for h in range(24)),
    ]
    sections: list[tuple[str, Counter[str]]] = [
        ("Откуда репостит", r.forwards),
        ("Какие каналы упоминает", Counter({f"@{k}": v for k, v in r.mentioned_channels.items()})),
        ("Куда ведут ссылки", r.domains),
        ("Хэштеги", Counter({f"#{k}": v for k, v in r.hashtags.items()})),
    ]
    for title, counter in sections:
        if counter:
            lines += ["", f"{title}:"]
            lines += [f"  {v:>4}  {k}" for k, v in counter.most_common(8)]
    if r.top_posts:
        lines += ["", "Самые просматриваемые:"]
        for p in r.top_posts:
            preview = p.text.replace("\n", " ")[:60] or "[" + ", ".join(p.media) + "]"
            lines.append(f"  {fmt_num(p.views):>6}  t.me/{c.username}/{p.id}  {preview}")
    if found := findings(r):
        lines += ["", "Что видно:"]
        lines += [f"  • {f}" for f in found]
    return "\n".join(lines)


def _sparkline(values: list[int]) -> str:
    top = max(values) or 1
    return "".join(BARS[round(v / top * (len(BARS) - 1))] for v in values)


# ---------------------------------------------------------------- json


def render_json(r: Report) -> str:
    data: dict[str, Any] = {
        "channel": vars(r.channel),
        "timezone": r.tz_label,
        "period": [
            r.first.isoformat() if r.first else None,
            r.last.isoformat() if r.last else None,
        ],
        "posts_per_day": round(r.per_day, 2),
        "median_views": r.median_views,
        "heatmap": r.heatmap,
        "forwards": dict(r.forwards.most_common()),
        "mentioned_channels": dict(r.mentioned_channels.most_common()),
        "domains": dict(r.domains.most_common()),
        "hashtags": dict(r.hashtags.most_common()),
        "authors": dict(r.authors.most_common()),
        "media": dict(r.media.most_common()),
        "id_gaps": [[g.after, g.before] for g in r.gaps],
        "utc_offset_guess": r.utc_offset_guess,
        "findings": findings(r),
        "posts": [{**vars(p), "date": p.date.isoformat() if p.date else None} for p in r.posts],
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- html


def render_html(r: Report) -> str:
    c = r.channel
    e = html.escape
    top = max((v for row in r.heatmap for v in row), default=0) or 1

    cells = []
    for day, row in enumerate(r.heatmap):
        cells.append(f'<div class="lbl">{WEEKDAYS[day]}</div>')
        for hour, v in enumerate(row):
            level = 0 if v == 0 else 1 + round(v / top * 4)
            cells.append(
                f'<div class="cell l{min(level, 5)}" title="{WEEKDAYS[day]} {hour:02d}:00 — {v}"></div>'
            )
    hours = '<div class="lbl"></div>' + "".join(
        f'<div class="hr">{h if h % 3 == 0 else ""}</div>' for h in range(24)
    )

    def table(title: str, counter: Counter[str], link: Any = None) -> str:
        if not counter:
            return ""
        biggest = counter.most_common(1)[0][1]
        rows = []
        for name, count in counter.most_common(15):
            href = link(name) if link else None
            label = f'<a href="{e(href)}" rel="noreferrer">{e(name)}</a>' if href else e(name)
            rows.append(
                f'<tr><td>{label}</td><td class="n">{count}</td>'
                f'<td class="bar"><span style="width:{count / biggest * 100:.0f}%"></span></td></tr>'
            )
        return f"<section><h2>{e(title)}</h2><table>{''.join(rows)}</table></section>"

    top_rows = "".join(
        f'<tr><td class="n">{fmt_num(p.views)}</td><td>{fmt_date(p.date)}</td>'
        f'<td><a href="https://t.me/{e(c.username)}/{p.id}" rel="noreferrer">'
        f"{e(p.text[:120].replace(chr(10), ' ') or '[' + ', '.join(p.media) + ']')}</a></td></tr>"
        for p in r.top_posts
    )
    notes = "".join(f"<li>{e(f)}</li>" for f in findings(r))
    stats = [
        ("Подписчики", fmt_num(c.subscribers)),
        ("Постов в выборке", str(len(r.posts))),
        ("В день", f"{r.per_day:.1f}"),
        ("Медиана просмотров", fmt_num(r.median_views)),
        ("Репостов", str(sum(r.forwards.values()))),
        ("Пропусков в номерах", str(r.missing_ids)),
    ]

    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>@{e(c.username)} — tgscope</title>
<style>
:root{{--bg:#fff;--fg:#1a1a1a;--muted:#6b6b6b;--line:#e4e4e4;--acc:#2f6fdf;
--h0:#f0f0f0;--h1:#d6e3fa;--h2:#a9c4f3;--h3:#6f9bea;--h4:#3d74de;--h5:#1f4fae}}
@media (prefers-color-scheme:dark){{:root{{--bg:#141414;--fg:#e8e8e8;--muted:#9a9a9a;--line:#2c2c2c;
--acc:#7aa5f0;--h0:#222;--h1:#1d2c47;--h2:#264479;--h3:#3460ad;--h4:#4f80d6;--h5:#8db0f0}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,system-ui,"Segoe UI",sans-serif}}
main{{max-width:960px;margin:0 auto;padding:32px 16px 64px}}
h1{{font-size:26px;margin:0 0 4px}} h2{{font-size:16px;margin:0 0 12px}}
.sub{{color:var(--muted);margin:0 0 24px;white-space:pre-line}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:1px;background:var(--line);
border:1px solid var(--line);margin-bottom:32px}}
.stats div{{background:var(--bg);padding:12px 14px}} .stats b{{display:block;font-size:22px}}
.stats span{{color:var(--muted);font-size:13px}}
section{{margin:0 0 32px}}
.heat{{display:grid;grid-template-columns:28px repeat(24,1fr);gap:2px;overflow-x:auto}}
.cell{{aspect-ratio:1;border-radius:2px;background:var(--h0)}}
.l1{{background:var(--h1)}}.l2{{background:var(--h2)}}.l3{{background:var(--h3)}}.l4{{background:var(--h4)}}.l5{{background:var(--h5)}}
.lbl,.hr{{font-size:11px;color:var(--muted)}} .hr{{text-align:left}}
table{{width:100%;border-collapse:collapse}} td{{padding:5px 8px 5px 0;border-bottom:1px solid var(--line);vertical-align:top}}
td.n{{text-align:right;width:60px;font-variant-numeric:tabular-nums;white-space:nowrap}}
td.bar{{width:30%}} td.bar span{{display:block;height:8px;margin-top:7px;background:var(--acc);border-radius:2px}}
a{{color:var(--acc);text-decoration:none;word-break:break-word}} a:hover{{text-decoration:underline}}
.cols{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:0 32px}}
ul{{padding-left:20px;margin:0}} li{{margin-bottom:6px}}
footer{{color:var(--muted);font-size:13px;border-top:1px solid var(--line);padding-top:16px}}
</style></head><body><main>
<h1>{e(c.title or "@" + c.username)}{" ✔" if c.verified else ""}</h1>
<p class="sub"><a href="https://t.me/{e(c.username)}" rel="noreferrer">@{e(c.username)}</a> · {fmt_date(r.first)} – {fmt_date(r.last)}
{e(c.description)}</p>
<div class="stats">{"".join(f"<div><b>{e(v)}</b><span>{e(k)}</span></div>" for k, v in stats)}</div>
{f"<section><h2>Что видно</h2><ul>{notes}</ul></section>" if notes else ""}
<section><h2>Когда публикует ({e(r.tz_label)})</h2><div class="heat">{hours}{"".join(cells)}</div></section>
<div class="cols">
{table("Откуда репостит", r.forwards, lambda n: r.forward_urls.get(n))}
{table("Какие каналы упоминает", r.mentioned_channels, lambda n: f"https://t.me/{n}")}
{table("Куда ведут ссылки", r.domains)}
{table("Хэштеги", r.hashtags)}
{table("Кто подписывает посты", r.authors)}
{table("Типы постов", r.media)}
</div>
{f"<section><h2>Самые просматриваемые</h2><table>{top_rows}</table></section>" if top_rows else ""}
<footer>Отчёт tgscope по публичной веб-версии t.me/s/{e(c.username)} от {datetime.now():%d.%m.%Y %H:%M}.
Числа описывают только загруженную выборку. Часовой пояс автора — гипотеза, а не факт.</footer>
</main></body></html>
"""
