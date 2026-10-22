import asyncio
import base64
import datetime
import difflib
import logging
import os
import re
import shutil
import time
import xml.etree.ElementTree as ET

import aiosqlite
import httpx
import yaml
from quart import Quart, jsonify, request, send_from_directory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("grabarr")

app = Quart(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB_PATH = os.path.join(BASE_DIR, "data", "grabarr.db")

CONFIG = {k: os.environ.get(k, d) for k, d in {
    "PROWLARR_URL": "",
    "PROWLARR_API_KEY": "",
    "TMDB_API_KEY": "",
    "PLEX_URL": "",
    "PLEX_TOKEN": "",
    "PLEX_LIBRARY_MOVIES": "Filmy",
    "PLEX_LIBRARY_SERIES": "Seriale",
    "PLEX_CACHE_HOURS": "6",
    "DELUGE_URL": "",
    "DELUGE_PASSWORD": "",
    "MOVIES_DIR": "",
    "SERIES_DIR": "",
    "MAX_SIZE_GB": "30",
    "MIN_RESOLUTION": "1080",
    "REMOTE_USER_HEADER": "X-Authentik-Username",
    "AUTH_DISABLED": "false",
    "POLL_SECONDS": "60",
    "PROWLARR_TIMEOUT": "300",
}.items()}

for key in ("PLEX_CACHE_HOURS", "MAX_SIZE_GB", "MIN_RESOLUTION", "POLL_SECONDS", "PROWLARR_TIMEOUT"):
    CONFIG[key] = int(CONFIG[key])
for key in ("PROWLARR_URL", "PLEX_URL", "DELUGE_URL"):
    CONFIG[key] = CONFIG[key].rstrip("/")

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p/w342"
ACTIVE_STATUSES = ("searching", "sent", "downloading", "copying")

DEFAULT_CONFIG_YAML = """lang_tokens:
  pl_audio:
    - pl
    - lektor
    - lektorpl
    - dubpl
    - pldub
    - dubbing
    - dubbingpl
    - polski
    - pol
  subs_only:
    - napisy
    - napisypl
    - plsubbed
    - subpl
  multi:
    - multi
  en:
    - eng
    - english
"""


def load_lang_tokens():
    path = os.path.join(BASE_DIR, "data", "config.yaml")
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(DEFAULT_CONFIG_YAML)
    with open(path) as f:
        return yaml.safe_load(f)["lang_tokens"]


LANG_TOKENS = load_lang_tokens()

PLEX_CACHE = {"movies": set(), "series": set(), "ts": 0.0}

TMDB_GUID = re.compile(r"tmdb://(\d+)")


def extract_tmdb_ids(m):
    out = []
    for s in (m.get("guid") or "", *(g.get("id") or "" for g in m.iter("Guid"))):
        match = TMDB_GUID.search(s)
        if match:
            out.append(int(match.group(1)))
    return out


def plex_items(root):
    return [m for m in root.iter() if m.get("guid")]


async def refresh_plex_cache():
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{CONFIG['PLEX_URL']}/library/sections",
            params={"X-Plex-Token": CONFIG["PLEX_TOKEN"]},
        )
        r.raise_for_status()
        sections = {
            d.get("title"): d.get("key")
            for d in ET.fromstring(r.content).iter("Directory")
        }
        ids = {}
        for media_type, name in (("movies", "PLEX_LIBRARY_MOVIES"), ("series", "PLEX_LIBRARY_SERIES")):
            key = sections.get(CONFIG[name])
            if not key:
                log.warning("plex section not found: %s", CONFIG[name])
                continue
            r = await client.get(
                f"{CONFIG['PLEX_URL']}/library/sections/{key}/all",
                params={"X-Plex-Token": CONFIG["PLEX_TOKEN"]},
            )
            r.raise_for_status()
            items = plex_items(ET.fromstring(r.content))
            ids[media_type] = {i for m in items for i in extract_tmdb_ids(m)}
            log.info(
                "plex: section %r (%s): %d items, %d tmdb ids",
                CONFIG[name], media_type, len(items), len(ids[media_type]),
            )
            if items and not ids[media_type]:
                log.warning(
                    "plex: section %r has %d items but no tmdb:// ids (legacy agent?)",
                    CONFIG[name], len(items),
                )
    for media_type, s in ids.items():
        PLEX_CACHE[media_type] = s
    PLEX_CACHE["ts"] = time.time()


async def get_plex_ids(media_type):
    if time.time() - PLEX_CACHE["ts"] > CONFIG["PLEX_CACHE_HOURS"] * 3600:
        try:
            await refresh_plex_cache()
        except (httpx.HTTPError, ValueError, TypeError):
            log.warning("plex refresh failed", exc_info=True)
    return PLEX_CACHE["movies" if media_type == "movie" else "series"]


async def db_fetchall(sql, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        return [dict(row) for row in await cur.fetchall()]


async def tmdb_get(client, path, params):
    r = await client.get(
        f"{TMDB_BASE}{path}",
        params={"api_key": CONFIG["TMDB_API_KEY"], "language": "pl-PL", **params},
    )
    r.raise_for_status()
    return r.json()


async def db_execute(sql, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, params)
        await db.commit()
        return cur.lastrowid


async def migrate_db(db):
    cur = await db.execute("PRAGMA table_info(requests)")
    cols = {r[1] for r in await cur.fetchall()}
    if "error" not in cols:
        await db.execute("ALTER TABLE requests ADD COLUMN error TEXT")


def classify_language(title):
    tokens = set(re.findall(r"[a-z0-9]+", title.lower()))
    for cls in ("pl_audio", "subs_only", "en", "multi"):
        if any(t in tokens for t in LANG_TOKENS[cls]):
            return cls
    return "unknown"


def language_passes(cls, target):
    return cls == "pl_audio" if target == "pl" else cls != "pl_audio"


def parse_seasons(title):
    t = title.lower()
    if re.search(r"s\d{2}e\d{2}", t):
        return set()
    covered = set()
    for m in re.finditer(r"s(\d{2})-s?(\d{2})", t):
        covered.update(range(int(m.group(1)), int(m.group(2)) + 1))
    rest = re.sub(r"s\d{2}-s?\d{2}", " ", t)
    covered.update(int(m.group(1)) for m in re.finditer(r"s(\d{2})", rest))
    return covered or None


def parse_resolution(title):
    m = re.search(r"(\d{3,4})p", title.lower())
    return int(m.group(1)) if m else 0


def season_coverage(covered, season_req, number_of_seasons):
    total = number_of_seasons or 1
    if covered == set():
        return None
    if season_req is not None:
        if covered is not None and season_req not in covered:
            return None
        return len(covered) if covered is not None else total
    if covered is not None and not set(range(1, total + 1)) <= covered:
        return None
    return total


def filter_releases(releases, target, media_type, season_req, number_of_seasons, stats=None):
    best = {}
    for r in releases:
        key = ((r.get("title") or "").lower(), r.get("indexerId"))
        if key not in best or (r.get("seeders") or 0) > (best[key].get("seeders") or 0):
            best[key] = r
    if stats is not None:
        stats.update(total=len(releases), deduped=len(best), lang=0, resolution=0, seasons=0, size=0)
    out = []
    for r in best.values():
        title = r.get("title") or ""
        if not language_passes(classify_language(title), target):
            if stats is not None:
                stats["lang"] += 1
            continue
        res = parse_resolution(title)
        if res < CONFIG["MIN_RESOLUTION"]:
            if stats is not None:
                stats["resolution"] += 1
            continue
        if media_type == "tv":
            k = season_coverage(parse_seasons(title), season_req, number_of_seasons)
            if k is None:
                if stats is not None:
                    stats["seasons"] += 1
                continue
        else:
            k = 1
        if (r.get("size") or 0) > CONFIG["MAX_SIZE_GB"] * k * 1024**3:
            if stats is not None:
                stats["size"] += 1
            continue
        out.append(r)
    out.sort(key=lambda r: (-parse_resolution(r["title"]), -(r.get("seeders") or 0)))
    return out


def prowlarr_headers():
    return {"X-Api-Key": CONFIG["PROWLARR_API_KEY"]}


async def prowlarr_search(client, query, category):
    r = await client.get(
        f"{CONFIG['PROWLARR_URL']}/api/v1/search",
        params={"query": query, "categories": category},
        headers=prowlarr_headers(),
    )
    r.raise_for_status()
    return r.json()


async def prowlarr_grab(client, release):
    r = await client.post(
        f"{CONFIG['PROWLARR_URL']}/api/v1/search",
        json=release,
        headers=prowlarr_headers(),
    )
    r.raise_for_status()


async def tmdb_detail(media_type, tmdb_id):
    async with httpx.AsyncClient(timeout=15) as client:
        d = await tmdb_get(client, f"/{media_type}/{tmdb_id}", {})
    return {
        "title_original": d.get("original_title") or d.get("original_name") or "",
        "title_pl": d.get("title") or d.get("name") or "",
        "year": (d.get("release_date") or d.get("first_air_date") or "")[:4] or None,
        "origin_country": d.get("origin_country") or [],
        "animation": any(g.get("id") == 16 for g in d.get("genres") or []),
        "number_of_seasons": d.get("number_of_seasons"),
    }


async def find_and_grab(media_type, d, target, season_req):
    category = 2000 if media_type == "movie" else 5000

    def query(title):
        return f"{title} {d['year']}" if media_type == "movie" and d["year"] else title

    queries = [query(d["title_original"])]
    if target == "pl" and d["title_pl"] and d["title_pl"] != d["title_original"]:
        queries.append(query(d["title_pl"]))
    async with httpx.AsyncClient(timeout=CONFIG["PROWLARR_TIMEOUT"]) as client:
        results = await asyncio.gather(
            *[prowlarr_search(client, q, category) for q in queries]
        )
        releases = [r for res in results for r in res]
        log.info("prowlarr: %d raw results for %r target=%s", len(releases), queries, target)
        stats = {}
        candidates = filter_releases(
            releases, target, media_type,
            season_req if media_type == "tv" else None,
            d["number_of_seasons"] if media_type == "tv" else None,
            stats=stats,
        )
        if not candidates:
            log.warning("prowlarr: no release for %r target=%s stats=%s", queries, target, stats)
            return None
        grab_release = candidates[0]
        log.info(
            "prowlarr: grabbing %r indexer=%s size=%s seeders=%s",
            grab_release.get("title"), grab_release.get("indexerId"),
            grab_release.get("size"), grab_release.get("seeders"),
        )
        await prowlarr_grab(client, grab_release)
        return grab_release


ACTIVE_RIDS = set()


async def perform_request(rid):
    if rid in ACTIVE_RIDS:
        return
    ACTIVE_RIDS.add(rid)
    try:
        row = (await db_fetchall("SELECT * FROM requests WHERE id = ?", (rid,)))[0]
        if row["status"] != "searching":
            return
        d = await tmdb_detail(row["media_type"], row["tmdb_id"])
        if row["language_target"]:
            target = row["language_target"]
        elif "PL" in d["origin_country"] or d["animation"]:
            target = "pl"
        else:
            target = "en"
        release = await find_and_grab(row["media_type"], d, target, row["season"])
        status = "sent" if release else ("no_match_pl" if target == "pl" else "no_release")
        await db_execute(
            "UPDATE requests SET title = ?, year = ?, language_target = ?, release_name = ?,"
            " indexer_id = ?, guid = ?, info_hash = ?, size = ?, seeders = ?, status = ? WHERE id = ?",
            (
                d["title_pl"] or d["title_original"],
                int(d["year"]) if d["year"] else None,
                target,
                release and release.get("title"), release and release.get("indexerId"),
                release and release.get("guid"), release and release.get("infoHash"),
                release and release.get("size"), release and release.get("seeders"),
                status, rid,
            ),
        )
        if status == "sent":
            spawn_background(first_check(rid))
    except Exception as exc:
        log.warning("search failed for request %s", rid, exc_info=True)
        await db_execute(
            "UPDATE requests SET status = 'search_failed', error = ? WHERE id = ?",
            (str(exc) or type(exc).__name__, rid),
        )
    finally:
        ACTIVE_RIDS.discard(rid)


async def resume_searching():
    rows = await db_fetchall("SELECT id FROM requests WHERE status = ?", ("searching",))
    return [spawn_background(perform_request(r["id"])) for r in rows]


POLL_LOCK = asyncio.Lock()
BACKGROUND_TASKS = set()


def spawn_background(coro):
    task = asyncio.create_task(coro)
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)
    return task


def hash_hex(info_hash):
    s = (info_hash or "").lower()
    if re.fullmatch(r"[a-z2-7]{32}", s):
        try:
            return base64.b32decode(s.upper(), casefold=True).hex()
        except ValueError:
            return s
    return s


async def deluge_call(client, method, params):
    r = await client.post(
        f"{CONFIG['DELUGE_URL']}/json",
        json={"method": method, "params": params, "id": 1},
    )
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"deluge: {data['error'].get('message')}")
    return data["result"]


async def deluge_torrents():
    keys = ["name", "state", "progress", "save_path", "move_completed", "move_completed_path"]
    async with httpx.AsyncClient(timeout=15) as client:
        ok = await deluge_call(client, "auth.login", [CONFIG["DELUGE_PASSWORD"]])
        if not ok:
            raise RuntimeError("deluge: auth failed")
        result = await deluge_call(client, "web.update_ui", [keys, {}])
    return result.get("torrents", {})


def torrent_completed(t):
    return t.get("state") == "Seeding" or t.get("progress") == 100.0


def torrent_save_path(t):
    if t.get("move_completed"):
        return t.get("move_completed_path") or t.get("save_path")
    return t.get("save_path")


def safe_name(name):
    parts = [p for p in str(name or "").replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return os.path.join(*parts) if parts else ""


def do_copy(t, dest_dir):
    name = safe_name(t["name"])
    if not name:
        raise ValueError("torrent has no usable name")
    src = os.path.join(torrent_save_path(t), name)
    dest = os.path.join(dest_dir, name)
    if os.path.isdir(src):
        shutil.copytree(src, dest, dirs_exist_ok=True, copy_function=shutil.copy2)
    else:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)


async def copy_and_complete(row, t_hash, t):
    dest_dir = CONFIG["MOVIES_DIR"] if row["media_type"] == "movie" else CONFIG["SERIES_DIR"]
    if not dest_dir:
        log.error("destination dir not configured for request %s", row["id"])
        return
    await db_execute(
        "UPDATE requests SET status = 'copying', info_hash = COALESCE(info_hash, ?),"
        " torrent_name = COALESCE(torrent_name, ?) WHERE id = ?",
        (t_hash, t["name"], row["id"]),
    )
    try:
        await asyncio.to_thread(do_copy, t, dest_dir)
    except Exception:
        log.error("copy failed for request %s", row["id"], exc_info=True)
        await db_execute(
            "UPDATE requests SET status = 'downloading' WHERE id = ?", (row["id"],)
        )
        return
    await db_execute(
        "UPDATE requests SET status = 'done', copied_at = ? WHERE id = ?",
        (datetime.datetime.now(datetime.timezone.utc).isoformat(), row["id"]),
    )
    log.info("request %s copied", row["id"])


async def poll_once():
    if not CONFIG["DELUGE_URL"]:
        return
    async with POLL_LOCK:
        torrents = await deluge_torrents()
        assigned = {
            hash_hex(r["info_hash"])
            for r in await db_fetchall("SELECT info_hash FROM requests WHERE info_hash IS NOT NULL")
        }
        rows = await db_fetchall(
            "SELECT * FROM requests WHERE status IN (?, ?, ?)",
            ("sent", "downloading", "copying"),
        )
        now = datetime.datetime.now(datetime.timezone.utc)
        for row in rows:
            if row["status"] == "copying":
                try:
                    age = now - datetime.datetime.fromisoformat(row["requested_at"])
                except ValueError:
                    continue
                if age < datetime.timedelta(hours=1):
                    continue
                log.warning("request %s stuck in copying, requeueing", row["id"])
                await db_execute(
                    "UPDATE requests SET status = 'downloading' WHERE id = ?", (row["id"],)
                )
                continue
            t_hash, t = None, None
            if row["info_hash"]:
                t_hash = hash_hex(row["info_hash"])
                t = torrents.get(t_hash)
            if t is None and row["info_hash"] is None:
                rel = (row["release_name"] or "").lower()
                best, match_h = 0.85, None
                for h, cand in torrents.items():
                    if h in assigned:
                        continue
                    ratio = difflib.SequenceMatcher(None, rel, (cand.get("name") or "").lower()).ratio()
                    if ratio >= best:
                        best, match_h = ratio, h
                if match_h:
                    assigned.add(match_h)
                    t_hash, t = match_h, torrents[match_h]
            if t is None:
                if row["status"] == "sent":
                    try:
                        age = now - datetime.datetime.fromisoformat(row["requested_at"])
                    except ValueError:
                        continue
                    if age > datetime.timedelta(hours=24):
                        await db_execute(
                            "UPDATE requests SET status = 'file_not_found' WHERE id = ?", (row["id"],)
                        )
                continue
            if torrent_completed(t):
                await copy_and_complete(row, t_hash, t)
            elif row["status"] == "sent":
                await db_execute(
                    "UPDATE requests SET status = 'downloading', info_hash = COALESCE(info_hash, ?),"
                    " torrent_name = COALESCE(torrent_name, ?) WHERE id = ?",
                    (t_hash, t["name"], row["id"]),
                )


async def first_check(rid):
    await asyncio.sleep(5)
    try:
        await poll_once()
    except Exception:
        log.warning("first check for request %s failed", rid, exc_info=True)


async def poll_loop():
    while True:
        try:
            await poll_once()
        except Exception:
            log.warning("poll failed", exc_info=True)
        await asyncio.sleep(CONFIG["POLL_SECONDS"])


async def plex_refresh_loop():
    while True:
        try:
            await refresh_plex_cache()
        except Exception:
            log.warning("plex refresh failed", exc_info=True)
        await asyncio.sleep(CONFIG["PLEX_CACHE_HOURS"] * 3600)


@app.before_serving
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS requests (
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
            error TEXT,
            copied_at TEXT
        )""")
        await migrate_db(db)
        await db.commit()
    await resume_searching()
    spawn_background(poll_loop())
    spawn_background(plex_refresh_loop())


@app.before_request
async def auth():
    if CONFIG["AUTH_DISABLED"].lower() == "true":
        return
    if not request.headers.get(CONFIG["REMOTE_USER_HEADER"]):
        return jsonify({"error": "unauthorized"}), 401


@app.route("/")
async def index():
    return await send_from_directory("static", "index.html")


@app.route("/api/search")
async def api_search():
    query = request.args.get("query", "").strip()
    if not query:
        return jsonify({"error": "missing query"}), 400
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            movies, tv = await asyncio.gather(
                tmdb_get(client, "/search/movie", {"query": query}),
                tmdb_get(client, "/search/tv", {"query": query}),
            )
            tv_results = tv.get("results", [])[:20]
            tv_details = await asyncio.gather(*[
                tmdb_get(client, f"/tv/{r['id']}", {}) for r in tv_results
            ], return_exceptions=True)
    except httpx.HTTPError as exc:
        return jsonify({"error": f"tmdb: {exc}"}), 502
    items = []
    for r in movies.get("results", [])[:20]:
        items.append({
            "tmdb_id": r["id"],
            "media_type": "movie",
            "title": r.get("title") or r.get("original_title"),
            "year": (r.get("release_date") or "")[:4] or None,
            "poster": TMDB_IMG + r["poster_path"] if r.get("poster_path") else None,
            "popularity": r.get("popularity") or 0,
        })
    for r, d in zip(tv_results, tv_details):
        if isinstance(d, BaseException):
            continue
        items.append({
            "tmdb_id": r["id"],
            "media_type": "tv",
            "title": r.get("name") or r.get("original_name"),
            "year": (r.get("first_air_date") or "")[:4] or None,
            "poster": TMDB_IMG + r["poster_path"] if r.get("poster_path") else None,
            "number_of_seasons": d.get("number_of_seasons") or 0,
            "popularity": r.get("popularity") or 0,
        })
    items.sort(key=lambda i: i["popularity"], reverse=True)
    plex_ids = {"movie": await get_plex_ids("movie"), "tv": await get_plex_ids("tv")}
    active = await db_fetchall(
        f"SELECT tmdb_id, media_type FROM requests WHERE status IN ({','.join('?' * len(ACTIVE_STATUSES))})",
        ACTIVE_STATUSES,
    )
    active_keys = {(r["tmdb_id"], r["media_type"]) for r in active}
    for item in items:
        if item["tmdb_id"] in plex_ids[item["media_type"]]:
            item["badge"] = "library"
        else:
            item["badge"] = "downloading" if (item["tmdb_id"], item["media_type"]) in active_keys else None
    return jsonify(items)


@app.route("/api/requests")
async def api_requests():
    rows = await db_fetchall(
        "SELECT id, tmdb_id, media_type, season, title, year, language_target, "
        "status, error, requested_by, requested_at, copied_at FROM requests ORDER BY id DESC"
    )
    return jsonify(rows)


@app.route("/api/request", methods=["POST"])
async def api_request():
    body = await request.get_json() or {}
    tmdb_id = body.get("tmdb_id")
    media_type = body.get("media_type")
    season = body.get("season")
    language = body.get("language") or ""
    if isinstance(tmdb_id, bool) or not isinstance(tmdb_id, int) or media_type not in ("movie", "tv"):
        return jsonify({"error": "bad request"}), 400
    if media_type == "movie":
        season = None
    elif isinstance(season, bool) or (season is not None and (not isinstance(season, int) or season < 1)):
        return jsonify({"error": "bad season"}), 400
    if language not in ("", "pl", "en"):
        return jsonify({"error": "bad language"}), 400
    user = request.headers.get(CONFIG["REMOTE_USER_HEADER"]) or "dev"
    active = await db_fetchall(
        f"SELECT id FROM requests WHERE tmdb_id = ? AND media_type = ?"
        f" AND status IN ({','.join('?' * len(ACTIVE_STATUSES))})",
        (tmdb_id, media_type, *ACTIVE_STATUSES),
    )
    if active:
        return jsonify({"error": "already requested"}), 409
    if tmdb_id in await get_plex_ids(media_type):
        return jsonify({"error": "already in library"}), 409
    title, year = body.get("title"), body.get("year")
    if not isinstance(title, str) or not title:
        try:
            d = await tmdb_detail(media_type, tmdb_id)
        except httpx.HTTPError as exc:
            return jsonify({"error": f"tmdb: {exc}"}), 502
        title = d["title_pl"] or d["title_original"]
        year = d["year"]
    try:
        year = int(year)
    except (TypeError, ValueError):
        year = None
    rid = await db_execute(
        "INSERT INTO requests (tmdb_id, media_type, season, title, year, language_target,"
        " requested_by, requested_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'searching')",
        (
            tmdb_id, media_type, season, title, year, language, user,
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
        ),
    )
    spawn_background(perform_request(rid))
    return jsonify({"id": rid, "status": "searching"})


@app.route("/api/requests/<int:rid>/fallback-en", methods=["POST"])
async def fallback_en(rid):
    rows = await db_fetchall("SELECT * FROM requests WHERE id = ?", (rid,))
    if not rows:
        return jsonify({"error": "not found"}), 404
    row = rows[0]
    if row["status"] != "no_match_pl":
        return jsonify({"error": "bad status"}), 409
    await db_execute(
        "UPDATE requests SET status = 'searching', language_target = 'en' WHERE id = ?", (rid,)
    )
    spawn_background(perform_request(rid))
    return jsonify({"id": rid, "status": "searching"}), 202


@app.route("/api/health")
async def health():
    return jsonify({"status": "ok"})
