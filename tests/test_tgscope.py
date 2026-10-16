from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from tgscope.analyze import analyze, guess_night
from tgscope.cli import parse_tz
from tgscope.fetch import ChannelError, fetch_channel, normalize_channel, retry_after
from tgscope.parse import ChannelInfo, Post, parse_channel, parse_count, parse_posts, tme_channel
from tgscope.report import findings, render_html, render_json, render_text

PAGE = (Path(__file__).parent / "fixtures" / "page.html").read_text(encoding="utf-8")


def test_parse_count():
    assert parse_count("9.31M") == 9_310_000
    assert parse_count("13.7K") == 13_700
    assert parse_count("538") == 538
    assert parse_count("") is None
    assert parse_count("n/a") is None


@pytest.mark.parametrize(
    ("url", "name"),
    [
        ("https://t.me/SourceChan/77", "sourcechan"),
        ("https://t.me/s/demochannel", "demochannel"),
        ("https://t.me/+AbCdEf123", None),
        ("https://t.me/joinchat/xyz", None),
        ("https://example.com/t.me", None),
    ],
)
def test_tme_channel(url, name):
    assert tme_channel(url) == name


def test_normalize_channel():
    assert normalize_channel("@demochannel") == "demochannel"
    assert normalize_channel("https://t.me/s/demochannel/5?x=1") == "demochannel"
    with pytest.raises(ChannelError):
        normalize_channel("ab")


def test_parse_channel_and_posts():
    info = parse_channel(PAGE, "demochannel")
    assert (info.title, info.subscribers, info.verified) == ("Demo Channel", 12_500, True)

    first, repost, hidden = parse_posts(PAGE)
    assert first.id == 10 and first.views == 1200 and first.links == ["https://example.com/a"]
    assert repost.forwarded_from == "Source Chan"
    assert repost.forwarded_url == "https://t.me/sourcechan/77"
    assert repost.edited and repost.author == "Аня" and repost.media == ["photo"]
    assert repost.reactions == 1520
    assert hidden.forwarded_from == "(скрытый источник)" and hidden.views is None


def test_analyze_counts_and_gaps():
    info = parse_channel(PAGE, "demochannel")
    report = analyze(info, parse_posts(PAGE), *parse_tz("+3"))
    assert report.forwards == {"sourcechan": 1, "(скрытый источник)": 1}
    assert report.forward_titles == {"sourcechan": "Source Chan"}
    assert report.source_url("sourcechan") == "https://t.me/sourcechan"
    assert report.source_url("(скрытый источник)") is None
    assert report.mentioned_channels == {"otherchan": 1, "sourcechan": 1}
    assert report.domains == {"example.com": 1}
    assert report.hashtags == {"новости": 1}
    assert [(g.after, g.before, g.missing) for g in report.gaps] == [(10, 13, 2)]
    assert report.heatmap[0][12] == 1  # Monday 09:15 UTC -> 12:15 at UTC+3
    assert report.median_views == 1050


def test_guess_night_centres_long_quiet_stretch():
    # Active 06:00–21:59 UTC, silent 22:00–05:59: centre of silence is 02:00 UTC.
    hours = [0] * 6 + [5] * 16 + [0] * 2
    (start, end), offset = guess_night(hours)
    assert (start, end) == (23, 4)
    assert offset == 2  # 01:30 UTC centre -> ~03:30 local


def test_parse_tz():
    assert parse_tz("UTC")[1] == "UTC"
    tz, label = parse_tz("-5")
    assert label == "UTC-5" and tz.utcoffset(None).total_seconds() == -5 * 3600
    assert parse_tz("Europe/Moscow")[1] == "Europe/Moscow"


def test_renderers_escape_and_run():
    info = parse_channel(PAGE, "demochannel")
    info.title = "<script>x</script>"
    report = analyze(info, parse_posts(PAGE), UTC, "UTC")
    page = render_html(report)
    assert "<script>x" not in page and "&lt;script&gt;" in page
    assert "Source Chan" in render_text(report)
    assert '"demochannel"' in render_json(report)
    assert any("репосты" in f for f in findings(report))


def test_fetch_paginates_with_mock_transport():
    def page(ids):
        posts = "".join(
            f'<div class="tgme_widget_message" data-post="demochannel/{i}">'
            f'<a class="tgme_widget_message_date">'
            f'<time datetime="2026-01-0{1 + i % 8}T10:00:00+00:00"></time></a></div>'
            for i in ids
        )
        return f'<div class="tgme_channel_info"></div>{posts}'

    seen = []

    def handler(request):
        seen.append(str(request.url))
        before = request.url.params.get("before")
        if before is None:
            return httpx.Response(200, text=page(range(21, 31)))
        return httpx.Response(200, text=page(range(max(int(before) - 10, 1), int(before))))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    _, posts = fetch_channel("demochannel", limit=25, delay=0, client=client)
    assert [p.id for p in posts] == list(range(6, 31))
    assert seen[1].endswith("before=21") and seen[2].endswith("before=11")


def test_fetch_rejects_private_channel():
    empty = httpx.MockTransport(lambda r: httpx.Response(200, text="<html/>"))
    client = httpx.Client(transport=empty)
    with pytest.raises(ChannelError):
        fetch_channel("demochannel", client=client, delay=0)


def test_guess_night_needs_a_clearly_quiet_window():
    assert guess_night([5] * 24) is None  # posts around the clock
    assert guess_night([3] * 5 + [4] * 19) is None  # slightly quieter is not "night"
    assert guess_night([0] * 24) is None


def test_mention_and_link_to_same_channel_count_once():
    post = Post(
        id=1,
        date=datetime(2026, 1, 1, tzinfo=UTC),
        text="см @OtherChan и @demochannel",
        links=["https://t.me/otherchan", "https://t.me/thirdchan/5"],
    )
    report = analyze(ChannelInfo("demochannel"), [post], UTC, "UTC")
    assert report.mentioned_channels == {"otherchan": 1, "thirdchan": 1}


def test_parse_tz_keeps_sign_of_zero_hours():
    assert parse_tz("-00:30")[0].utcoffset(None) == -timedelta(minutes=30)
    assert parse_tz("UTC-05:30")[0].utcoffset(None) == -timedelta(hours=5, minutes=30)
    assert parse_tz("+05:45")[0].utcoffset(None) == timedelta(hours=5, minutes=45)


def test_retry_after_is_honoured_in_full(monkeypatch):
    assert retry_after("120", default=2) == 120
    assert retry_after(None, default=4) == 4
    assert retry_after("garbage", default=4) == 4

    slept = []
    monkeypatch.setattr("tgscope.fetch.time.sleep", slept.append)
    responses = iter(
        [httpx.Response(429, headers={"Retry-After": "120"}), httpx.Response(200, text="ok")]
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda r: next(responses)))
    from tgscope.fetch import _get

    assert _get(client, "https://t.me/s/demochannel").text == "ok"
    assert slept == [120]


def test_too_long_retry_after_stops_instead_of_hanging(monkeypatch):
    monkeypatch.setattr("tgscope.fetch.time.sleep", lambda s: None)
    blocked = httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "3600"}))
    with pytest.raises(ChannelError, match="60 мин"):
        fetch_channel("demochannel", client=httpx.Client(transport=blocked), delay=0)


def test_channels_with_the_same_title_stay_separate():
    def repost(post_id, source):
        return Post(
            id=post_id,
            date=datetime(2026, 1, 1, tzinfo=UTC),
            forwarded_from="Новости",
            forwarded_url=f"https://t.me/{source}/1",
        )

    posts = [repost(1, "news_one"), repost(2, "news_two"), repost(3, "news_two")]
    report = analyze(ChannelInfo("demochannel"), posts, UTC, "UTC")
    assert report.forwards == {"news_two": 2, "news_one": 1}
    text = render_text(report)
    assert "https://t.me/news_one  Новости" in text and "https://t.me/news_two  Новости" in text
    page = render_html(report)
    assert 'href="https://t.me/news_one"' in page and 'href="https://t.me/news_two"' in page
    assert '"url": "https://t.me/news_two"' in render_json(report)
