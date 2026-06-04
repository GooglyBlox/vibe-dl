# vibe-dl

Search and download full-length tracks from [VIBE](https://vibe.naver.com) (NAVER's music service). No account needed.

Requires Python 3.10+ and [ffmpeg](https://ffmpeg.org/) on your PATH.

## Usage

```bash
# search tracks, albums, and artists (each result prints a link you can pass back in)
python vibe_cli.py search "jasper mitchell"
python vibe_cli.py search "iu" --type album --limit 5

# download a whole album, a single track, or an artist's entire discography
python vibe_cli.py "https://vibe.naver.com/album/6766828"
python vibe_cli.py "https://vibe.naver.com/track/52132838"
python vibe_cli.py "https://vibe.naver.com/artist/4958319" --output discography

# just some album tracks
python vibe_cli.py "https://vibe.naver.com/album/6766828" --tracks 1,3,8
```

You can also pass a bare album id. Output is tagged `.m4a` files (up to 320kbps AAC)
with embedded cover art and embedded synced lyrics (where VIBE has them), plus a
`cover.jpg`. Use `--no-lyrics` to skip lyrics and `--output DIR` to choose a folder.

## How it works

VIBE serves a preview HLS manifest to anyone, listing only the first 60 seconds;
but its CDN token actually authorizes every segment of the track. The tool walks
past the preview, rebuilds the full manifest, and lets ffmpeg decrypt and mux the
complete song.

## Disclaimer

Use responsibly. Make sure what you download is permitted in your jurisdiction and
under the rights attached to the content.
