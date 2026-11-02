"""Unit tests for the youtube-mcp extractor (no network)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled, VideoUnavailable

import extractor


@pytest.mark.parametrize(
    "value",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s",
        "https://youtu.be/dQw4w9WgXcQ?si=abc",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ",
        "https://m.youtube.com/live/dQw4w9WgXcQ",
        "dQw4w9WgXcQ",
    ],
)
def test_parse_video_id(value):
    assert extractor.parse_video_id(value) == "dQw4w9WgXcQ"


@pytest.mark.parametrize("value", ["", "https://example.com/watch?v=dQw4w9WgXcQ", "not a url", "https://youtube.com/"])
def test_parse_video_id_rejects_invalid(value):
    with pytest.raises(ValueError):
        extractor.parse_video_id(value)


def test_format_timestamp():
    assert extractor.format_timestamp(0) == "00:00"
    assert extractor.format_timestamp(494.7) == "08:14"
    assert extractor.format_timestamp(3725) == "1:02:05"


def test_extract_links_flags_affiliates_and_utm():
    desc = (
        "Gear: https://amzn.to/3abc and https://bit.ly/xyz\n"
        "Site: https://example.com/page?utm_source=yt&utm_medium=desc.\n"
        "Plain: https://github.com/org/repo)"
    )
    links = {l["url"]: l for l in extractor.extract_links(desc)}
    assert links["https://amzn.to/3abc"]["affiliate"] is True
    assert links["https://bit.ly/xyz"]["shortener"] is True
    assert links["https://example.com/page?utm_source=yt&utm_medium=desc"]["utm"] is True
    assert links["https://github.com/org/repo"] == {
        "url": "https://github.com/org/repo",
        "domain": "github.com",
        "affiliate": False,
        "shortener": False,
        "utm": False,
    }


def test_extract_links_amazon_tag_param_is_affiliate():
    links = extractor.extract_links("https://www.amazon.com/dp/B0X?tag=creator-20")
    assert links[0]["affiliate"] is True


def test_extract_promo_codes():
    desc = "Use code TECH20 for 20% off! Coupon: SAVE-10 and promo code 'BEST5'. Nothing else here."
    assert extractor.extract_promo_codes(desc) == ["TECH20", "SAVE-10", "BEST5"]


def test_parse_chapters():
    desc = "Intro text\n0:00 Intro\n02:10 Sponsor segment\n1:02:05 - Wrap up\nnot a chapter 12:34 inline"
    assert extractor.parse_chapters(desc) == [
        {"timestamp": "0:00", "start_sec": 0, "title": "Intro"},
        {"timestamp": "02:10", "start_sec": 130, "title": "Sponsor segment"},
        {"timestamp": "1:02:05", "start_sec": 3725, "title": "Wrap up"},
    ]


def _yt_client(items):
    client = MagicMock()
    client.videos.return_value.list.return_value.execute.return_value = {"items": items}
    return client


def test_fetch_metadata_maps_api_fields():
    item = {
        "snippet": {
            "title": "T",
            "channelId": "UC123",
            "channelTitle": "Chan",
            "publishedAt": "2025-01-02T03:04:05Z",
            "description": "0:00 Intro\nBuy https://amzn.to/x use code ABC12",
            "defaultAudioLanguage": "en",
        },
        "contentDetails": {"duration": "PT10M5S"},
        "statistics": {"viewCount": "1000", "commentCount": "7"},
    }
    meta = extractor.fetch_metadata("vid", _yt_client([item]))
    assert meta["video_id"] == "vid"
    assert meta["channel_id"] == "UC123"
    assert meta["channel_name"] == "Chan"
    assert meta["title"] == "T"
    assert meta["upload_date"] == "2025-01-02T03:04:05Z"
    assert meta["duration_sec"] == 605
    assert meta["view_count"] == 1000
    assert meta["chapters"][0]["title"] == "Intro"
    assert meta["links"][0]["affiliate"] is True
    assert meta["promo_codes"] == ["ABC12"]


def test_fetch_metadata_not_found():
    with pytest.raises(LookupError):
        extractor.fetch_metadata("missing", _yt_client([]))


def _snippet(text, start, duration=2.0):
    return SimpleNamespace(text=text, start=start, duration=duration)


def test_fetch_transcript_ok_preserves_timestamps():
    api = MagicMock()
    api.fetch.return_value = SimpleNamespace(
        language_code="en", is_generated=True, snippets=[_snippet("hello", 0.0), _snippet("world", 494.2)]
    )
    out = extractor.fetch_transcript("vid", None, api)
    assert out["status"] == "OK"
    assert out["language"] == "en"
    assert out["segments"][1] == {"timestamp": "08:14", "start_sec": 494.2, "text": "world"}
    api.fetch.assert_called_once()


def test_fetch_transcript_language_filter_start_end():
    api = MagicMock()
    api.fetch.return_value = SimpleNamespace(
        language_code="en", is_generated=False, snippets=[_snippet("a", 1), _snippet("b", 50), _snippet("c", 100)]
    )
    out = extractor.fetch_transcript("vid", None, api, start_sec=10, end_sec=60)
    assert [s["text"] for s in out["segments"]] == ["b"]


@pytest.mark.parametrize(
    "exc",
    [
        TranscriptsDisabled("vid"),
        NoTranscriptFound("vid", ["en"], MagicMock()),
    ],
)
def test_fetch_transcript_disabled_returns_status(exc):
    api = MagicMock()
    api.fetch.side_effect = exc
    api.list.side_effect = exc
    out = extractor.fetch_transcript("vid", None, api)
    assert out["status"] == "TRANSCRIPTS_DISABLED"
    assert out["segments"] == []
    assert "unavailable" in out["message"].lower() or "disabled" in out["message"].lower()


def test_fetch_transcript_falls_back_to_any_language():
    api = MagicMock()
    api.fetch.side_effect = NoTranscriptFound("vid", ["en"], MagicMock())
    es = MagicMock()
    es.fetch.return_value = SimpleNamespace(language_code="es", is_generated=False, snippets=[_snippet("hola", 3)])
    api.list.return_value = iter([es])
    out = extractor.fetch_transcript("vid", None, api)
    assert out["status"] == "OK"
    assert out["language"] == "es"


def test_fetch_transcript_video_unavailable():
    api = MagicMock()
    api.fetch.side_effect = VideoUnavailable("vid")
    out = extractor.fetch_transcript("vid", None, api)
    assert out["status"] == "VIDEO_UNAVAILABLE"


def test_fetch_comments_maps_and_marks_creator_replies():
    client = MagicMock()
    client.commentThreads.return_value.list.return_value.execute.return_value = {
        "items": [
            {
                "snippet": {
                    "isPublic": True,
                    "totalReplyCount": 1,
                    "topLevelComment": {
                        "snippet": {
                            "authorDisplayName": "@dev",
                            "authorChannelId": {"value": "UCfan"},
                            "textDisplay": "Use --legacy-peer-deps",
                            "likeCount": 42,
                            "publishedAt": "2025-01-01T00:00:00Z",
                        }
                    },
                },
                "replies": {
                    "comments": [
                        {"snippet": {"authorDisplayName": "Creator", "authorChannelId": {"value": "UCown"}, "textDisplay": "thanks", "likeCount": 3}}
                    ]
                },
            }
        ]
    }
    out = extractor.fetch_comments("vid", 100, "top", client, channel_id="UCown")
    assert out["status"] == "OK"
    c = out["comments"][0]
    assert c["author"] == "@dev" and c["likes"] == 42 and c["text"] == "Use --legacy-peer-deps"
    assert c["replies"][0]["is_creator"] is True
    kwargs = client.commentThreads.return_value.list.call_args.kwargs
    assert kwargs["order"] == "relevance" and kwargs["videoId"] == "vid"


def test_fetch_comments_disabled():
    from googleapiclient.errors import HttpError

    resp = SimpleNamespace(status=403, reason="forbidden")
    err = HttpError(resp, b'{"error":{"errors":[{"reason":"commentsDisabled"}]}}')
    client = MagicMock()
    client.commentThreads.return_value.list.return_value.execute.side_effect = err
    out = extractor.fetch_comments("vid", 10, "top", client)
    assert out["status"] == "COMMENTS_DISABLED"
    assert out["comments"] == []


def test_fetch_comments_rejects_bad_sort():
    with pytest.raises(ValueError):
        extractor.fetch_comments("vid", 10, "weird", MagicMock())


# ---- cache ----------------------------------------------------------------------------------------
from cache import DiskCache  # noqa: E402


def _ok_api(calls):
    api = MagicMock()

    def fetch(video_id, languages):
        calls.append(video_id)
        return SimpleNamespace(language_code="en", is_generated=False, snippets=[_snippet("a", 1), _snippet("b", 50), _snippet("c", 100)])

    api.fetch.side_effect = fetch
    return api


def test_disk_cache_roundtrip_ttl_and_corruption(tmp_path, monkeypatch):
    cache = DiskCache(tmp_path)
    cache.put("k", {"a": 1}, ttl_sec=60)
    assert cache.get("k") == {"a": 1}
    assert cache.get("missing") is None

    cache.put("short", 1, ttl_sec=-1)  # already expired
    assert cache.get("short") is None

    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert cache.get("bad") is None  # corrupt file is a miss, not a crash
    assert not list(tmp_path.glob("*.tmp"))  # atomic write leaves no temp files

    cache.put("../escape/attempt", 1, ttl_sec=60)  # key is sanitized into the cache dir
    assert all(p.parent == tmp_path for p in tmp_path.rglob("*.json"))


def test_transcript_second_call_is_served_from_cache(tmp_path):
    calls, cache = [], DiskCache(tmp_path)
    api = _ok_api(calls)
    first = extractor.fetch_transcript_cached("vid", None, api, cache)
    second = extractor.fetch_transcript_cached("vid", None, api, cache)
    assert (first["cached"], second["cached"]) == (False, True)
    assert calls == ["vid"]  # YouTube was contacted once
    assert second["segments"] == first["segments"] and second["status"] == "OK"


def test_cached_transcript_can_be_sliced_and_languages_get_separate_entries(tmp_path):
    calls, cache = [], DiskCache(tmp_path)
    api = _ok_api(calls)
    extractor.fetch_transcript_cached("vid", None, api, cache)
    sliced = extractor.fetch_transcript_cached("vid", None, api, cache, start_sec=10, end_sec=60)
    assert sliced["cached"] is True and [s["text"] for s in sliced["segments"]] == ["b"]
    extractor.fetch_transcript_cached("vid", ["es"], api, cache)  # different language preference -> new fetch
    assert calls == ["vid", "vid"]


def test_disabled_transcript_is_cached_but_blocks_and_errors_are_not(tmp_path):
    cache = DiskCache(tmp_path)
    api = MagicMock()
    api.fetch.side_effect = TranscriptsDisabled("vid")
    api.list.side_effect = TranscriptsDisabled("vid")
    extractor.fetch_transcript_cached("vid", None, api, cache)
    again = extractor.fetch_transcript_cached("vid", None, api, cache)
    assert again["status"] == "TRANSCRIPTS_DISABLED" and again["cached"] is True
    assert api.fetch.call_count == 1

    from youtube_transcript_api import IpBlocked

    blocked = MagicMock()
    blocked.fetch.side_effect = IpBlocked("vid2")
    r1 = extractor.fetch_transcript_cached("vid2", None, blocked, cache)
    r2 = extractor.fetch_transcript_cached("vid2", None, blocked, cache)
    assert r1["status"] == r2["status"] == "REQUEST_BLOCKED" and r2["cached"] is False
    assert blocked.fetch.call_count == 2  # a temporary block must never be remembered


def test_metadata_is_cached(tmp_path):
    cache = DiskCache(tmp_path)
    item = {"snippet": {"title": "T", "channelId": "UC", "channelTitle": "C", "description": ""}, "contentDetails": {}, "statistics": {}}
    client = _yt_client([item])
    a = extractor.fetch_metadata_cached("vid", client, cache)
    b = extractor.fetch_metadata_cached("vid", client, cache)
    assert (a["cached"], b["cached"]) == (False, True) and b["title"] == "T"
    assert client.videos.return_value.list.return_value.execute.call_count == 1
