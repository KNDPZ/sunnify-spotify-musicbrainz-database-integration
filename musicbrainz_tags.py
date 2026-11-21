"""Write MusicBrainz's *verified, available* supplementary metadata into audio tags.

Existing user tags win.  MusicBrainz does not have e.g. lyrics for every
recording; never manufacture unavailable or ambiguous values.
"""

from __future__ import annotations

import base64
import os

from mutagen.id3 import (
    APIC, COMM, ID3, TALB, TCOM, TCON, TCOP, TDRC, TEXT, TIT2, TMED, TPE1,
    TPE2, TPOS, TPUB, TRCK, TSRC, TXXX,
)

# ID3 TXXX is used for extensible MusicBrainz IDs.  Keep spelling common to
# MusicBrainz Picard, so other taggers will recognize these fields.
_EXTRA = {
    "musicbrainzRecordingId": "MusicBrainz Recording Id",
    "musicbrainzReleaseId": "MusicBrainz Album Id",
    "musicbrainzReleaseGroupId": "MusicBrainz Release Group Id",
    "musicbrainzArtistId": "MusicBrainz Artist Id",
    "musicbrainzAlbumArtistId": "MusicBrainz Album Artist Id",
    "originalDate": "ORIGINALDATE",
    "barcode": "BARCODE",
    "catalogNumber": "CATALOGNUMBER",
    "country": "RELEASECOUNTRY",
    "releaseStatus": "RELEASESTATUS",
    "releaseType": "RELEASETYPE",
    "asin": "ASIN",
    "disambiguation": "MUSICBRAINZ_DISAMBIGUATION",
    "iswc": "ISWC",
    "writer": "WRITER",
    "arranger": "ARRANGER",
    "language": "LANGUAGE",
    "script": "SCRIPT",
    "packaging": "PACKAGING",
    "secondaryTypes": "RELEASE_SECONDARY_TYPES",
}
_TEXT_ID3 = {
    "genre": TCON,
    "albumArtist": TPE2,
    "composer": TCOM,
    "lyricist": TEXT,
    "label": TPUB,
    "isrc": TSRC,
    "media": TMED,
    "copyright": TCOP,
}
_FLAC_NAMES = {
    "releaseDate": "DATE",
    "albumArtist": "ALBUMARTIST",
    "discNumber": "DISCNUMBER",
    "discTotal": "DISCTOTAL",
    "trackNumber": "TRACKNUMBER",
    "trackTotal": "TRACKTOTAL",
    "genre": "GENRE",
    "composer": "COMPOSER",
    "lyricist": "LYRICIST",
    "label": "LABEL",
    "isrc": "ISRC",
    "media": "MEDIA",
    "copyright": "COPYRIGHT",
}


def _as_text(value) -> str:
    return str(value).strip() if value is not None else ""


def _id3_text(tags: ID3, cls, value) -> None:
    value = _as_text(value)
    if value and not any(getattr(frame, "text", None) for frame in tags.getall(cls.__name__)):
        tags.add(cls(encoding=1, text=[value]))


def _id3_custom(tags: ID3, description: str, value):
    value = _as_text(value)
    if value and not any(
        frame.desc.casefold() == description.casefold() and frame.text
        for frame in tags.getall("TXXX")
    ):
        tags.add(TXXX(encoding=1, desc=description, text=[value]))


def _pair(number, total) -> str:
    a, b = _as_text(number), _as_text(total)
    return f"{a}/{b}" if a and b else a


def _id3_number_and_total(tags: ID3, cls, number, total) -> None:
    """Preserve an existing track/disc number while adding a missing total."""
    frames = tags.getall(cls.__name__)
    current = str(frames[0].text[0]).strip() if frames and frames[0].text else ""
    if current:
        total_text = _as_text(total)
        if total_text and "/" not in current:
            frames[0].text = [f"{current}/{total_text}"]
        return
    _id3_text(tags, cls, _pair(number, total))


def fill_id3(id3: ID3, data: dict, *, include_basic: bool = False):
    if include_basic:
        for field, frame in (
            ("title", TIT2), ("artists", TPE1), ("album", TALB),
            ("releaseDate", TDRC),
        ):
            _id3_text(id3, frame, data.get(field))
    for field, cls in _TEXT_ID3.items():
        _id3_text(id3, cls, data.get(field))
    _id3_number_and_total(id3, TRCK, data.get("trackNumber"), data.get("trackTotal"))
    _id3_number_and_total(id3, TPOS, data.get("discNumber"), data.get("discTotal"))
    for key, desc in _EXTRA.items():
        _id3_custom(id3, desc, data.get(key))


def _picture(cover_bytes):
    if not cover_bytes:
        return None
    from mutagen.flac import Picture

    pic = Picture()
    pic.type = 3
    pic.mime = "image/png" if cover_bytes.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg"
    pic.desc = "Cover"
    pic.data = cover_bytes
    return pic


def fill_vorbis(audio, data: dict):
    for key, name in (
        ("title", "TITLE"), ("artists", "ARTIST"), ("album", "ALBUM"),
        *_FLAC_NAMES.items(),
    ):
        value = _as_text(data.get(key))
        if value and not audio.get(name):
            audio[name] = [value]
    for key, name in _EXTRA.items():
        value = _as_text(data.get(key))
        if value and not audio.get(name):
            audio[name] = [value]


def write_extra(filename: str, data: dict) -> None:
    """Adds extended tags to files already saved by Sunnify's regular writer."""
    ext = os.path.splitext(filename)[1].casefold()
    if ext == ".mp3":
        id3 = ID3(filename)
        fill_id3(id3, data)
        id3.update_to_v23()
        id3.save(v2_version=3)
    elif ext == ".m4a":
        from mutagen.mp4 import MP4

        audio = MP4(filename)
        values = {
            "genre": "\xa9gen", "albumArtist": "aART", "composer": "\xa9wrt",
            "copyright": "cprt",
        }
        for field, atom in values.items():
            value = _as_text(data.get(field))
            if value and not audio.get(atom):
                audio[atom] = [value]
        for field, desc in _EXTRA.items():
            value = _as_text(data.get(field))
            atom = "----:com.apple.iTunes:" + desc
            if value and not audio.get(atom):
                audio[atom] = [value.encode("utf-8")]
        for field, desc in (
            ("label", "LABEL"), ("isrc", "ISRC"), ("lyricist", "LYRICIST"),
            ("media", "MEDIA"),
        ):
            value = _as_text(data.get(field))
            atom = "----:com.apple.iTunes:" + desc
            if value and not audio.get(atom):
                audio[atom] = [value.encode("utf-8")]
        for number, total, atom in (
            ("trackNumber", "trackTotal", "trkn"),
            ("discNumber", "discTotal", "disk"),
        ):
            if not audio.get(atom) and data.get(number):
                audio[atom] = [(int(data[number]), int(data.get(total) or 0))]
            elif audio.get(atom) and data.get(total):
                existing = audio[atom][0]
                if existing[1] == 0:
                    audio[atom] = [(existing[0], int(data[total]))]
        audio.save()
    elif ext == ".flac":
        from mutagen.flac import FLAC

        audio = FLAC(filename)
        fill_vorbis(audio, data)
        audio.save()


def write_ogg_opus(filename: str, data: dict, cover_bytes: bytes | None):
    from mutagen.oggopus import OggOpus

    audio = OggOpus(filename)
    fill_vorbis(audio, data)
    if cover_bytes and not audio.get("metadata_block_picture"):
        pic = _picture(cover_bytes)
        audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
    audio.save()


def write_wave(filename: str, data: dict, cover_bytes: bytes | None):
    from mutagen.wave import WAVE

    audio = WAVE(filename)
    if audio.tags is None:
        audio.add_tags()
    fill_id3(audio.tags, data, include_basic=True)
    if cover_bytes and not audio.tags.getall("APIC"):
        pic = _picture(cover_bytes)
        audio.tags.add(APIC(encoding=1, mime=pic.mime, type=3, desc="Cover", data=cover_bytes))
    audio.save(v2_version=3)
