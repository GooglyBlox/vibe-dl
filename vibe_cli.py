#!/usr/bin/env python3
"""Search and download full-length tracks from VIBE (Naver). No login required. Needs ffmpeg."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

WEB_API = "https://apis.naver.com/vibeWeb/vibe-service-web-api"
MUSIC_API = "https://apis.naver.com/vibeWeb/musicapiweb"
STPLAY = "https://apis.naver.com/nmwebplayer/music/stplay_trackStPlay_NO_HMAC"
ID_RE = re.compile(r"/(album|track|artist)/(\d+)", re.IGNORECASE)
BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1F]')
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Referer": "https://vibe.naver.com/",
}
DEVICE_ID = uuid.uuid4().hex


class VibeError(Exception):
    pass


def fetch(url: str, method: str = "GET") -> str:
    with urlopen(Request(url, headers=HEADERS, method=method), timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def api_json(url: str) -> Any:
    # The musicapiweb endpoints default to XML; ask for JSON explicitly.
    headers = {**HEADERS, "Accept": "application/json"}
    with urlopen(Request(url, headers=headers), timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def status(url: str) -> int:
    with urlopen(Request(url, headers=HEADERS, method="HEAD"), timeout=20) as r:
        return r.status


def find(node: Any, key: str) -> Any:
    """First value found under `key`, anywhere in the structure."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            hit = find(value, key)
            if hit is not None:
                return hit
    elif isinstance(node, list):
        for value in node:
            hit = find(value, key)
            if hit is not None:
                return hit
    return None


def result(payload: dict) -> dict:
    return payload.get("response", {}).get("result", payload.get("result", payload)) or {}


def artists(item: dict) -> str:
    names = [a.get("artistName") for a in (item.get("artists") or []) if a.get("artistName")]
    return "; ".join(names) if names else "Unknown Artist"


def sanitize(name: str) -> str:
    return BAD_CHARS.sub("_", name).strip().rstrip(". ") or "untitled"


# --- API ------------------------------------------------------------------- #
def album_meta(album_id: int) -> tuple[dict, list[dict]]:
    album = result(api_json(f"{WEB_API}/album/{album_id}"))
    tracks = find(api_json(f"{WEB_API}/album/{album_id}/tracks"), "tracks") or []
    return album, tracks


def artist_albums(artist_id: int) -> list[dict]:
    data = api_json(f"{MUSIC_API}/v3/musician/artist/{artist_id}/albums?start=1&display=100")
    return find(data, "albums") or []


def track_album_id(track_id: int) -> int:
    match = re.search(r"<albumId>(\d+)</albumId>", fetch(f"{MUSIC_API}/track/{track_id}"))
    if not match:
        raise VibeError(f"could not resolve album for track {track_id}")
    return int(match.group(1))


def search(query: str, kind: str, limit: int) -> list[dict]:
    url = f"{WEB_API}/search/{kind}?query={quote(query)}&start=1&display={limit}"
    return find(api_json(url), kind + "s") or []


def lyrics_lrc(track_id: int) -> str | None:
    try:  # lyrics are optional, never let a hiccup here fail the download
        lyric = result(api_json(f"{MUSIC_API}/vibe/v4/lyric/{track_id}")).get("lyric") or {}
    except (HTTPError, URLError, ValueError):
        return None
    sync = lyric.get("syncLyric") or {}
    times = sync.get("startTimeIndex") or []
    contents = sync.get("contents") or []
    if times and contents:
        default = next((c for c in contents if c.get("languageType") == "default"), contents[0])
        lines = []
        for seconds, line in zip(times, default.get("text") or []):
            minutes, seconds = divmod(float(seconds), 60)
            lines.append(f"[{int(minutes):02d}:{seconds:05.2f}]{line}")
        if lines:
            return "\n".join(lines)
    return (lyric.get("normalLyric") or {}).get("text") or None


# --- streaming -------------------------------------------------------------- #
def manifest_url(track_id: int) -> str:
    params = urlencode({
        "play.trackId": track_id,
        "deviceType": "VIBE_WEB",
        "deviceId": DEVICE_ID,
        "play.mediaSourceType": "AAC_320_ENC",
        "play.aacSupported": "Y",
    })
    body = fetch(f"{STPLAY}?{params}")
    match = re.search(r'"hlsManifestUrl"\s*:\s*"([^"]+)"', body)
    if not match or not match.group(1):
        reason = re.search(r'"playTypeReason"\s*:\s*"([^"]*)"', body)
        raise VibeError(f"no stream for track {track_id} ({reason.group(1) if reason else '?'})")
    return match.group(1)


def full_manifest(preview_url: str, dest: Path) -> int:
    preview = fetch(preview_url)
    key = re.search(r"#EXT-X-KEY:[^\n]+", preview)
    seg = re.search(r"(https://\S+?-)(\d+)(-enc\.ts\S*)", preview)
    if not key or not seg:
        raise VibeError("unexpected manifest format")
    prefix, suffix = seg.group(1), seg.group(3)

    # The preview lists only 60s, but the CDN token covers the whole track;
    # walk segments until one stops returning 200 to find the real end.
    count = 0
    while count < 2000:
        try:
            if status(f"{prefix}{count}{suffix}") != 200:
                break
        except (HTTPError, URLError):
            break
        count += 1
    if not count:
        raise VibeError("no segments accessible")

    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:11",
             "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD", key.group(0)]
    for i in range(count):
        lines += ["#EXTINF:10.0,", f"{prefix}{i}{suffix}"]
    lines.append("#EXT-X-ENDLIST")
    dest.write_text("\n".join(lines), encoding="utf-8")
    return count


# --- download -------------------------------------------------------------- #
def download_cover(url: str, dest: Path) -> Path | None:
    if not url:
        return None
    try:
        dest.write_bytes(urlopen(Request(url, headers=HEADERS), timeout=20).read())
        return dest
    except (HTTPError, URLError, OSError):
        return None


def run_ffmpeg(manifest: Path, out: Path, meta: dict, cover: Path | None) -> None:
    def cmd(with_cover: bool) -> list[str]:
        c = ["ffmpeg", "-y", "-loglevel", "error",
             "-protocol_whitelist", "file,http,https,tcp,tls,crypto,data",
             "-allowed_extensions", "ALL", "-i", str(manifest)]
        if with_cover:
            c += ["-i", str(cover), "-map", "0:a", "-map", "1:v",
                  "-c:v", "mjpeg", "-disposition:v:0", "attached_pic"]
        else:
            c += ["-map", "0:a"]
        c += ["-c:a", "copy"]
        for key, value in meta.items():
            c += ["-metadata", f"{key}={value}"]
        return c + ["-movflags", "+faststart", str(out)]

    proc = subprocess.run(cmd(bool(cover)), capture_output=True, text=True)
    if proc.returncode and cover:
        proc = subprocess.run(cmd(False), capture_output=True, text=True)
    if proc.returncode:
        raise VibeError(f"ffmpeg failed: {proc.stderr.strip()[:300]}")


def album_tags(album: dict) -> dict:
    """Album-level metadata shared by every track on it."""
    tags = {"album": album.get("albumTitle") or "album"}
    album_artist = artists(album)
    if album_artist != "Unknown Artist":
        tags["album_artist"] = album_artist
    if album.get("releaseDate"):
        tags["date"] = album["releaseDate"].replace(".", "-")
    return tags


def download_track(track: dict, out_dir: Path, tags: dict, totals: tuple[int, int],
                   cover: Path | None, want_lyrics: bool) -> bool:
    track_id = int(track["trackId"])
    title = str(track.get("trackTitle") or "Unknown")
    artist = artists(track)
    number = int(track.get("trackNumber") or 0)
    disc = int(track.get("discNumber") or 1)
    total_tracks, total_discs = totals
    try:
        manifest = out_dir / f".{track_id}.m3u8"
        segments = full_manifest(manifest_url(track_id), manifest)
    except VibeError as exc:
        print(f"  ! {title}: {exc}", file=sys.stderr)
        return False

    stem = f"{number:02d} {title} - {artist}" if number else f"{title} - {artist}"
    out = out_dir / f"{sanitize(stem)}.m4a"
    meta = {**tags, "title": title, "artist": artist,
            "track": f"{number}/{total_tracks}" if number else str(number),
            "disc": f"{disc}/{total_discs}"}
    lyrics = lyrics_lrc(track_id) if want_lyrics else None
    if lyrics:
        meta["lyrics"] = lyrics
    try:
        run_ffmpeg(manifest, out, meta, cover)
    finally:
        manifest.unlink(missing_ok=True)
    print(f"  -> {out.name}  ({segments} segments{' +lyrics' if lyrics else ''})")
    return True


def download_album(album_id: int, out_dir: Path, *, only_track: int | None = None,
                   numbers: set[int] | None = None, want_lyrics: bool = True) -> int:
    album, tracks = album_meta(album_id)
    totals = (int(album.get("trackTotalCount") or len(tracks)),
              max((int(t.get("discNumber") or 1) for t in tracks), default=1))
    if only_track is not None:
        tracks = [t for t in tracks if int(t.get("trackId", 0)) == only_track]
    elif numbers:
        tracks = [t for t in tracks if int(t.get("trackNumber", 0)) in numbers]
    tags = album_tags(album)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"{tags['album']} — {len(tracks)} track(s)")
    cover = download_cover(str(album.get("imageUrl") or ""), out_dir / "cover.jpg")
    done = sum(download_track(t, out_dir, tags, totals, cover, want_lyrics) for t in tracks)
    print(f"Done: {done}/{len(tracks)} -> {out_dir}/")
    return done


# --- commands -------------------------------------------------------------- #
def cmd_search(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="vibe_cli.py search", description="Search VIBE.")
    parser.add_argument("query")
    parser.add_argument("--type", choices=["all", "track", "album", "artist"], default="all")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)

    kinds = ["track", "album", "artist"] if args.type == "all" else [args.type]
    for kind in kinds:
        items = search(args.query, kind, args.limit)
        if not items:
            continue
        print(f"\n{kind.upper()}S")
        for it in items:
            if kind == "track":
                print(f"  {it['trackTitle']} — {artists(it)}"
                      f"   https://vibe.naver.com/track/{it['trackId']}")
            elif kind == "album":
                date = it.get("releaseDate", "")
                print(f"  {it['albumTitle']} — {artists(it)} ({date})"
                      f"   https://vibe.naver.com/album/{it['albumId']}")
            else:
                print(f"  {it['artistName']}"
                      f"   https://vibe.naver.com/artist/{it['artistId']}")
    return 0


def cmd_download(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="vibe_cli.py",
                                     description="Download an album, track, or artist from VIBE (Naver).")
    parser.add_argument("target", help="VIBE album/track/artist URL, or a numeric album id.")
    parser.add_argument("--output", default="downloads", help="Output directory.")
    parser.add_argument("--tracks", help="Album only: comma-separated track numbers, e.g. 1,3,8.")
    parser.add_argument("--no-lyrics", action="store_true", help="Don't embed lyrics.")
    args = parser.parse_args(argv)

    match = ID_RE.search(args.target)
    if match:
        kind, ident = match.group(1).lower(), int(match.group(2))
    elif args.target.isdigit():
        kind, ident = "album", int(args.target)
    else:
        print("Provide a VIBE album/track/artist URL or numeric album id.", file=sys.stderr)
        return 1

    want_lyrics = not args.no_lyrics
    out = Path(args.output)
    if kind == "artist":
        albums = artist_albums(ident)
        print(f"{len(albums)} album(s) by artist {ident}\n")
        total = 0
        for album in albums:
            total += download_album(int(album["albumId"]),
                                    out / sanitize(album.get("albumTitle") or "album"),
                                    want_lyrics=want_lyrics)
            print()
        return 0 if total else 1
    if kind == "track":
        done = download_album(track_album_id(ident), out, only_track=ident, want_lyrics=want_lyrics)
        return 0 if done else 1
    numbers = {int(x) for x in args.tracks.replace(" ", "").split(",") if x} if args.tracks else None
    done = download_album(ident, out, numbers=numbers, want_lyrics=want_lyrics)
    return 0 if done else 1


def main() -> int:
    argv = sys.argv[1:]
    try:
        if argv and argv[0] == "search":
            return cmd_search(argv[1:])
        return cmd_download(argv)
    except (HTTPError, URLError) as exc:
        print(f"Network error: {exc}", file=sys.stderr)
        return 1
    except VibeError as exc:
        print(exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
