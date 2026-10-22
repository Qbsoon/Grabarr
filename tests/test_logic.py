import base64
import sys
import os
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    classify_language,
    extract_tmdb_ids,
    filter_releases,
    hash_hex,
    parse_resolution,
    parse_seasons,
    plex_items,
    safe_name,
    season_coverage,
)


def mk(title, size, seeders=10, indexer=1, guid="g"):
    return {"title": title, "size": size, "seeders": seeders, "indexerId": indexer, "guid": guid}


def test_classify_pl_audio():
    assert classify_language("Movie.2023.PL.LEKTOR.1080p") == "pl_audio"
    assert classify_language("Movie.2023.PLDUB.1080p") == "pl_audio"
    assert classify_language("Movie.2023.DUBBING.1080p") == "pl_audio"


def test_classify_subs_only():
    assert classify_language("Movie.2023.NAPISYPL.1080p") == "subs_only"


def test_classify_multi():
    assert classify_language("Movie.2023.MULTI.1080p") == "multi"
    assert classify_language("Movie.2023.MULTI.PL.1080p") == "pl_audio"


def test_classify_en_and_unknown():
    assert classify_language("Movie.2023.ENGLISH.1080p") == "en"
    assert classify_language("Movie.2023.1080p.BluRay") == "unknown"


def test_parse_seasons():
    assert parse_seasons("Show.S02.1080p") == {2}
    assert parse_seasons("Show.S01-03.1080p") == {1, 2, 3}
    assert parse_seasons("Show.S01-S03.1080p") == {1, 2, 3}
    assert parse_seasons("Show.S02E05.1080p") == set()
    assert parse_seasons("Show.Complete.Series.1080p") is None


def test_parse_resolution():
    assert parse_resolution("x.2160p") == 2160
    assert parse_resolution("x.1080p") == 1080
    assert parse_resolution("x.720p") == 720
    assert parse_resolution("x.NoRes") == 0


def test_season_coverage():
    assert season_coverage({2}, 2, 5) == 1
    assert season_coverage({1, 2, 3}, 2, 5) == 3
    assert season_coverage(None, 2, 5) == 5
    assert season_coverage({1}, 2, 5) is None
    assert season_coverage(None, None, 5) == 5
    assert season_coverage({1, 2, 3}, None, 5) is None
    assert season_coverage(set(), None, 5) is None


def test_filter_sort_and_classes():
    rel = [
        mk("Movie.2023.1080p", 10 * 1024**3, seeders=5, indexer=1, guid="a"),
        mk("Movie.2023.2160p", 10 * 1024**3, seeders=5, indexer=1, guid="b"),
        mk("Movie.2023.720p", 10 * 1024**3, seeders=100, indexer=1, guid="c"),
        mk("Movie.2023.NAPISYPL.1080p", 10 * 1024**3, seeders=50, indexer=1, guid="d"),
        mk("Movie.2023.PL.1080p", 10 * 1024**3, seeders=60, indexer=1, guid="e"),
    ]
    out = filter_releases(rel, "en", "movie", None, None)
    # subs_only passes at EN target (plan §7): d (1080p, 50 seeders) ranks between b and a
    assert [r["guid"] for r in out] == ["b", "d", "a"]
    out = filter_releases(rel, "pl", "movie", None, None)
    assert [r["guid"] for r in out] == ["e"]


def test_filter_size_limit_per_season():
    assert filter_releases([mk("Show.S01-03.1080p", 91 * 1024**3, indexer=1, guid="f")], "en", "tv", None, 3) == []
    assert len(filter_releases([mk("Show.S01-03.1080p", 90 * 1024**3, indexer=1, guid="f")], "en", "tv", None, 3)) == 1


def test_filter_episodes_rejected():
    rel = [mk("Show.S02E05.1080p", 1024**3, indexer=1, guid="g")]
    assert filter_releases(rel, "en", "tv", 2, 5) == []
    assert filter_releases(rel, "en", "tv", None, 5) == []


def test_filter_season_coverage():
    # S02 covers the requested season -> accepted (plan §13)
    assert len(filter_releases([mk("Show.S02.1080p", 1024**3, indexer=1, guid="h")], "en", "tv", 2, 5)) == 1
    # S02 does not cover the whole series -> rejected
    assert len(filter_releases([mk("Show.S02.1080p", 1024**3, indexer=1, guid="h")], "en", "tv", None, 5)) == 0
    # S01-05 covers all seasons -> accepted
    assert len(filter_releases([mk("Show.S01-05.1080p", 1024**3, indexer=1, guid="h")], "en", "tv", None, 5)) == 1


def test_filter_dedupe_seeders_priority():
    rel = [
        mk("Movie.2023.1080p", 10 * 1024**3, seeders=5, indexer=1, guid="i"),
        mk("Movie.2023.1080p", 10 * 1024**3, seeders=50, indexer=1, guid="j"),
        mk("Movie.2023.1080p", 10 * 1024**3, seeders=7, indexer=2, guid="k"),
    ]
    out = filter_releases(rel, "en", "movie", None, None)
    assert [r["guid"] for r in out] == ["j", "k"]


def test_hash_hex_base32():
    raw = bytes(20)
    b32 = base64.b32encode(raw).decode().lower()
    assert hash_hex(b32) == raw.hex()
    assert hash_hex(raw.hex()) == raw.hex()


def test_safe_name():
    assert safe_name("../../etc/passwd") == "etc/passwd"


def test_filter_stats():
    rel = [
        mk("Movie.2023.PL.LEKTOR.1080p", 10 * 1024**3, indexer=1, guid="s1"),
        mk("Movie.2023.720p", 10 * 1024**3, indexer=1, guid="s2"),
        mk("Movie.2023.1080p", 40 * 1024**3, indexer=1, guid="s3"),
    ]
    stats = {}
    out = filter_releases(rel, "en", "movie", None, None, stats=stats)
    assert out == []
    assert stats == {"total": 3, "deduped": 3, "lang": 1, "resolution": 1, "seasons": 0, "size": 1}


def test_extract_tmdb_ids():
    xml = """<MediaContainer>
<Metadata guid="plex://movie/5d776845d1916d001fa9e3f6"><Guid id="tmdb://550"/><Guid id="imdb://tt0137523"/></Metadata>
<Metadata guid="tmdb://42"/>
</MediaContainer>"""
    root = ET.fromstring(xml)
    ids = {i for m in root.iter("Metadata") for i in extract_tmdb_ids(m)}
    assert ids == {550, 42}


def test_plex_items_xml():
    # Plex XML: itemy biblioteki to Video/Directory, nie Metadata (klucz z JSON)
    xml = (
        "<MediaContainer>"
        '<Video guid="plex://movie/a"><Guid id="tmdb://550"/></Video>'
        '<Directory guid="plex://show/b"><Guid id="tmdb://42"/></Directory>'
        "</MediaContainer>"
    )
    root = ET.fromstring(xml)
    ids = {i for m in plex_items(root) for i in extract_tmdb_ids(m)}
    assert ids == {550, 42}
    assert safe_name("/absolute/path") == "absolute/path"
    assert safe_name("a\\..\\b") == "a/b"
    assert safe_name("..") == ""
