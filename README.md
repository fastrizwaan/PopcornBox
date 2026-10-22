# PopcornBox 2.2

A Linux media client for discovering and playing movies, series, anime, and other media through Stremio-compatible addons. PopcornBox combines catalog browsing, search, a local library, and an integrated MPV-based player in a GTK4 and Libadwaita desktop app.

## Features

### Discover and search

- Browse Home and Discover feeds built from installed addon catalogs.
- Browse movies, series, anime, live TV, and other catalog types when supported by your addons.
- Filter Discover by media type, addon catalog, section, genre, year, language, and country.
- Use **Unique** to combine matching catalog results while removing duplicates. Discover opens with Unique enabled by default.
- Search while filtering. Search checks titles across addon catalogs, supports title-only matching, and loads additional pages as you scroll.
- Use the separate search view to search available catalogs and browse results by media type.

### Library and addons

- Keep Favorites, History, Watched, and Continue Watching lists, with progress saved for playback.
- Search and filter local library lists by media type.
- Browse Community, Official, and Stremio-Addons.net directories to find compatible addons; install by manifest URL and import or export addon configurations.
- Optionally sync supported library, playback progress, and addon data with a connected Stremio or Nuvio account.
- Manage downloads and resume saved playback sessions.

### Playback

- Play streams from addons, torrent-backed streams, local video files, folders, URLs, and playlists.
- Use the integrated MPV-based player with hardware decoding when available.
- Select audio and subtitle tracks, load external tracks, search for subtitles, and navigate chapters.
- Adjust playback and picture options such as volume, speed, brightness, contrast, zoom, and aspect ratio.
- Resume playback and optionally play the next episode automatically.
- Integrate with desktop media controls through MPRIS.

## Install or build

PopcornBox is packaged as a Flatpak. To build and install the current source locally, use a Linux system with Flatpak, Flatpak Builder, and the GNOME 50 runtime/SDK available:

```bash
git clone https://github.com/fastrizwaan/PopcornBox.git
cd PopcornBox
./build_bundle.sh
```

Then launch it with:

```bash
flatpak run io.github.fastrizwaan.PopcornBox
```

The build script uses the project Flatpak manifest and installs the app for your user. To build directly instead:

```bash
flatpak-builder --user --install --force-clean --disable-rofiles-fuse \
  build-dir build-aux/flatpak/io.github.fastrizwaan.PopcornBox.json
```

## Project details

- **Version:** 2.2
- **Platform:** GNU/Linux (Flatpak)
- **UI:** Python, GTK4, Libadwaita
- **Playback:** MPV with libtorrent-backed streaming support
- **License:** GPL-3.0-or-later

Addon catalogs and streams are provided by the addons you choose to install. Availability and metadata can vary between providers.

## Links

- [Source code](https://github.com/fastrizwaan/PopcornBox)
- [Report an issue](https://github.com/fastrizwaan/PopcornBox/issues)
