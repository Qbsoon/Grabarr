import asyncio
import datetime
import os
import sys
import tempfile

import aiosqlite

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as grabarr  # noqa: E402

SCHEMA = """CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tmdb_id INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    season INTEGER,
    title TEXT NOT NULL,
    year INTEGER,
    language_target TEXT NOT NULL,
    release_name TEXT,
    indexer_id INTEGER,
    guid TEXT,
    info_hash TEXT,
    size INTEGER,
    seeders INTEGER,
    torrent_name TEXT,
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    status TEXT NOT NULL,
    copied_at TEXT
)"""


def run(coro):
    return asyncio.run(coro)


async def _create(path):
    async with aiosqlite.connect(path) as db:
        await db.execute(SCHEMA)
        await db.commit()


def client():
    grabarr.CONFIG["AUTH_DISABLED"] = "true"
    return grabarr.app.test_client()


def fresh_db(monkeypatch):
    path = os.path.join(tempfile.mkdtemp(), "test.db")
    monkeypatch.setattr(grabarr, "DB_PATH", path)
    monkeypatch.setattr(grabarr, "POLL_LOCK", asyncio.Lock())
    monkeypatch.setitem(grabarr.CONFIG, "DELUGE_URL", "http://deluge.test")
    run(_create(path))
    return path


def test_auth_required():
    old = grabarr.CONFIG["AUTH_DISABLED"]
    grabarr.CONFIG["AUTH_DISABLED"] = "false"
    try:
        c = grabarr.app.test_client()
        assert run(c.get("/api/health")).status_code == 401
        r = run(c.get("/api/health", headers={"X-Authentik-Username": "u"}))
        assert r.status_code == 200 and run(r.json) == {"status": "ok"}
    finally:
        grabarr.CONFIG["AUTH_DISABLED"] = old


def test_search(monkeypatch):
    fresh_db(monkeypatch)

    async def fake_tmdb_get(_client, path, _params):
        if path == "/search/movie":
            return {"results": [{"id": 1, "title": "Godzilla", "release_date": "2023-01-01",
                                 "poster_path": "/p.jpg", "popularity": 10}]}
        if path == "/search/tv":
            return {"results": [{"id": 2, "name": "Show", "first_air_date": "2020-01-01",
                                 "poster_path": None, "popularity": 5}]}
        if path.startswith("/tv/"):
            return {"number_of_seasons": 3}
        return {}

    async def fake_fetchall(_sql, _params=()):
        return []

    monkeypatch.setattr(grabarr, "tmdb_get", fake_tmdb_get)
    monkeypatch.setattr(grabarr, "db_fetchall", fake_fetchall)
    monkeypatch.setattr(grabarr, "get_plex_ids", fake_plex_ids)

    c = client()
    r = run(c.get("/api/search?query=godzilla"))
    assert r.status_code == 200
    items = run(r.json)
    assert items[0]["title"] == "Godzilla" and items[0]["badge"] is None
    tv = [i for i in items if i["media_type"] == "tv"][0]
    assert tv["number_of_seasons"] == 3


async def fake_plex_ids(_media_type=None):
    return set()


def test_request_flow(monkeypatch):
    fresh_db(monkeypatch)

    async def fake_detail(_media_type, _tmdb_id):
        return {"title_original": "Foreign Movie", "title_pl": "Obcy Film", "year": "2023",
                "origin_country": [], "animation": False, "number_of_seasons": None}

    async def fake_none(*_a):
        return None

    monkeypatch.setattr(grabarr, "tmdb_detail", fake_detail)
    monkeypatch.setattr(grabarr, "find_and_grab", fake_none)
    monkeypatch.setattr(grabarr, "get_plex_ids", fake_plex_ids)

    c = client()
    r = run(c.post("/api/request", json={"tmdb_id": 1, "media_type": "movie"}))
    assert r.status_code == 200 and run(r.json)["status"] == "no_release"

    r = run(c.post("/api/request", json={"tmdb_id": 1, "media_type": "movie", "language": "pl"}))
    assert r.status_code == 200 and run(r.json)["status"] == "no_match_pl"
    rid = run(r.json)["id"]

    async def fake_release(*_a):
        return {"title": "Foreign.Movie.2023.2160p", "indexerId": 1, "guid": "g1",
                "infoHash": "abc", "size": 1024**3, "seeders": 10}

    monkeypatch.setattr(grabarr, "find_and_grab", fake_release)
    r = run(c.post(f"/api/requests/{rid}/fallback-en"))
    assert r.status_code == 200 and run(r.json)["status"] == "sent"

    r = run(c.post("/api/request", json={"tmdb_id": 1, "media_type": "movie"}))
    assert r.status_code == 409


def test_request_plex_duplicate(monkeypatch):
    fresh_db(monkeypatch)

    async def fake_plex(media_type):
        return {123} if media_type == "movie" else set()

    monkeypatch.setattr(grabarr, "get_plex_ids", lambda mt: fake_plex(mt))
    c = client()
    r = run(c.post("/api/request", json={"tmdb_id": 123, "media_type": "movie"}))
    assert r.status_code == 409


def test_poll_copies_completed(monkeypatch):
    fresh_db(monkeypatch)
    src_dir = tempfile.mkdtemp()
    dest_dir = tempfile.mkdtemp()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with open(os.path.join(src_dir, "Movie.2023.2160p"), "w") as f:
        f.write("data")
    run(grabarr.db_execute(
        "INSERT INTO requests (tmdb_id, media_type, season, title, year, language_target,"
        " release_name, requested_by, requested_at, status)"
        " VALUES (1, 'movie', NULL, 'Movie', 2023, 'en', 'Movie.2023.2160p', 't', ?, 'sent')",
        (now,),
    ))

    async def fake_torrents():
        return {"abc123": {"name": "Movie.2023.2160p", "state": "Seeding", "progress": 100.0,
                           "save_path": src_dir, "move_completed": False}}

    monkeypatch.setattr(grabarr, "deluge_torrents", fake_torrents)
    monkeypatch.setitem(grabarr.CONFIG, "MOVIES_DIR", dest_dir)
    monkeypatch.setitem(grabarr.CONFIG, "SERIES_DIR", dest_dir)

    run(grabarr.poll_once())
    assert open(os.path.join(dest_dir, "Movie.2023.2160p")).read() == "data"
    assert open(os.path.join(src_dir, "Movie.2023.2160p")).read() == "data"
    rows = run(grabarr.db_fetchall("SELECT status, copied_at FROM requests"))
    assert rows[0]["status"] == "done" and rows[0]["copied_at"]


def test_poll_marks_downloading(monkeypatch):
    fresh_db(monkeypatch)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    run(grabarr.db_execute(
        "INSERT INTO requests (tmdb_id, media_type, season, title, year, language_target,"
        " release_name, requested_by, requested_at, status)"
        " VALUES (1, 'movie', NULL, 'Movie', 2023, 'en', 'Movie.2023.1080p', 't', ?, 'sent')",
        (now,),
    ))

    async def fake_torrents():
        return {"abc123": {"name": "Movie.2023.1080p", "state": "Downloading",
                           "progress": 40.0, "save_path": "/tmp", "move_completed": False}}

    monkeypatch.setattr(grabarr, "deluge_torrents", fake_torrents)
    monkeypatch.setitem(grabarr.CONFIG, "MOVIES_DIR", "")
    monkeypatch.setitem(grabarr.CONFIG, "SERIES_DIR", "")

    run(grabarr.poll_once())
    rows = run(grabarr.db_fetchall("SELECT status, info_hash, torrent_name FROM requests"))
    assert rows[0]["status"] == "downloading"
    assert rows[0]["info_hash"] == "abc123"
    assert rows[0]["torrent_name"] == "Movie.2023.1080p"


def test_poll_file_not_found_after_24h(monkeypatch):
    fresh_db(monkeypatch)
    old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=25)).isoformat()
    run(grabarr.db_execute(
        "INSERT INTO requests (tmdb_id, media_type, season, title, year, language_target,"
        " release_name, requested_by, requested_at, status)"
        " VALUES (1, 'movie', NULL, 'Movie', 2023, 'en', 'Movie.2023.1080p', 't', ?, 'sent')",
        (old,),
    ))

    async def fake_torrents():
        return {}

    monkeypatch.setattr(grabarr, "deluge_torrents", fake_torrents)
    run(grabarr.poll_once())
    rows = run(grabarr.db_fetchall("SELECT status FROM requests"))
    assert rows[0]["status"] == "file_not_found"
