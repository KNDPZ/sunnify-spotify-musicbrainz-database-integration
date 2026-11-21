# Windows fork: MusicBrainz metadata and database download

This change is scoped to the **Windows** GUI/Windows executable. Sunnify's
original download sources, file naming, credentials, and UI remain unchanged.

## Settings

1. Open **Settings** (gear icon).
2. **MusicBrainz enrichment:** enabled by default on Windows. Requires **Add Meta
   Tags** on the download screen (also checked by default on Windows).
3. **MusicBrainz database folder:** pick a writable location with enough space.
   This is **not** the folder used for downloaded songs.
4. **MusicBrainz database → Download official database archives…:** optional;
   downloads `mbdump.tar.bz2` (core) and `mbdump-derived.tar.bz2` (derived tags)
   from the latest official dump. The two files total approximately **8 GiB**
   compressed. Allow extra disk space for a large SQLite cache. The app verifies
   SHA-256 checksums supplied by MusicBrainz, supports HTTP range resume when
   supported by the server, and uses `.part` files while downloading. Cancel
   pauses, and restarting the download attempts to resume.
5. Save Settings. New songs will receive supplemental tags when an accurate
   MusicBrainz match exists. The full archive download is **not required**.

**Important limitation:** MusicBrainz's published dump files are PostgreSQL
backups, not an indexed library for this program to query directly. The dump
option **only downloads and verifies the originals**, and **does not** unpack
or import them. They can be imported into a MusicBrainz PostgreSQL server
separately if desired. The executable's actual metadata lookups use
`musicbrainz.org/ws/2` with a meaningful User-Agent and strict rate limiting,
then store results in `<chosen database folder>/musicbrainz-cache.sqlite3`.
Saved matches are reusable when offline. New uncached songs require internet
access; downloading the PostgreSQL dumps does not make them available offline.

This design avoids silently performing a multi-hour full PostgreSQL import and
prevents users from mistaking raw archives for an offline searchable cache.

## Added tags

Only nonempty verified values are added. Supported fields vary by format:
- MusicBrainz curated genre votes (positive ranked genres, up to 3); fallback
  recording → album/release → release group → primary artist.
- Album artist, label, catalogue number, release country, release status,
  primary/secondary release type, original release date, barcode, ASIN,
  disc number and total, track number and total, medium format, ISRC.
- MusicBrainz recording/release/release-group/artist/album-artist identifiers.
- Composition-derived composer, lyricist, writer, arranger and ISWC where an
  unambiguous work relationship is provided by MusicBrainz.

No fabricated genre, lyrics, copyright owner, or metadata is written when not
available. A recording must match the track title, performer, and (if supplied)
duration conservatively, and release-level information requires a confident
album match. Existing Spotify metadata is never intentionally replaced.

Formats: MP3 (ID3v2.3), M4A (iTunes atoms and freeform fields), FLAC and Opus
(Vorbis comments), WAV (ID3 RIFF chunk). Changing existing files is limited
to the app's normal per-track tagging path; **the MusicBrainz database button
never scans or changes your existing music folders**.

## Building the Windows executable

On **Windows** with Python 3.10 or newer (and FFmpeg installed / supplied as
in the original project):

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r req.txt
pip install pyinstaller
pyinstaller Sunnify.spec
```

Look for `dist/Sunnify.exe`. The build spec bundles both metadata modules and
supported tag formats. The ZIP here contains **source code**, not a Windows
binary; this workspace cannot execute/test the Windows GUI directly.

## Data and licensing

The core dump is generally CC0. The derived dump (which contains user tags,
including genre associations) is under CC BY-NC-SA 3.0; observe MusicBrainz's
published data terms if using it. Consult the official resources:
- [MusicBrainz download documentation](https://musicbrainz.org/doc/MusicBrainz_Database/Download)
- [MetaBrainz dataset overview](https://metabrainz.org/datasets/postgres-dumps)
- [MusicBrainz Web Service](https://musicbrainz.org/doc/MusicBrainz_API)
- [API rate limiting](https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting)
