# NostalgiaTV Channel Allocator

Watches Plex and NostalgiaTV, finds shows and movies that have no permanent channel, and places them.
Confident placements happen automatically; anything uncertain is posted to a Discord channel with a
channel picker. Your choice is applied as soon as you press Approve. Titles you delete from Plex are
removed from channel lists automatically (logged only, never posted).

## What it does and does not do

- Only adds titles to channels that already exist. It never creates, hides, renames, renumbers or
  removes channels. The only removal it makes is the clean-up of titles deleted from Plex (below).
- New episodes of shows you already have need nothing: channels hold whole shows.
- A title only on a seasonal channel counts as unplaced.
- Before every change it takes a NostalgiaTV config backup (`/api/config/backups/create`), then
  refreshes only the channels it touched. Restore from Configure > Admin > Automatic Backup if needed.
- YouTube, Classicals, Fan Edits, Reality Shows and Music are ignored (see `ignore_libraries`).

## How a scan works (every 15 minutes by default)

1. Ask NostalgiaTV for unassigned content, and ask Plex for shows and movies added in the last 35 days.
2. Skip anything already handled, and anything that already has a permanent channel. A title counts as
   placed when a visible channel's manual list contains it (read from the exported config). NostalgiaTV's
   `channels-containing` lookup is only trusted for visible channels without a manual list: it also
   reports automatic-criteria matches and hidden channels.
3. Place each remaining title with `rules.yaml`:
   - automatic: placed, then listed in a "Placed automatically" message
   - not certain: posted as "Where should this go?" with up to 3 suggestions pre-selected. Suggestions
     combine the rules with NostalgiaTV's own criteria matches on visible channels (it knows original
     networks, e.g. NBC for The Office, which Plex often does not).
4. In Discord, adjust the selection (up to 3 channels) and press **Approve**, or press
   **Skip for now** to decide later. A skipped title that still has no channel after `REASK_DAYS`
   (default 30) is asked about again, once. Open questions are never re-posted.
5. Clean-up: titles deleted from Plex are removed from every channel's manual list (see below).

## Clean-up of deleted titles

After each scan, every channel's manual list is compared with Plex. A title is removed from all its
channels when it is missing from every Plex library listing **and** Plex answers "not found" for it, on
two consecutive scans (so about 15 minutes after you delete it). Removals go to the container log only.

- Only keys from your Plex server are checked; NostalgiaTV's own local media is never touched.
- If Plex errors during the check, nothing is removed that pass.
- If more than `CLEANUP_MAX_REMOVALS` (default 50) titles would go in one pass, nothing is removed and a
  warning is logged: that usually means a library or drive is unmounted. Raise the limit for a big purge.
- A backup is taken before removing, and only the affected channels are refreshed.
- Set `CLEANUP=false` to turn it off. With `DRY_RUN=true` it only logs what it would remove.

Validated against the line-up built on 2026-11-11: 113 of 171 shows and 561 of 564 movies would be
placed automatically, and 1 automatic show placement (Bump in the Night: Faux Kids instead of ITV) differs
from the hand allocation. Everything else goes to Discord.

## Setup

### 1. Discord bot (about 5 minutes)
1. https://discord.com/developers/applications > New Application > name it "NTV Allocator".
2. Bot tab > Reset Token > copy the token into `DISCORD_TOKEN` in `.env`. No privileged intents are needed.
3. OAuth2 > URL Generator: scope `bot`; permissions `View Channels`, `Send Messages`, `Embed Links`,
   `Read Message History`. Open the generated URL and add the bot to your server.
4. Create the channel for it (for example `#ntv-allocator`). Discord Settings > Advanced > Developer Mode on,
   then right-click the channel > Copy Channel ID into `DISCORD_CHANNEL_ID`.
5. Optional: right-click your own name > Copy User ID into `DISCORD_ALLOWED_USER_IDS` so only you can approve.

### 2. Plex token
Use your existing Plex token (the same one your other apps use) for `PLEX_TOKEN`.

### 3. NostalgiaTV access
- If Configure has no login set (open mode), leave `NTV_API_KEY`, `NTV_USERNAME` and `NTV_PASSWORD` blank.
- If it has a login, set `NTV_USERNAME` and `NTV_PASSWORD` to the Configure login.

### 4. Deploy in Dockge
Every push to `main` builds `ghcr.io/d4rkm3ch/ntv-allocator:latest` with GitHub Actions
(`.github/workflows/image.yml`), so the NAS only pulls an image.

1. The image is public, so no registry login is needed.
2. Create the folders `/mnt/Swimming/Sandboxes/docker/ntv-allocator/config` and `.../data`, owned by your PUID/PGID.
3. In Dockge, create a stack named `ntv-allocator`, paste `compose.yaml`, and put the values from
   `.env.example` in the stack's `.env` box.
4. Deploy. Leave `DRY_RUN=true` for the first run: it posts what it would do without changing anything.
   When the Discord messages look right, set `DRY_RUN=false` and redeploy.
5. To update later: Dockge > Update (pulls the new `latest`).

## Editing the rules

`rules.yaml` is copied to `/mnt/Swimming/Sandboxes/docker/ntv-allocator/config/rules.yaml` on first start;
edit that copy and restart the container. The file is checked on start-up: a rule that names a channel
which is not in the catalogue stops the container with a clear error instead of misplacing titles.

- `title_overrides` pins exact titles, for example `"Some Show (2027)": [app_ef_x]`.
- Mark a show rule `auto: true` to stop being asked about matches.
- Add a channel to the catalogue only if it already exists and is visible in NostalgiaTV.

## Files

- `app/main.py`: scan loop and Discord interaction
- `app/rules.py`: placement rules engine
- `app/clients.py`: Plex and NostalgiaTV API clients
- `app/state.py`: SQLite record of decisions (`/data/state.db`)
- `tests/test_allocator.py`: end-to-end checks against fake Plex, NostalgiaTV and Discord
  (`python tests/test_allocator.py`)

## Notes

- Verified live on 2026-11-11: X-Men placed automatically (Superhero, Action, Lackluster); The Office (US)
  approved through Discord.
- If NostalgiaTV refuses a title for a channel, the reason is logged and posted in Discord. A title refused
  on every channel is not recorded as placed and is asked about again after `REASK_DAYS`.
- One title failing to evaluate (for example a Plex lookup error) is logged and retried next scan; the
  rest of the scan continues. A failed Approve shows an error in Discord and leaves the question open.
- State lives in `/data/state.db`. Deleting it makes the allocator re-check everything; nothing is
  placed twice because titles that already have channels are skipped.
