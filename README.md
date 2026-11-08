# beets-ui

A small, self-hosted web frontend for [beets](https://beets.io). It runs in one container and is written for low-power home servers: one Python process, no Redis, no worker containers, about 100 MB of RAM.

- **Inbox review.** Each folder you drop into the inbox is looked up against MusicBrainz and any other metadata sources you have enabled. You then review the candidates track by track and import them. You can also:
  - pick another candidate,
  - search by artist/album or by ID,
  - import as-is or as individual tracks,
  - skip a folder,
  - resolve duplicates (skip, keep both, replace, merge or upgrade).
- **Library.** Browse and search with beets query syntax, view albums, artists, tracks and singletons, edit any field (and write the tags to the files), move files, and remove albums or tracks.
- **Artwork.**
  - Album covers: find, choose, upload or fill in automatically, with an optional embed into the files.
  - Artist and circle pictures, saved as `folder.jpg` in each artist folder, which is where Jellyfin and most players look for them.
  - Sources: Cover Art Archive, fanart.tv, TheAudioDB, Deezer, iTunes, Discogs, **VocaDB**, **UtaiteDB** and **TouhouDB**.
- **Soulseek.** Search and download through your existing [slskd](https://github.com/slskd/slskd), one folder at a time. Finished downloads land in the inbox for review.
- **Console.** Run any non-interactive `beet` command (`update`, `move`, `fetchart`, `replaygain`, `convert`, `mbsync`, plugin commands and so on) and follow its output live. Anything beets can do is reachable from here.
- **Jobs.** Every change to the library goes through a single queue, so nothing fights over the database, and each job keeps its log.
- **Login.** A single password, or LDAP (lldap, OpenLDAP, Authentik and others).

The UI uses a dark variant of [min.css](https://mincss.com).

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
| `BEETSUI_LDAP_ENABLED` | `false` | Use LDAP instead of the password. |
| `LDAP_URL` | — | e.g. `ldap://lldap:3890` or `ldaps://ldap.example.org`. |
| `LDAP_BIND_DN` / `LDAP_BIND_PASSWORD` | — | Service account used to look users up. Leave empty for an anonymous search. |
| `LDAP_BASE_DN` | — | Where users live, e.g. `ou=people,dc=example,dc=org`. |
| `LDAP_USER_FILTER` | `(uid={username})` | Search filter; `{username}` is escaped. Put group restrictions here. |
| `LDAP_START_TLS` | `false` | Upgrade a plain `ldap://` connection with StartTLS. |
| `SLSKD_URL` / `SLSKD_API_KEY` | — | Turns on the Soulseek page. |
| `BEETSUI_COVER_SOURCES` | `coverartarchive,fanarttv,theaudiodb,deezer,itunes,discogs,vocadb,touhoudb,utaitedb` | Album cover sources, tried in order. |
| `BEETSUI_ARTIST_SOURCES` | `fanarttv,theaudiodb,deezer,discogs,vocadb,touhoudb,utaitedb` | Artist/circle picture sources, tried in order. |
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
- Doujin sources: add `vocadb`, `touhoudb` and/or `utaitedb` to `plugins:` and their candidates appear on the review page. See [beets-vocadb](https://github.com/prTopi/beets-vocadb) for their options.
- Artist pictures need each album artist to have their own folder, e.g. `paths: default: $albumartist/$album%aunique{}/$track $title` (the default). With a flat layout such as `$albumartist - $album/…` there is no artist folder to put the picture in, and the Artwork page says so.

### Migrating from beets-flask

beets-flask keeps beets' files in `config/beets/`, and so does beets-ui:

1. Stop beets-flask.
2. Mount the same folder: `- /path/to/beets-flask/config/beets:/config/beets`, or copy `config.yaml` and `library.db` into `./config/beets/`.
3. Mount your music at the same container path beets-flask used (`/music/clean` or whatever your `directory:` says), and your inbox at `/inbox`.
4. Keep `musicbrainz` in `plugins:` (see above).

The `gui:` section of a beets-flask config is ignored.

> beets-ui ships a recent beets. The first time it opens an older `library.db` it migrates the database, and writes `library.db-before-*.bak` backups next to it. Don't run an older beets (or beets-flask) against the same database afterwards.

### Soulseek (slskd)

1. In slskd, create an API key (`web.authentication.api_keys` in `slskd.yml`, or the `SLSKD_API_KEY` env var) with read/write access.
2. Set `SLSKD_URL` (as seen from the beets-ui container, e.g. `http://slskd:5030` on the same Docker network) and `SLSKD_API_KEY`.
3. Mount slskd's **downloads** directory as `/inbox`. slskd keeps partial files in its separate *incomplete* folder, so only finished files show up. The debounce waits until a folder has stopped changing before looking it up.

Use **Soulseek → Search**, open a result and press **Download**. The whole remote folder is queued. When it finishes it appears in the Inbox. **Search Soulseek for this album** on a review page pre-fills the search from the folder's tags.

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

1. **Inbox.** New folders show up as *New*, get looked up, and become *Ready* (with the best match %) or *Needs attention*. Open one:
   - Every candidate shows match %, source, year, label, catalog number and what differs. Expand it to see a track-by-track diff (changed titles in yellow, length mismatches in red).
   - Choose a candidate, *as-is* or *skip* for each album in the folder, then press **Import**.
   - Nothing good? Use **Search again** with artist/album, or paste MusicBrainz/Discogs IDs or URLs, and tick *keep current candidates* to add the results to the list. You can also switch the folder to *albums grouped by tags* or *individual tracks*.
   - If beets finds the album already in your library, the folder becomes *Duplicate*. Pick what to do and press Import again.
2. **Library.** Search with [beets queries](https://beets.readthedocs.io/en/stable/reference/query.html) (`albumartist:Foo year:2000..2010`, `format:FLAC`, …). On an album page you can edit fields, write tags, move files, remove the album, or jump to the console with `mbsync`, `fetchart`, `replaygain` and similar commands filled in.
3. **Artwork.** *Album covers* lists albums with missing (or low-resolution) covers. *Artist pictures* lists artists without a picture. Pick an image per item or let **Fill automatically** work through all of them as a job.
4. **Console.** Type a command without the leading `beet`. Commands that prompt can't be answered, so use `-y`/`-q` (e.g. `import -q -L <query>` to re-tag library albums).

Not supported:
- **VGMdb.** It has no official API, its unofficial mirror is offline and the site blocks automated requests.
- **Undoing an import.** Remove the album from the library instead.

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

MIT. See [LICENSE](LICENSE). Includes min.css © Owen Versteeg (MIT) and htmx (BSD-2-Clause).
