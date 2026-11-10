# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

One person: the owner of a self-hosted music library, signing in through LDAP behind Authentik. They curate the library themselves, mostly at a desktop; the phone (often the installed app) is for occasional checks.

## Product Purpose

beets-ui is a light web front end for [beets](https://beets.io) that runs beets in-process. It is where music comes into the library, gets tagged and imported, and is kept in order. Success is a library that is complete, correctly tagged and tidy, with as little manual searching, downloading and moving of files as possible.

## Positioning

One place for the whole path a release takes: notice it is missing (discographies from VocaDB, TouhouDB, UtaiteDB, MusicBrainz), get it (search a download source, pick a copy, download it), review and import it with beets, then keep it tidy. Getting is automatic but never silent: anything beets-ui is not sure about waits for the owner. Lidarr-style watching for new releases is deliberately left to Lidarr.

## Operating Context

- Runs on the owner's server next to slskd (the download source, Soulseek) and Jellyfin (playback). Downloads land in the inbox, which is slskd's downloads folder.
- The library is mostly Japanese doujin, Vocaloid, utaite and Touhou releases: names are romanized for Jellyfin, the originals kept alongside.
- A visit is a working session. In order of how often, the owner comes to: get more music (gaps, Get, watch it arrive); look after the library (covers, names, lyrics, upgrades); clear what needs them (inbox review, choosing a copy, failures). Browsing and playing happen mainly in Jellyfin.

## Capabilities and Constraints

- Inbox: folders are looked up and reviewed against candidates; confident matches can import on their own. Incoming lists downloads and requests.
- Find music and Get: a source-neutral layer over download sources (Soulseek through slskd today; others such as streaming rippers may be added). Pages never name the source except where it needs fixing.
- Library, Artwork, Insights (discography gaps, upgrades, listening, lyrics), Console (any non-interactive beet command), Jobs (one queue for library changes, undo for edits).
- Stack: FastAPI, Jinja templates, htmx; plain JS and CSS, no build step. Boosted navigation, live updates over server-sent events, installable as an app.
- Interface language: English.

## Brand Commitments

- Name: beets-ui, lowercase. Mark: a lowercase "b" in a rounded square.
- Voice: plain sentence case; controls say what they do; messages say what happened and what to do next.
- The visual system is the shadcn-based design system already shipped in `src/beets_ui/static/ds/` (see DESIGN.md once recorded): neutral palette, system fonts, light and dark themes.

## Evidence on Hand

No screenshots, users or metrics beyond the owner's own library; nothing to quote or count beyond what the app reads from beets and its state database.

## Product Principles

1. Automatic, never silent: do the routine work, and stop for the owner exactly when it isn't sure.
2. One path for a release: missing, getting, reviewing, imported; each step visible from the next.
3. Source-neutral: where music comes from is a detail behind the interface.
4. Show what needs the owner first, then what is in progress, then everything else.
5. A working tool: fast, precise and calm, for long desktop sessions.
