"""Opt-in Windows MusicBrainz enrichment and local cache for Sunnify.

A full MusicBrainz PostgreSQL dump is *not* a queryable SQLite DB.  The
separately downloaded official archives are retained intact; normal tagging
uses the rate-limited MusicBrainz web service and an actual small SQLite cache
in the same user-selected directory.  This distinction is deliberate.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import quote

import requests

log = logging.getLogger("sunnify.musicbrainz")
MB_API = "https://musicbrainz.org/ws/2"
DUMPS_BASE = "https://data.metabrainz.org/pub/musicbrainz/data/fullexport"
DUMPS = ("mbdump.tar.bz2", "mbdump-derived.tar.bz2")
USER_AGENT = "Sunnify-MusicBrainz-Fork/1.0 (https://github.com/sunnypatell/sunnify-spotify-downloader)"
CACHE_NAME = "musicbrainz-cache.sqlite3"
CACHE_SCHEMA = 1
# Queries on one IP must be serialized across parallel downloads.
_REQUEST_LOCK = threading.Lock()
_NEXT_REQUEST = 0.0


def default_database_folder() -> str:
    """Keep DB files outside the executable, particularly on Windows."""
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(root, "Sunnify", "MusicBrainz")
    return os.path.join(os.path.expanduser("~"), ".local", "share", "sunnify", "musicbrainz")


def normalize(value: str | None) -> str:
    value = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return " ".join("".join(c if c.isalnum() else " " for c in value).split())


def _similar(a: str, b: str) -> float:
    a, b = normalize(a), normalize(b)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _first_artist(artists: str) -> str:
    return re.split(r"\s*(?:,|;|\s+feat\.?\s+|\s+ft\.?\s+|\s+&\s+)\s*", artists, maxsplit=1, flags=re.I)[0].strip()


def _artist_credit(recording: dict) -> tuple[str, str]:
    names, ids = [], []
    for a in recording.get("artist-credit") or []:
        if isinstance(a, dict):
            name = a.get("name") or (a.get("artist") or {}).get("name")
            if name:
                names.append(str(name))
            mbid = (a.get("artist") or {}).get("id")
            if mbid:
                ids.append(mbid)
    return ", ".join(names), "; ".join(ids)


def _value_genres(entity: dict) -> str:
    candidates = []
    for item in entity.get("genres") or []:
        if isinstance(item, dict) and item.get("name") and (item.get("count") or 0) > 0:
            candidates.append((int(item.get("count") or 0), item["name"]))
    # Genre names are an official curated subset of community tags; don't use
    # arbitrary user tags as genres ("seen live", "favorites", etc).
    candidates.sort(key=lambda pair: (-pair[0], pair[1].casefold()))
    return "; ".join(str(name) for _, name in candidates[:3])


def _text(value):
    return str(value).strip() if value is not None else ""


def _has(value):
    return value is not None and value != "" and value != []


def merge_missing(tags: dict, additions: dict) -> dict:
    """Never replace existing Spotify metadata with guessed external values."""
    merged = dict(tags)
    for key, value in additions.items():
        if _has(value) and not _has(merged.get(key)):
            merged[key] = value
    return merged


class MetadataCache:
    """SQLite connection per operation: safe for PyQt worker threads."""

    def __init__(self, directory: str):
        self.directory = os.path.abspath(os.path.expanduser(directory))
        self.path = os.path.join(self.directory, CACHE_NAME)
        os.makedirs(self.directory, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, "
                "data TEXT NOT NULL, fetched_at INTEGER NOT NULL, schema_version INTEGER NOT NULL)"
            )

    @contextlib.contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        try:
            db.execute("PRAGMA busy_timeout=20000")
            yield db
            db.commit()
        finally:
            db.close()

    @staticmethod
    def key(title: str, artists: str, album: str, duration_ms=None) -> str:
        key = [normalize(title), normalize(_first_artist(artists)), normalize(album)]
        if duration_ms:
            try:
                key.append(str(round(int(duration_ms) / 10000)))
            except (ValueError, TypeError):
                pass
        return json.dumps(key, ensure_ascii=False)

    def get(self, key: str) -> dict | None:
        with self._connect() as db:
            record = db.execute(
                "SELECT data, fetched_at, schema_version FROM metadata WHERE key = ?", (key,)
            ).fetchone()
        if not record or record[2] != CACHE_SCHEMA:
            return None
        # Keep a positive verified match for a year; negative answers retry
        # after seven days in case MusicBrainz has since added the recording.
        data = json.loads(record[0])
        age = time.time() - record[1]
        if age > (365 if data else 7) * 86400:
            return None
        return data

    def put(self, key: str, data: dict):
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO metadata (key, data, fetched_at, schema_version) "
                "VALUES (?, ?, ?, ?)",
                (key, json.dumps(data, ensure_ascii=False), int(time.time()), CACHE_SCHEMA),
            )


class MusicBrainzClient:
    """MusicBrainz lookups with conservative matching and disk caching."""

    def __init__(self, folder: str, *, session: requests.Session | None = None):
        self.cache = MetadataCache(folder)
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    def _get(self, endpoint: str, params: dict | None = None) -> dict:
        global _NEXT_REQUEST
        # Hold lock until response completes, preventing parallel downloader
        # threads from bypassing the MusicBrainz 1-request/second IP limit.
        with _REQUEST_LOCK:
            delay = _NEXT_REQUEST - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            _NEXT_REQUEST = time.monotonic() + 1.2
            response = self.session.get(f"{MB_API}/{endpoint}", params={**(params or {}), "fmt": "json"}, timeout=(6, 16))
            if response.status_code in (429, 503):
                retry = response.headers.get("Retry-After", "")
                try:
                    wait = min(max(int(retry), 5), 60)
                except ValueError:
                    wait = 6
                _NEXT_REQUEST = max(_NEXT_REQUEST, time.monotonic() + wait)
                raise requests.HTTPError(f"MusicBrainz rate limit ({response.status_code})")
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _choose_recording(results: list, title: str, artists: str, duration_ms=None):
        best = None
        for item in results:
            if not isinstance(item, dict):
                continue
            credit, _ = _artist_credit(item)
            target_artist = _first_artist(artists)
            # Search API scores are a hint, not a substitute for matching.
            if _similar(item.get("title", ""), title) < 0.94:
                continue
            if not credit or max(
                _similar(_first_artist(credit), target_artist),
                _similar(credit, artists),
            ) < 0.82:
                continue
            if int(item.get("score") or 0) < 80:
                continue
            if duration_ms and item.get("length"):
                if abs(int(item["length"]) - int(duration_ms)) > 15000:
                    continue
            rank = (int(item.get("score") or 0), _similar(credit, artists))
            if best is None or rank > best[0]:
                best = (rank, item)
        return best[1] if best else None

    @staticmethod
    def _choose_release(releases: list, wanted_album: str) -> dict | None:
        # Never infer that an arbitrary compilation/pressing is the album.
        if not wanted_album:
            return None
        possible = [r for r in releases if _similar(r.get("title", ""), wanted_album) >= 0.95]
        possible.sort(key=lambda r: (
            r.get("status") != "Official",
            not bool(r.get("date")),
            r.get("date") or "9999",
        ))
        return possible[0] if possible else None

    def _query(self, title: str, artists: str, album: str, duration_ms=None) -> dict:
        # No string interpolation into a URL: requests does percent encoding.
        def quote_search(value: str) -> str:
            return '"' + re.sub(r'["\\]', " ", value).strip() + '"'

        query = f"recording:{quote_search(title)} AND artist:{quote_search(_first_artist(artists))}"
        search = self._get("recording", {"query": query, "limit": 8})
        found = self._choose_recording(search.get("recordings") or [], title, artists, duration_ms)
        if not found:
            log.debug("MusicBrainz: no confident match for %r — %r", title, artists)
            return {}

        rec = self._get(
            f"recording/{quote(found['id'])}",
            {"inc": "artists+genres+isrcs+releases+work-rels"},
        )
        credit, artist_ids = _artist_credit(rec)
        result = {
            "musicbrainzRecordingId": rec.get("id"),
            "musicbrainzArtistId": artist_ids,
            "genre": _value_genres(rec),
            "isrc": "; ".join(rec.get("isrcs") or []) or None,
            "disambiguation": rec.get("disambiguation"),
        }
        release = self._choose_release(rec.get("releases") or found.get("releases") or [], album)
        if release:
            # Rich release information is authoritative only when it matches
            # the album already identified by Spotify.
            detail = self._get(
                f"release/{quote(release['id'])}",
                {"inc": "release-groups+labels+artist-credits+genres+media"},
            )
            result.update({
                "album": detail.get("title"),
                "releaseDate": detail.get("date"),
                "country": detail.get("country"),
                "barcode": detail.get("barcode"),
                "musicbrainzReleaseId": detail.get("id"),
                "releaseStatus": detail.get("status"),
                "asin": detail.get("asin"),
                "language": (detail.get("text-representation") or {}).get("language"),
                "script": (detail.get("text-representation") or {}).get("script"),
                "packaging": detail.get("packaging"),
                "genre": result.get("genre") or _value_genres(detail),
            })
            result["albumArtist"] = _artist_credit(detail)[0] or None
            result["musicbrainzAlbumArtistId"] = _artist_credit(detail)[1] or None
            if detail.get("label-info"):
                label_info = next((x for x in detail["label-info"] if (x.get("label") or {}).get("name")), None)
                if label_info:
                    result["label"] = label_info["label"]["name"]
                    result["catalogNumber"] = label_info.get("catalog-number")
            group = detail.get("release-group") or {}
            result["musicbrainzReleaseGroupId"] = group.get("id")
            result["releaseType"] = group.get("primary-type")
            if group.get("id"):
                group_detail = self._get(f"release-group/{quote(group['id'])}", {"inc": "genres"})
                result["genre"] = result.get("genre") or _value_genres(group_detail)
                result["originalDate"] = group_detail.get("first-release-date")
                result["secondaryTypes"] = "; ".join(group_detail.get("secondary-types") or [])
            for medium in detail.get("media") or []:
                for track in medium.get("tracks") or []:
                    if (track.get("recording") or {}).get("id") == rec.get("id"):
                        result["discNumber"] = medium.get("position")
                        result["discTotal"] = len(detail.get("media") or [])
                        result["trackNumber"] = track.get("position")
                        result["trackTotal"] = medium.get("track-count")
                        result["media"] = medium.get("format")
                        break
                if result.get("discNumber"):
                    break
        if not result.get("genre") and artist_ids:
            # Genre hierarchy: recording -> release -> release group -> artist.
            primary_artist_id = artist_ids.split(";")[0].strip()
            artist = self._get(f"artist/{quote(primary_artist_id)}", {"inc": "genres"})
            result["genre"] = _value_genres(artist)
        # Composer, lyricist and ISWC live on the composition (work), not the
        # recording. Resolve just one unambiguous performance relationship.
        works = [r.get("work") for r in rec.get("relations") or []
                 if r.get("target-type") == "work" and r.get("type") == "performance"
                 and (r.get("work") or {}).get("id")]
        if len(works) == 1:
            work = self._get(f"work/{quote(works[0]['id'])}", {"inc": "artist-rels"})
            credits = {"composer": [], "lyricist": [], "writer": [], "arranger": []}
            for rel in work.get("relations") or []:
                kind = rel.get("type", "").lower()
                person = (rel.get("artist") or {}).get("name", "")
                if kind in credits and person and person not in credits[kind]:
                    credits[kind].append(person)
            for kind, names in credits.items():
                if names:
                    result[kind] = "; ".join(names)
            iswcs = work.get("iswcs") or []
            result["iswc"] = "; ".join(
                (w.get("id") if isinstance(w, dict) else str(w)) for w in iswcs
            )
        return {k: v for k, v in result.items() if _has(v)}

    def enrich(self, tags: dict, *, duration_ms=None) -> dict:
        """Returns a copy; failures never interrupt downloads or clobber tags."""
        title, artists = _text(tags.get("title")), _text(tags.get("artists"))
        if not title or not artists:
            return dict(tags)
        album = _text(tags.get("album"))
        key = MetadataCache.key(title, artists, album, duration_ms)
        try:
            additions = self.cache.get(key)
            if additions is None:
                additions = self._query(title, artists, album, duration_ms)
                self.cache.put(key, additions)
            return merge_missing(tags, additions)
        except (requests.RequestException, OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
            log.warning("MusicBrainz lookup unavailable for %r: %s", title, exc)
            return dict(tags)


def _sha256(path: str, chunk_size=1024 * 1024) -> str:
    dig = hashlib.sha256()
    with open(path, "rb") as fh:
        for piece in iter(lambda: fh.read(chunk_size), b""):
            dig.update(piece)
    return dig.hexdigest()


def _parse_checksums(text: str) -> dict[str, str]:
    checksums = {}
    for line in text.splitlines():
        hit = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+)", line.strip())
        if hit:
            checksums[os.path.basename(hit.group(2))] = hit.group(1).lower()
    return checksums


class DownloadCancelled(Exception):
    pass


class MusicBrainzDumpDownloader:
    """Downloads official core+derived snapshots with resume and SHA-256 checks.

    These are *PostgreSQL archives*, not immediately usable by SQLite.  They
    remain untouched and are never imported automatically.  The app enriches
    files using the web API and its independent SQLite cache.
    """

    def __init__(self, folder: str, *, session=None):
        self.folder = os.path.abspath(os.path.expanduser(folder))
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def _latest_url(self) -> str:
        response = self.session.get(f"{DUMPS_BASE}/LATEST", timeout=(10, 30))
        response.raise_for_status()
        version = response.text.strip()
        if not re.fullmatch(r"20\d{6}-\d{6}", version):
            raise ValueError(f"MusicBrainz returned invalid snapshot version: {version[:40]!r}")
        return f"{DUMPS_BASE}/{version}"

    def download(self, progress=None, cancel: threading.Event | None = None):
        """Return verified archive paths. A cancelled transfer keeps .part files."""
        cancel = cancel or threading.Event()
        os.makedirs(self.folder, exist_ok=True)
        base = self._latest_url()
        r = self.session.get(f"{base}/SHA256SUMS", timeout=(10, 60))
        r.raise_for_status()
        hashes = _parse_checksums(r.text)
        for name in DUMPS:
            if name not in hashes:
                raise ValueError(f"MusicBrainz did not publish a checksum for {name}")
        results = []
        for index, name in enumerate(DUMPS):
            if cancel.is_set():
                raise DownloadCancelled()
            destination = os.path.join(self.folder, name)
            part = destination + ".part"
            if os.path.isfile(destination) and _sha256(destination) == hashes[name]:
                if progress:
                    progress(name, index, len(DUMPS), os.stat(destination).st_size, os.stat(destination).st_size, "verified")
                results.append(destination)
                continue
            start = os.path.getsize(part) if os.path.isfile(part) else 0
            headers = {"Range": f"bytes={start}-"} if start else {}
            with self.session.get(f"{base}/{name}", headers=headers, stream=True, timeout=(15, 90)) as response:
                # For a Range request the server MUST reply 206. If it replies
                # 200, restart; appending would corrupt the archive.
                response.raise_for_status()
                if response.status_code == 206 and start:
                    cr = response.headers.get("Content-Range", "")
                    if not cr.startswith(f"bytes {start}-"):
                        raise OSError("Server returned an unexpected resumed range")
                    append = True
                else:
                    start = 0
                    append = False
                size = int(response.headers.get("Content-Length") or 0) + start
                downloaded = start
                last_emit = 0.0
                with open(part, "ab" if append else "wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if cancel.is_set():
                            raise DownloadCancelled()
                        if chunk:
                            handle.write(chunk)
                            downloaded += len(chunk)
                            now = time.monotonic()
                            if progress and now - last_emit >= .25:
                                progress(name, index, len(DUMPS), downloaded, size, "downloading")
                                last_emit = now
            if cancel.is_set():
                raise DownloadCancelled()
            if progress:
                progress(name, index, len(DUMPS), downloaded, size, "verifying SHA-256")
            if _sha256(part) != hashes[name]:
                # Wrong bytes can't ever be resumed safely.
                os.remove(part)
                raise OSError(f"Checksum failed for {name}; incomplete archive removed")
            os.replace(part, destination)
            results.append(destination)
            if progress:
                progress(name, index, len(DUMPS), downloaded, size, "verified")
        # A small manifest makes it clear what the current pair is.
        with open(os.path.join(self.folder, "snapshot.json"), "w", encoding="utf-8") as fh:
            json.dump({"source": base, "archives": list(DUMPS), "sha256": {n: hashes[n] for n in DUMPS}}, fh, indent=2)
        return results
