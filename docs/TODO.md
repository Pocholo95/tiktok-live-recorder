# Pending work

Things agreed on but not built yet. Newest first.

## Upload safety (converted MP4 replaces the raw)

Today `PUT /recordings/{id}/converted` only checks that the file is a
browser-playable MP4. Uploading the MP4 of a *different* live is accepted,
swapped in, and the raw `.ts` is deleted - unrecoverable.

- [ ] **Server: compare durations before swapping.** ffprobe the raw and
  the uploaded file; reject (raw untouched) if they differ by more than
  `max(10 s, 2 %)`. Conversion doesn't change duration, so this catches a
  wrong file regardless of its name.
- [ ] **Browser: warn on a name mismatch.** The generated command names the
  output `<raw stem>.mp4`; if the dropped file is named differently, ask
  for confirmation before uploading (a warning, not a block).

## Guest names: possible incomplete source

A recording listed two co-hosts alternating (`@favoritamariiaofi`,
`@favoritarespaldo`) while both were probably on screen. Hypothesis:
`link_mic.rival_anchor_id` only names the current battle opponent when 3+
creators are linked. Next step: get a `<recording>.participants.jsonl`
from such a live and check whether `battle_scores` or the TikTokLive
websocket (`LinkMicBattle.anchorsInfo`, connects anonymously) list
everyone. Risks of TikTokLive were discussed: no account risk without a
session, some IP-rate-limit risk, depends on Euler Stream signing.
