"""Regression tests for the Windows MusicBrainz-only fork feature."""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
from unittest.mock import patch

import pytest
import requests

from musicbrainz_metadata import (
    DownloadCancelled,
    MetadataCache,
    MusicBrainzClient,
    MusicBrainzDumpDownloader,
    _parse_checksums,
    merge_missing,
)
from musicbrainz_tags import write_extra, write_ogg_opus, write_wave


REC_ID = "11111111-1111-1111-1111-111111111111"
REL_ID = "22222222-2222-2222-2222-222222222222"
GROUP_ID = "33333333-3333-3333-3333-333333333333"
ARTIST_ID = "44444444-4444-4444-4444-444444444444"


def artist_credit():
    return [{"name": "The Artist", "artist": {"id": ARTIST_ID, "name": "The Artist"}}]


def recording_genres():
    return [{"name": "rock", "count": 4}, {"name": "electronic", "count": 2},
            {"name": "unpopular", "count": -1}, {"name": "old", "count": 0}]


class FakeClient(MusicBrainzClient):
    def __init__(self, folder):
        super().__init__(str(folder))
        self.calls = []

    def _get(self, endpoint, params=None):
        self.calls.append((endpoint, params))
        if endpoint == "recording":
            return {"recordings": [
                {"id": "wrong", "title": "Great Song", "artist-credit": artist_credit(),
                 "score": 99, "length": 400000},
                {"id": REC_ID, "title": "Great Song", "artist-credit": artist_credit(),
                 "score": 94, "length": 180000},
            ]}
        if endpoint == f"recording/{REC_ID}":
            return {"id": REC_ID, "artist-credit": artist_credit(),
                    "genres": recording_genres(), "isrcs": ["USABC1234567"],
                    "releases": [{"id": REL_ID, "title": "Great Album", "status": "Official",
                                  "date": "2022-01-01"}]}
        if endpoint == f"release-group/{GROUP_ID}":
            return {"id": GROUP_ID, "genres": [{"name": "ambient", "count": 4}],
                    "first-release-date": "2020-05-01"}
        if endpoint == f"release/{REL_ID}":
            return {"id": REL_ID, "title": "Great Album", "date": "2022-01-01",
                    "country": "US", "barcode": "12345", "status": "Official",
                    "release-group": {"id": GROUP_ID, "primary-type": "Album"},
                    "artist-credit": artist_credit(),
                    "label-info": [{"label": {"name": "Sample Label"},
                                    "catalog-number": "ABC001"}],
                    "media": [{"position": 1, "format": "CD", "track-count": 12,
                               "tracks": [{"position": 4, "recording": {"id": REC_ID}}]}]}
        raise AssertionError("Unexpected call: " + endpoint)


def test_merge_preserves_spotify_values():
    existing = {"title": "Spotify Name", "genre": "", "album": "Spotify Album", "cover": "cover"}
    merged = merge_missing(existing, {"title": "Wrong name", "genre": "rock", "album": "Wrong",
                                      "cover": "different"})
    assert merged == {**existing, "genre": "rock"}
    assert existing["genre"] == ""


def test_enrichment_caches_fields_and_never_overwrites(tmp_path):
    client = FakeClient(tmp_path)
    tags = {"title": "Great Song", "artists": "The Artist", "album": "Great Album",
            "releaseDate": "2022", "cover": "spotify-cover"}
    one = client.enrich(tags, duration_ms=180000)
    assert one["genre"] == "rock; electronic"
    assert one["originalDate"] == "2020-05-01"
    assert one["musicbrainzRecordingId"] == REC_ID
    assert one["musicbrainzReleaseGroupId"] == GROUP_ID
    assert one["label"] == "Sample Label"
    assert one["country"] == "US"
    assert one["trackNumber"] == 4
    assert one["trackTotal"] == 12
    assert one["isrc"] == "USABC1234567"
    assert one["releaseDate"] == "2022"  # Spotify wins
    assert one["cover"] == "spotify-cover"
    assert len(client.calls) == 4
    two = client.enrich(tags, duration_ms=180000)
    assert one == two
    assert len(client.calls) == 4
    assert (tmp_path / "musicbrainz-cache.sqlite3").is_file()


def test_reject_weak_mismatch(tmp_path):
    client = FakeClient(tmp_path)
    tags = {"title": "Different Song", "artists": "The Artist", "album": "Great Album"}
    assert client.enrich(tags) == tags
    assert len(client.calls) == 1
    assert client.enrich(tags) == tags
    assert len(client.calls) == 1


def test_cache_key_distinguishes_release(tmp_path):
    c = MetadataCache(str(tmp_path))
    a = c.key("Same", "Artist", "Album A")
    b = c.key("Same", "Artist", "Album B")
    assert a != b
    assert c.get(a) is None
    c.put(a, {"genre": "pop"})
    assert c.get(a) == {"genre": "pop"}
    assert c.get(b) is None


class MockReply:
    def __init__(self, data: bytes, status=200, headers=None):
        self.content = data
        self.text = data.decode("utf-8", errors="replace")
        self.status_code = status
        self.headers = headers or {"Content-Length": str(len(data))}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def iter_content(self, chunk_size=1024):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class MockSession:
    def __init__(self, pieces):
        self.pieces = pieces
        self.calls = []
        self.headers = {}

    def get(self, url, headers=None, **kwargs):
        self.calls.append((url, headers))
        if url.endswith("/LATEST"):
            return MockReply(b"20261118-002316\n")
        if url.endswith("/SHA256SUMS"):
            return MockReply("\n".join(
                f"{hashlib.sha256(data).hexdigest()}  {name}" for name, data in self.pieces.items()
            ).encode())
        name = url.rsplit("/", 1)[-1]
        body = self.pieces[name]
        if headers and headers.get("Range"):
            offset = int(headers["Range"].split("=")[1].split("-")[0])
            return MockReply(body[offset:], 206, {
                "Content-Length": str(len(body) - offset),
                "Content-Range": f"bytes {offset}-{len(body) - 1}/{len(body)}",
            })
        return MockReply(body)


def test_dump_download_verifies_and_resumes(tmp_path):
    import musicbrainz_metadata as mb

    files = {"mbdump.tar.bz2": b"HEAD" * 500,
             "mbdump-derived.tar.bz2": b"DATA" * 250}
    first = files["mbdump.tar.bz2"]
    (tmp_path / "mbdump.tar.bz2.part").write_bytes(first[:400])
    session = MockSession(files)
    d = MusicBrainzDumpDownloader(str(tmp_path), session=session)
    with patch.object(mb, "DUMPS", tuple(files)):
        result = d.download()
    assert len(result) == 2
    assert (tmp_path / "mbdump.tar.bz2").read_bytes() == first
    assert not (tmp_path / "mbdump.tar.bz2.part").exists()
    assert session.calls[2][1] == {"Range": "bytes=400-"}
    assert (tmp_path / "snapshot.json").exists()


def test_dump_download_cancel_keeps_partial(tmp_path):
    import threading
    import musicbrainz_metadata as mb

    files = {"mbdump.tar.bz2": b"X" * 1024,
             "mbdump-derived.tar.bz2": b"Y" * 1024}
    cancel = threading.Event()
    downloader = MusicBrainzDumpDownloader(str(tmp_path), session=MockSession(files))
    def on_progress(name, idx, total, received, expected, state):
        if state == "downloading":
            cancel.set()
    with patch.object(mb, "DUMPS", tuple(files)):
        with pytest.raises(DownloadCancelled):
            downloader.download(progress=on_progress, cancel=cancel)
    assert (tmp_path / "mbdump.tar.bz2.part").exists()


@pytest.mark.parametrize("ext", ["mp3", "m4a", "flac", "opus", "wav"])
def test_tag_writers_embed_extended_fields(tmp_path, ext):
    from mutagen import File
    from mutagen.easyid3 import EasyID3

    path = str(tmp_path / ("example." + ext))
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=0.1", "-y", path,
    ], check=True)
    data = {"title": "Test", "artists": "Artist", "album": "Album",
            "genre": "jazz", "trackNumber": 2, "trackTotal": 10,
            "albumArtist": "Artist", "musicbrainzRecordingId": REC_ID}
    if ext == "mp3":
        audio = EasyID3(path)
        audio["title"] = "Existing Title"
        audio.save()
        write_extra(path, data)
        assert EasyID3(path)["title"] == ["Existing Title"]
    elif ext == "opus":
        write_ogg_opus(path, data, None)
    elif ext == "wav":
        write_wave(path, data, None)
    else:
        write_extra(path, data)
    result = File(path)
    if ext == "mp3":
        assert result.tags["TCON"].text == ["jazz"]
        assert result.tags["TRCK"].text == ["2/10"]
    elif ext == "wav":
        assert result.tags["TCON"].text == ["jazz"]
    elif ext == "m4a":
        assert result.tags["\xa9gen"] == ["jazz"]
        assert result.tags["trkn"] == [(2, 10)]
    else:
        assert result.tags["genre"] == ["jazz"]


def test_mp3_existing_track_number_only_adds_missing_total(tmp_path):
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import ID3

    path = str(tmp_path / "example.mp3")
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=0.1", "-y", path,
    ], check=True)
    existing = EasyID3(path)
    existing["tracknumber"] = "7"
    existing["title"] = "My title"
    existing.save(v2_version=3)
    write_extra(path, {"trackNumber": 2, "trackTotal": 10, "genre": "electronic"})
    tags = ID3(path)
    assert tags["TRCK"].text == ["7/10"]
    assert tags["TIT2"].text == ["My title"]
    assert tags["TCON"].text == ["electronic"]
