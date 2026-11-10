# TODO

## Maybe later

- **Browse a user's shares** from Soulseek (`GET /users/{user}/browse` in slskd). Big shares
  send listings of tens of MB, so it needs caching and a folder tree that expands lazily.
  Searching and downloading folders already work without it.

## Out of scope for now

- **New release alerts.** Get → Discover lists ListenBrainz's fresh releases by
  artists you listen to when you open it. Notifications, or getting them on their
  own, wait until that list proves useful. ListenBrainz only knows releases in
  MusicBrainz; VocaDB-only circles still show up only under Get → Missing.
