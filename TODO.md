# TODO

## Maybe later

- **Browse a user's shares** from Soulseek (`GET /users/{user}/browse` in slskd). Big shares
  send listings of tens of MB, so it needs caching and a folder tree that expands lazily.
  Searching and downloading folders already work without it.

## Out of scope for now

- **New release alerts.** Watching artists and announcing new releases is what
  [Lidarr](https://lidarr.audio) already does, and it can hand downloads to slskd
  through Soularr. Revisit only if Get → Missing (VocaDB, TouhouDB,
  UtaiteDB, MusicBrainz) should turn into a scheduled check with notifications.
