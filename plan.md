# Grabarr — plan wdrożenia

## 1. Cel
Jedna aplikacja webowa. Użytkownik wpisuje tytuł. Klika "Pobierz". System wybiera release, pobiera przez Prowlarr do Deluge i kopiuje do bibliotek Plex. Bez Radarr. Bez Sonarr. Bez kont. Bez monitorowania.

## 2. Wymagania twarde
- Auth wyłącznie przez forward_auth (Authentik) w **istniejącym, zewnętrznym Caddy** użytkownika — aplikacja NIE bundle'uje własnego reverse proxy ani wspólnej sieci Docker; wystawia port na hoście. Aplikacja czyta nagłówek użytkownika. Zero własnych kont, haseł, rejestracji.
- Filtry release: rozdzielczość >= 1080p, rozmiar <= 30 GB na sezon (twardy limit; release pokrywający K sezonów: limit 30*K GB). Preferencja 2160p gdy dostępna; w obrębie tej samej rozdzielczości sort po seeders malejąco.
- Brak monitorowania, upgrade'ów, RSS. Request = jednorazowa akcja.
- Kopiowanie po ukończeniu. Nie move. Nie hardlink. Oryginał zostaje w katalogu Deluge do seedowania.
- Sprzątanie seedów robi AutoRemovePlus w Deluge (min 3 dni LUB ratio 1.0). Poza aplikacją.
- Użytkownik nigdy nie widzi listy release'ów. Widzi pozycje TMDB i statusy.
- Język: domyślnie EN. PL gdy origin_country zawiera PL lub genre zawiera Animation. Ręczny toggle PL/EN przy requeście nadpisuje default.
- Brak duplikatów requestów: pozycja w Plex (badge "W bibliotece") lub aktywny request (badge "Pobieranie") → brak przycisku pobierania. Backend odrzuca duplikat (409).

## 3. Stack
- Backend: Python 3.12, Quart, httpx, hypercorn, aiosqlite.
- Frontend: jeden plik index.html, vanilla JS, serwowany przez Quart.
- Persystencja: SQLite (aiosqlite), jedna tabela.
- Jeden obraz Docker.

## 4. Zależności zewnętrzne
- Prowlarr: URL + X-Api-Key.
  - `GET /api/v1/search?query={tytul}&categories=2000` (filmy) / `categories=5000` (seriale).
  - `POST /api/v1/search` body = pełny obiekt ReleaseResource (zwrotka z wyszukiwania) — grab. Prowlarr sam wysyła do Deluge. ReleaseResource zawiera infoHash.
- TMDB: API key v3.
  - `/search/movie`, `/search/tv`.
  - `/movie/{id}`, `/tv/{id}` — origin_country, genres, tytuł oryginalny, tytuł polski, number_of_seasons.
- Plex: URL + token admina.
  - `/library/sections` — id sekcji filmów i seriali po nazwie.
  - `/library/sections/{id}/all` — lista GUIDów. Badge po GUIDach `tmdb://{id}`. Cache co 6 h.
- Deluge: Web API URL + hasło.
  - `POST /json` `auth.login` → cookie sesji.
  - `POST /json` `web.update_ui` z filter ukończonych torrentów — nazwa, hash, rozmiar, ścieżka plików.
- Filesystem: write do katalogów filmów i seriali.

## 5. Flow
1. `GET /api/search?query=` → TMDB (movie + tv), scalone, sort po popularności. Każdy wynik: tmdb_id, typ, tytuł, rok, plakat, number_of_seasons (tv), badge "W bibliotece" (cache Plex, match po tmdb:// GUID) lub "Pobieranie" (aktywny request w DB). Badge → brak przycisku pobierania.
2. UI: dla seriali wybór sezonu (1..number_of_seasons) lub "Cały serial". Użytkownik klika "Pobierz". Opcjonalny toggle PL/EN. `POST /api/request {tmdb_id, media_type, season?, language?}` — season: int dla konkretnego sezonu, brak/null = cały serial.
3. Backend: duplikat (pozycja w Plex lub aktywny request dla tego samego tmdb_id+media_type) → 409. Poziom serialu, zgodnie z badge "Pobieranie".
4. Backend ustala język docelowy: override > reguła (PL dla PL/Animation) > EN.
5. Zapytania Prowlarr: tytuł oryginalny. Gdy cel PL: dodatkowo polski tytuł z TMDB. Wyniki scalone, dedupe po (title, indexerId), pierwszeństwo wyższego seeders.
6. Klasyfikacja każdego release tytułowym matcherem (sekcja 7) + parser sezonów: `S\d{2}` (jeden sezon), `S\d{2}-S?\d{2}` (zakres włącznie, np. S01-03 = 3 sezony), `S\d{2}E\d{2}` (pojedynczy odcinek — nie pokrywa żadnego sezonu, odpada w filtrowaniu), brak tokenu = wszystkie sezony. Case-insensitive.
7. Filtrowanie: klasa zgodna z celem, rozdzielczość >= 1080p, pokrycie sezonów zgodne z requestem (konkretny sezon: release musi go pokrywać; cały serial: release musi pokrywać wszystkie sezony z TMDB), size <= 30 GB × pokryte sezony.
8. Sort: rozdzielczość malejąco (preferencja 2160p), w obrębie rozdzielczości seeders desc. Grab pierwszego spełniającego warunki: `POST /api/v1/search` z pełnym obiektem release.
9. Statusy: `sent → downloading → copying → done`. Albo `no_match_pl` / `no_release`.
10. Brak release'u PL: status "Brak PL" + przycisk "Pobierz EN". Fallback tylko ręczny.

## 6. Wykrywanie ukończenia i kopiowanie
- Poll Deluge Web API co 60 s: `auth.login` (sesja cookie), `web.update_ui` z filter torrentów w stanie ukończonym.
- Pierwsze sprawdzenie requestu: 5 s po grabie, asynchronicznie (torrent musi zdążyć się zarejestrować w Deluge).
- Dopasowanie torrent → request: najpierw dokładnie po infoHash z wyniku Prowlarr; gdy infoHash null, po najlepszym dopasowaniu nazwy (tolerancja ~15%). Torrenty już przypisane do innych requestów odpadają.
- Przypisanie torrent → request zapisywane. Jeden torrent nigdy do dwóch requestów.
- Torrent znaleziony, ale nieukończony → status `downloading`.
- Ścieżka plików torrenta z API (`move_completed_path`/`save_path` + lista plików). Zero skanowania katalogów.
- Film: copy {name} → `MOVIES_DIR/`.
- Serial: copy {name} → `SERIES_DIR/`.
- Kopiowanie: zero hardlinków.
- Po skopiowaniu: status `done`. Brak dalszych akcji.
- Brak dopasowania po 24 h: status `file_not_found`.

## 7. Matcher języka
Tabela w `/app/config.yaml` (stała ścieżka w obrazie). Nie w kodzie. Tokenizacja tytułu po znakach niealfanumerycznych. Case-insensitive.
- `pl_audio`: pl, lektor, lektorpl, dubpl, pldub, dubbing, dubbingpl, polski, pol
- `subs_only`: napisy, napisypl, plsubbed, subpl
- `multi`: multi — kandydat PL tylko gdy towarzyszy mu token z pl_audio
- `en`: eng, english
- Brak dopasowania: `unknown`
Reguła: `subs_only` NIGDY nie spełnia celu PL-audio. `unknown` nie spełnia celu PL. Przy celu EN: `en`, `unknown`, `multi` i `subs_only` przechodzą.

## 8. Model danych
Tabela `requests`:
`id, tmdb_id, media_type, season, title, year, language_target, release_name, indexer_id, guid, info_hash, size, seeders, torrent_name, requested_by, requested_at, status, copied_at`
Statusy: `sent, downloading, copying, done, no_match_pl, no_release, file_not_found`.

## 9. API
- `GET /` — UI.
- `GET /api/search?query=`
- `GET /api/requests` — lista statusów dla UI.
- `POST /api/request` — {tmdb_id, media_type, season?, language?}; 409 przy duplikacie (w Plex lub aktywny request)
- `POST /api/requests/{id}/fallback-en`
- `GET /api/health`

## 10. Autoryzacja
- Middleware: brak nagłówka użytkownika → 401. Wyjątek: `AUTH_DISABLED=true` dla dev.
- Nagłówek konfigurowalny: `REMOTE_USER_HEADER` (domyślnie `X-Authentik-Username`).
- `requested_by` zapisywane do tabeli. Audyt kto co pobrał.

## 11. Konfiguracja

Wszystkie wartości plain text w `docker-compose.yml`. Bez interpolacji `${}`, bez `.env`, bez `env_file`.

PROWLARR_URL=http://prowlarr:9696

PROWLARR_API_KEY=

TMDB_API_KEY=

PLEX_URL=http://plex:32400

PLEX_TOKEN=

PLEX_LIBRARY_MOVIES=Filmy

PLEX_LIBRARY_SERIES=Seriale

DELUGE_URL=http://deluge:8112

DELUGE_PASSWORD=

MOVIES_DIR=/plex/movies

SERIES_DIR=/plex/series

DB_PATH=/data/grabarr.db

REMOTE_USER_HEADER=X-Authentik-Username

POLL_SECONDS=60

## 12. Milestones
- [x] M1: szkielet Quart + config + auth middleware.
- [x] M2: TMDB search + UI lista (plakaty, badge Plex/Pobieranie, wybór sezonu).
- [x] M3: Prowlarr search + matcher języka + parser sezonów + filtry + grab.
- [x] M4: Plex cache + badge "W bibliotece" + blokada duplikatów.
- [x] M5: poller Deluge API (infoHash match) + kopiowanie.
- [x] M6: Dockerfile + testy E2E.

## 13. Kryteria akceptacji
- Search "Godzilla": lista TMDB z plakatami. Pozycja z biblioteki ma badge, bez przycisku pobierania.
- Request EN na film zagraniczny: release >= 1080p, <= 30 GB, preferencja 2160p gdy dostępna, w obrębie rozdzielczości max seeders, klasa en/unknown/subs_only.
- Request PL na film polski lub animowany: release z tokenem pl_audio. subs_only odpada.
- Request serialu S02: release pokrywający sezon 2 (S02 lub zakres zawierający 02), <= 30 GB. Release pojedynczego odcinka (S02E..) odpada.
- Request całego serialu (N sezonów): release pokrywający wszystkie sezony, <= 30*N GB.
- Toggle nadpisuje default.
- Po ukończeniu: kopia w katalogu docelowym. Oryginał nadal w katalogu Deluge.
- Brak nagłówka auth → 401.
- Brak PL → status + przycisk "Pobierz EN".
- Duplikat requestu (w Plex lub aktywny) → 409, UI bez przycisku.

## 14. Znane ograniczenia i edge case'y
- Badge Plex dla seriali jest na poziomie serialu (GUID tmdb:// jest per-show, nie per-sezon). Serial z tylko S01 w Plex ukrywa przyciski także dla S02. Zaakceptowane ograniczenie.
- Zakresy sezonów w tytułach (S01-03) liczone włącznie; limit rozmiaru skaluje się z liczbą pokrytych sezonów.
- Release bez tokenu sezonu traktowany jako komplet; jeśli faktycznie jest częściowy, limit 30×N może go przepuścić — zaakceptowane ryzyko.
- Nazwa torrenta w Deluge może odbiegać od nazwy release'u z Prowlarr (np. przepakowany release). Dopasowanie: infoHash (dokładne), fallback nazwa z tolerancją ~15%. Brak trafienia → `file_not_found` po 24 h.
- Plex scan dużej biblioteki: full scan w tle co 6 h, nie per request.
- Prowlarr zwraca duplikaty z wielu indexerów przy scalaniu zapytań PL/EN tytułów: dedupe po (title, indexerId), pierwszeństwo wyższego seeders.
- Race dwóch równoczesnych requestów na tę samą pozycję: blokada na poziomie backendu (409), nie tylko UI.
- Seriale wydawnictw bez packów sezonowych (tylko pojedyncze odcinki) → `no_release`. Brak pobierania per-odcinek w MVP.

## 15. Zasady dla agenta realizującego
- Kod zwięzły, bez zbędnych komentarzy, bez nadmiarowej abstrakcji.
- Zero funkcji poza niniejszą specyfikacją.
- Zero pomysłów "warto dodać".
- Tabela tokenów języka w config.yaml, nie w kodzie.
- Nie dodawać ffprobe ani jakiejkolwiek weryfikacji po pobraniu.
- Nie dodawać wyboru release'u przez użytkownika.