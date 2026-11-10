# beets-ui

A small, self-hosted web frontend for [beets](https://beets.io). It runs in one container and is written for low-power home servers: one Python process, no Redis, no worker containers, about 100 MB of RAM.

- **Inbox review.** Each folder you drop into the inbox is looked up against MusicBrainz and any other metadata sources you have enabled. You then review the candidates track by track and import them. You can also:
  - pick another candidate,
  - search by artist/album or by ID,
  - import as-is or as individual tracks,
  - skip a folder,
  - resolve duplicates (skip, keep both, replace, merge or upgrade),
  - play the files in the browser before importing,
  - let confident matches import on their own (`BEETSUI_AUTO_IMPORT`).
- **Library.** Browse and search with beets query syntax, view albums, artists, tracks and singletons, edit any field (and write the tags to the files), move files, and remove albums or tracks. Edits can be undone from their job page.
- **Romanized names.** Album, artist and track names in romaji (what Jellyfin shows), from VocaDB, UtaiteDB, TouhouDB or MusicBrainz, with the original names kept in extra tags and shown next to them (`BEETSUI_ROMANIZE`).
- **Artists.** Merge one artist's aliases ("ヌジャベス", "Nujabes") into one album artist; later imports use the kept name too. Collaborations can keep only their primary artist.
- **Artwork.**
  - Album covers: find, choose, upload or fill in automatically, with an optional embed into the files.
  - Artist and circle pictures, saved as `folder.jpg` in each artist folder, which is where Jellyfin and most players look for them.
  - Sources: Cover Art Archive, fanart.tv, Wikimedia Commons (through the artist's MusicBrainz ID), TheAudioDB, Deezer, iTunes, Discogs, **VocaDB**, **UtaiteDB** and **TouhouDB**.
  - **Fill automatically** works through every artist on its own. A picture found through the MusicBrainz ID is used straight away. A name search is used when exactly one artist has that spelling. Shared names (e.g. "since") are left for you under *needs review*.
  - Albums whose art is only embedded in the files show as *embedded only*, and the fill job can extract that art to a cover file.
- **Insights.**
  - *Discography gaps:* releases by your artists and circles that the library doesn't have, from VocaDB, TouhouDB, UtaiteDB or MusicBrainz, with a search for each.
  - *Upgrades:* lossy, low-bitrate, incomplete and mixed-format albums.
  - *Listening:* never played, not played in a year and most played, from Jellyfin's play counts.
  - *Synced lyrics:* from LRCLIB, with romaji under Japanese lines.
- **Find music.** Search for albums and download them into the inbox, which shows them while they come in and then for review. Download whole folders or choose the files, retry failed ones, and get a pop-up when one finishes. Downloads come from a *download source*; today that is Soulseek, through your existing [slskd](https://github.com/slskd/slskd), and pages don't depend on which source it is.
- **Console.** Run any non-interactive `beet` command (`update`, `move`, `fetchart`, `replaygain`, `convert`, `mbsync`, plugin commands and so on) and follow its output live. Anything beets can do is reachable from here.
- **Jobs.** Every change to the library goes through a single queue, so nothing fights over the database, and each job keeps its log. Lookups that only read the library (discographies, the Jellyfin sync) run in a second queue, so they never hold up an import.
- **Login.** A single password, or LDAP (lldap, OpenLDAP, Authentik and others).

The UI uses the beets-ui design system: shadcn's neutral token model with a sidebar layout, light and dark themes that follow the browser (or `BEETSUI_THEME`), and system fonts, so pages load nothing from third parties. Colour only marks a state (matched, needs review, running, failed) and always comes with a word.

---

## Install (Docker Compose)

Docker Compose is the only supported way to run beets-ui. The image, `ghcr.io/zcv28/beets-ui`, is built for `linux/amd64` and includes beets 2.x, ffmpeg, chromaprint (`fpcalc`) and the VocaDB/UtaiteDB/TouhouDB plugins.

### 1. Create `docker-compose.yml`

```yaml
services:
  beets-ui:
    image: ghcr.io/zcv28/beets-ui:latest
    container_name: beets-ui
    restart: unless-stopped
    user: "1000:1000"                 # owner of your music and slskd downloads
    ports:
      - "8337:8337"
    environment:
      TZ: Europe/Rome
      BEETSUI_PASSWORD: change-me      # required unless LDAP is enabled
      # SLSKD_URL: http://slskd:5030
      # SLSKD_API_KEY: your-slskd-api-key
    volumes:
      - ./config:/config               # beets config + library.db, beets-ui state
      - /srv/music:/music              # sorted library (beets "directory")
      - /srv/slskd/downloads:/inbox    # review inbox (slskd's downloads folder)
    deploy:
      resources:
        limits:
          memory: 512m
```

```sh
docker compose up -d
```

Open `http://your-server:8337` and log in with `BEETSUI_PASSWORD`.

### 2. Volumes

| Container path | What it is |
|---|---|
| `/config/beets/` | beets' `config.yaml`, `library.db` and import state. On first start a default `config.yaml` is written here if none exists. |
| `/config/beets-ui/` | beets-ui's own state (`state.db`, the session secret, thumbnails, extra pip packages). |
| `/music` | Your sorted library. It must match `directory:` in the beets config. |
| `/inbox` | The review inbox. Every **sub-folder** is one import (usually one album). Loose files directly in `/inbox` are ignored. |

The container user (`user:`) must be able to read the inbox and write to `/music` and `/config`.

> beets stores absolute paths in `library.db`. If you reuse an existing library, mount the music folder at the **same path it had before**, or update `directory:` and run `beet move` afterwards.

### 3. Environment variables

| Variable | Default | Description |
|---|---|---|
| `BEETSUI_PASSWORD` | — | Login password. **Required** when LDAP is off. |
| `BEETSUI_SECRET_KEY` | generated | Cookie signing key. Generated once and stored in `/config/beets-ui/secret_key`. |
| `BEETSUI_SECURE_COOKIES` | `false` | Set to `true` when beets-ui is only reachable over HTTPS. |
| `BEETSUI_INBOX` | `/inbox` | Review folder. |
| `BEETSUI_AUTO_PREVIEW` | `true` | Look up new inbox folders automatically. |
| `BEETSUI_SCAN_INTERVAL` | `30` | Seconds between inbox scans. |
| `BEETSUI_DEBOUNCE` | `30` | A folder must be unchanged this many seconds before it is looked up. This avoids looking up half-finished downloads. |
| `BEETSUI_DELETE_AFTER_IMPORT` | `false` | Delete the inbox folder after a successful import (beets copies by default). |
| `BEETSUI_AUTO_IMPORT` | `0` (off) | Import an automatically looked-up folder without review when every album in it matches at least this % (e.g. `95`), has no missing tracks or unmatched files, and nothing in it is already in the library. Duplicates beets finds still wait for you. |
| `BEETSUI_ROMANIZE` | `false` | Import VocaDB/UtaiteDB/TouhouDB/MusicBrainz matches with romanized names and keep the originals alongside. See *Romanized names* below. |
| `BEETSUI_THEME` | `auto` | Color theme: `auto` (follows the browser's light or dark setting), `light` or `dark`. The old theme names (`graphite`, `slate`, `mahogany`, `phosphor`) still work and mean `dark`. |
| `BEETSUI_LYRICS_ROMAJI` | `below` | Romaji in synced lyrics of Japanese songs: `below` (a romaji line under each line, same timestamp), `inline` (`line / romaji` on one line, the way beets stores translations), `only` (romaji instead of the Japanese) or `off`. |
| `JELLYFIN_URL` / `JELLYFIN_API_KEY` | — | Turns on *Insights → Listening*, e.g. `http://jellyfin:8096` and a key from Jellyfin's Dashboard → API Keys. |
| `JELLYFIN_USER` | first admin | Whose play counts to read, when Jellyfin has several users. |
| `BEETSUI_PRIMARY_ARTIST` | `false` | Keep only the first credited artist as album artist on import ("ヌジャベス & Joe Claussell" → "ヌジャベス"). Track artists keep the full credit. Albums already in the library can be fixed under *Library → Artists*. |
| `BEETSUI_LDAP_ENABLED` | `false` | Use LDAP instead of the password. |
| `LDAP_URL` | — | e.g. `ldap://lldap:3890` or `ldaps://ldap.example.org`. |
| `LDAP_BIND_DN` / `LDAP_BIND_PASSWORD` | — | Service account used to look users up. Leave empty for an anonymous search. |
| `LDAP_BASE_DN` | — | Where users live, e.g. `ou=people,dc=example,dc=org`. |
| `LDAP_USER_FILTER` | `(uid={username})` | Search filter; `{username}` is escaped. Put group restrictions here. |
| `LDAP_START_TLS` | `false` | Upgrade a plain `ldap://` connection with StartTLS. |
| `SLSKD_URL` / `SLSKD_API_KEY` | — | Turns on Find music and downloads, with Soulseek as the source. |
| `BEETSUI_COVER_SOURCES` | `coverartarchive,fanarttv,theaudiodb,deezer,itunes,discogs,vocadb,touhoudb,utaitedb` | Album cover sources, tried in order. |
| `BEETSUI_ARTIST_SOURCES` | `fanarttv,wikidata,theaudiodb,deezer,discogs,vocadb,touhoudb,utaitedb` | Artist/circle picture sources, tried in order. |
| `BEETSUI_ARTIST_IMAGE_NAME` | `folder.jpg` | File name(s) for artist pictures; a comma-separated list writes several (e.g. `folder.jpg,artist.jpg`). |
| `BEETSUI_ARTWORK_OVERWRITE` | `false` | Default for "replace existing pictures" when filling artist images. |
| `BEETSUI_MIN_COVER_PX` | `500` | Covers smaller than this count as low-resolution. |
| `FANARTTV_API_KEY` | — | Enables fanart.tv ([get a key](https://fanart.tv/get-an-api-key/)). |
| `THEAUDIODB_API_KEY` | `2` | TheAudioDB key (the free test key works, slowly). |
| `DISCOGS_TOKEN` | — | Enables Discogs images (personal access token). |
| `EXTRA_PIP_PACKAGES` | — | Extra Python packages for beets plugins, installed into `/config/beets-ui/pip` on first start and reused after that. |
| `TZ` | `UTC` | Time zone for timestamps. |

### 4. Configure beets

beets-ui reads `/config/beets/config.yaml` exactly as the `beet` command does. Copy/move, tag writing, path formats, plugins and their options all come from there. Restart the container after changing it.

- **beets 2.x needs `musicbrainz` in the plugin list.** If you list plugins and leave it out, lookups return no MusicBrainz candidates.
- ReplayGain: add `replaygain` to `plugins:` and set `replaygain: {backend: ffmpeg}`. ffmpeg is bundled; the GStreamer backend is not.
- The default `config.yaml` written on first start enables MusicBrainz, the doujin sources (`vocadb`, `touhoudb`, `utaitedb`; see [beets-vocadb](https://github.com/prTopi/beets-vocadb) for their options), AcoustID fingerprints (`chroma`), cover fetching and `mbsync`. It sets no `import.languages`, so beets keeps each artist's primary name and `BEETSUI_ROMANIZE` can romanize it with the original kept alongside.
- `fetchart: sources:` must be a YAML list (`[filesystem, coverart, …]`): beets 2.14 rejects a space-separated string and fetchart then fails to load.
- Artist pictures need each album artist to have their own folder, e.g. `paths: default: $albumartist/$album%aunique{}/$track $title` (the default). With a flat layout such as `$albumartist - $album/…` there is no artist folder to put the picture in, and the Artwork page says so.

### Migrating from beets-flask

beets-flask keeps beets' files in `config/beets/`, and so does beets-ui:

1. Stop beets-flask.
2. Mount the same folder: `- /path/to/beets-flask/config/beets:/config/beets`, or copy `config.yaml` and `library.db` into `./config/beets/`.
3. Mount your music at the same container path beets-flask used (`/music/clean` or whatever your `directory:` says), and your inbox at `/inbox`.
4. Keep `musicbrainz` in `plugins:` (see above).

The `gui:` section of a beets-flask config is ignored.

> beets-ui ships a recent beets. The first time it opens an older `library.db` it migrates the database, and writes `library.db-before-*.bak` backups next to it. Don't run an older beets (or beets-flask) against the same database afterwards.

### Download source: Soulseek (slskd)

1. In slskd, create an API key (`web.authentication.api_keys` in `slskd.yml`, or the `SLSKD_API_KEY` env var) with read/write access.
2. Set `SLSKD_URL` (as seen from the beets-ui container, e.g. `http://slskd:5030` on the same Docker network) and `SLSKD_API_KEY`.
3. Mount slskd's **downloads** directory as `/inbox`. slskd keeps partial files in its separate *incomplete* folder, so only finished files show up. The debounce waits until a folder has stopped changing before looking it up.

Search from **Inbox → Find music**. Results are grouped into one row per folder and where it comes from, with free upload slots and lossless folders first.

- **Download** takes the whole folder. beets-ui asks for the folder's full listing first, because a search only returns the files that matched it: covers, cue sheets and logs come along too. If there's no answer, only the matched files are downloaded, and the message says so.
- **Open a folder** to see every file in it, untick what you don't want, and download the rest.
- **Options** under the search box set how long to wait for answers and skip uploaders with a long queue or a slow connection. They stay set for later searches. A running search can be stopped.
- **Downloading**, at the top of the inbox, shows progress, speed and your place in the uploader's queue. Failed files can be retried, and failed rows removed. A finished download becomes an ordinary inbox folder.
- When a download finishes or fails, a pop-up says so on whatever page you're on. The **Inbox** link in the sidebar gets a dot when the source can't be reached or isn't connected, and the inbox offers **Reconnect**.

**Find music** on an inbox folder fills in the search from its tags, and Insights links to searches for missing and upgradeable albums. Old `/soulseek` links redirect to the new places.

beets-ui checks slskd every 5 seconds in the background. Uploads, shares, rooms and chat stay in slskd's own web UI.

### LDAP examples

lldap, users in the `music` group only:

```yaml
BEETSUI_LDAP_ENABLED: "true"
LDAP_URL: ldap://lldap:3890
LDAP_BIND_DN: uid=admin,ou=people,dc=example,dc=org
LDAP_BIND_PASSWORD: admin-password
LDAP_BASE_DN: ou=people,dc=example,dc=org
LDAP_USER_FILTER: (&(uid={username})(memberOf=cn=music,ou=groups,dc=example,dc=org))
```

OpenLDAP: `LDAP_USER_FILTER: (&(objectClass=inetOrgPerson)(uid={username}))`.
Authentik LDAP outpost: `LDAP_BASE_DN: ou=users,dc=ldap,dc=goauthentik,dc=io` and `LDAP_USER_FILTER: (cn={username})`.

### Updating

```sh
docker compose pull && docker compose up -d
```

Tags: `latest` (main branch), `X.Y.Z` / `X.Y` (releases), `sha-<commit>`.

---

## Using it

beets-ui behaves like an app. Moving between pages doesn't reload them, so a preview keeps playing while you look around. Lists such as the inbox and jobs update by themselves the moment something changes. Quick actions like deleting an inbox folder or dismissing a missing album happen in place, with a pop-up to **Undo** where that is possible. You can install it from the browser (the install icon in the address bar, or *Add to Home Screen* on a phone) to give it its own window and icon.

1. **Inbox.** New folders show up as *Not looked up*, get looked up, and become *Ready* (with the best match %) or *Needs attention*. Expand a row to see its files. Folders that are already in your library (same MusicBrainz album ID, or same album artist and album name) are marked with a link to the album; delete them one by one or all at once with **Delete imported**. Press **Review N folders** to start on the first folder waiting for you, or open any folder:
   - Every candidate shows match %, source, year, label, catalog number and what differs. The chosen one shows a track-by-track diff, with changed titles and length mismatches marked.
   - Choose a candidate (click its row), *as-is* or *skip* for each album in the folder; the chosen candidate's track changes show below it. Then press **Import and next**: the import runs as a job and the next folder to review opens (**Import** stays on the folder). The bar stays pinned at the bottom with a count of what will be imported, and the header shows how many folders are left. Press ▶ next to a file to listen to it in the Preview window.
   - Keyboard: <kbd>j</kbd>/<kbd>k</kbd> move between candidates, <kbd>x</kbd> chooses one, <kbd>o</kbd> shows the current files, <kbd>p</kbd> plays the first track, <kbd>i</kbd> imports and moves on, <kbd>n</kbd> / <kbd>Shift</kbd>+<kbd>N</kbd> open the next or previous folder.
   - Nothing good? Use **Search again** with artist/album, or paste MusicBrainz/Discogs IDs or URLs, and tick *keep current candidates* to add the results to the list. You can also switch the folder to *albums grouped by tags* or *individual tracks*.
   - If beets finds the album already in your library, the folder becomes *Duplicate*. Pick what to do and press Import again.
2. **Library.** Search with [beets queries](https://beets.readthedocs.io/en/stable/reference/query.html) (`albumartist:Foo year:2000..2010`, `format:FLAC`, …). On an album page you can edit fields, write tags, move files, remove the album, fetch its cover or synced lyrics, or run the other one-click commands under **More actions** (refresh from MusicBrainz, embed the cover, ReplayGain, plain lyrics; *re-tag* opens the console first).
   - **Undo.** Album and track edits, artist merges and primary-artist fixes save the old values first. Open the job (a pop-up links to it when it finishes) and press **Undo** to put the old values, tags and folders back.
   - **Artists.** Filter by name or letter. Tick two or more artists to merge them, or open *merge aliases* for suggestions: names that differ only in width or case, share an artist ID from the same source, or where one is the other's sort or credited name (MusicBrainz gives "ヌジャベス" the sort name "Nujabes"). Merged names are remembered and applied to future imports; remove one under *Remembered* to stop that.
3. **Artwork.** *Album covers* lists albums with missing (or low-resolution) covers. *Artist pictures* lists artists without a picture. Run **Fill automatically** first, then open *needs review* for the few artists it was unsure about (matched by name, several artists with the same name, or not found) and pick or upload those by hand.
4. **Romanized names** (*Library → Artists → romanize…*). The main album, artist and track tags get romanized names, which is what Jellyfin and other players show. The original names go into `album_original`, `albumartist_original`, `title_original` and `artist_original`: written to the files as custom tags (`ALBUM_ORIGINAL`, …), shown under the romanized names, and found by library searches.
   - **VocaDB, UtaiteDB, TouhouDB:** the site's Romaji names ("東方紅魔郷" → "Touhou Koumakyou"). Names nobody has romanized on the site stay as they are.
   - **MusicBrainz:** the release's Latin-script pseudo-release (*transliterated tracklisting*), when someone has added one. Many doujin releases have none.
   - **Artists**, on albums from any source (also as-is imports): MusicBrainz keeps many artists in their own script ("ヨルシカ") and lists the romanized name as an alias, so artists still in their own script get the artist's Latin name, English alias or sort name ("Yorushika", "Parannoul"). The artist is found by its MusicBrainz ID, or by an exact name search that matches exactly one artist; results are cached.
   - Anything still unromanized can be renamed by hand: tick the artist on the *Artists* page and type the new name. The old name is remembered for future imports and kept as the original.
   - With `BEETSUI_ROMANIZE=true` this happens on import; **Romanize library** does it for albums you already have (it can be undone from its job page). Folder names follow your path format, so they change too; Jellyfin treats moved files as new (play counts don't follow).
   - `vdbsync`, `tdbsync` and `mbsync` write the source's own names back, so run *Romanize library* afterwards (the album page's **mbsync** button does that by itself).
5. **Insights.**
   - **Discography gaps.** Press **Look up all artists** once; it runs in the background next to imports (MusicBrainz allows one request a second, so a big library takes a while). Artists are looked up where their albums were tagged from: VocaDB/TouhouDB/UtaiteDB list the artist's own releases (`OnlyMainAlbums`), MusicBrainz its release groups. An album counts as owned by ID, or by title (romanized or original) among that artist's albums. Choose which kinds to show (albums and EPs by default; singles, compilations, other). **Find** searches the romanized names, **Find (original)** the names as written. **Dismiss** hides a release you don't want.
   - **Upgrades.** Lossy (MP3, AAC, Vorbis, Opus, WMA), low bitrate (under 192 kbps, 96 for Opus), incomplete (fewer tracks than the track/disc totals) and mixed formats. Dismiss an album for a reason when it's the best copy there is. Low-resolution covers are under *Artwork*.
   - **Listening.** Play counts, last-played dates and favourites of one Jellyfin user, read every 6 hours when you open the page (or **Sync now**). Tracks are matched by their path inside the music folder, so Jellyfin and beets can mount it in different places. Jellyfin keeps all-time totals only, and a moved file starts again at zero. **Random unplayed album** opens something you haven't listened to yet.
   - **Lyrics.** Synced lyrics from [LRCLIB](https://lrclib.net), looked up by the original names first (LRCLIB lists Japanese songs under their Japanese titles). Japanese songs get romaji: VocaDB's own romanized lyrics line by line where they match, otherwise generated with [cutlet](https://github.com/polm/cutlet). The lyrics are written into the files with beets' `lyrics_backend`/`lyrics_url` fields; Jellyfin reads them on its next scan (for tracks it already had lyrics for, use *Refresh metadata → Replace all metadata* once). Use the **Synced lyrics** button on an album, **Fetch** on a track, or *Insights → Lyrics* for the whole library (300 tracks per run). Lyrics runs can be undone from their job page.
6. **Console.** Type a command without the leading `beet`. Commands that prompt can't be answered, so use `-y`/`-q` (e.g. `import -q -L <query>` to re-tag library albums).

Not supported:
- **VGMdb.** It has no official API, its unofficial mirror is offline and the site blocks automated requests.
- **Undoing an import or a removal.** Remove the album from the library instead; removals can't be undone.

---

## Development

```sh
uv venv && uv pip install -e ".[dev]"
BEETSDIR=./dev/beets BEETSUI_DATA=./dev/ui BEETSUI_INBOX=./dev/inbox BEETSUI_PASSWORD=dev .venv/bin/beets-ui
.venv/bin/pytest
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests
```

How it fits together:
- FastAPI renders Jinja2 templates; [htmx](https://htmx.org) polls for live updates.
- beets runs in the same process.
- **Inbox imports run in two passes.** A *capture* session records every task's candidates and imports nothing. Later, an *apply* session re-runs the folder with your decisions, restricting the lookup to the chosen IDs. No beets session is ever kept waiting for input.
- All writes run one at a time on a single job thread.

CI (`.github/workflows/ci.yml`) lints, runs the tests, builds the image, smoke-tests it and pushes it to GHCR on `main` and on `v*` tags.

## License

MIT. See [LICENSE](LICENSE). Includes htmx (BSD-2-Clause), component CSS from [shadcn-html](https://github.com/codylindley/shadcn-html) (MIT) and icons from [Lucide](https://lucide.dev) (ISC); see `src/beets_ui/static/ds/LICENSES.md`.
