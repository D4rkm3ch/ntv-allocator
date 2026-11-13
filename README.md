<p align="center"><img src="assets/logo-512.png" width="160" alt="NTV Allocator logo"></p>

# NostalgiaTV Channel Allocator

Watches Plex and NostalgiaTV, finds shows and movies that have no permanent channel, and places them.
Every new show or movie is posted to a Discord channel with a short summary and suggested channels
pre-selected; your choice is applied as soon as you press Approve. New episodes of a show that already
has a channel need nothing. Titles you delete from Plex are
offered for removal from their channels, one Discord question per show or movie.

## What it does and does not do

- Only adds titles to channels that already exist. It never creates, hides, renames, renumbers or
  removes channels. The only removal it makes is a title you deleted from Plex, after you press Remove (below).
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
   **Skip for now** to decide later. A skipped title that still has no channel after the re-ask period (Settings, default 30 days)
   (default 30) is asked about again, once. Open questions are never re-posted.
5. Clean-up: titles deleted from Plex are offered for removal in Discord (see below).

## Status page: login, settings, recent placements and the library

- **Login.** On first visit, create the admin password (at least 8 characters, entered twice) with the
  one-time setup code printed in the container log; only someone who can read the NAS's logs can claim
  the instance. After that, viewing and editing need a login (30 days per browser, HttpOnly cookie).
  Five wrong passwords lock login for 15 minutes. `/api/status` stays open for dashboards and carries
  health information only.
- **Forgot the password?** In the TrueNAS shell run
  `sudo docker exec ntv-allocator python -m app.reset_password`. It clears the password, signs everyone out
  and prints a new setup code; placements and settings are kept.
- **Settings** (status page): dry run, clean-up, scan interval, look-back, re-ask period, clean-up safety
  limit and alert timings. They are stored in `/data/state.db` and apply immediately. On the first start
  after upgrading, the old environment values (`DRY_RUN`, `SCAN_INTERVAL_MINUTES`, ...) are copied in once;
  after that they are ignored and can be removed from the compose file.

- **Recent placements** (status page): everything placed in the last 14 days, how it was placed, and an
  Edit button.
- **Library** (`/library`): every show and movie in the managed libraries, shows listed once (no seasons
  or episodes). Search, filter by library or channel, or show only titles with no channel, and edit any.
- Adds use NostalgiaTV's add-to-channel and clear an old exclusion; removals rewrite
  the channel's manual list so no exclusion is left behind (custom channels use remove-from-channel).
- Taking every channel off a title sends it back to Discord as a question on the next scan.

## Clean-up of deleted titles

After each scan, every channel's manual list is compared with Plex. When a title is missing from every
Plex library listing **and** Plex answers "not found" for it on two consecutive scans (about 15 minutes
after you delete it), one Discord message is posted for it:

> **Deleted from Plex: X-Men (2000)**
> Still on: Superhero, Action, Lackluster
> Remove it from these channels?  [Remove] [Keep]

- One question per show or per movie. Channels hold whole shows, so a deleted show is one question no
  matter how many episodes it had, and deleting some episodes of a show that is still in Plex needs
  nothing. Deleting three movies gives three questions.
- **Remove** takes it off every listed channel (after a config backup) and refreshes only those channels.
  **Keep** leaves it and never asks about that title again.
- If the title comes back to Plex before you answer, the question is closed automatically.
- Only keys from your Plex server are checked; NostalgiaTV's own local media is never touched.
- If Plex errors during the check, nothing is asked that pass.
- If more than the clean-up safety limit (Settings, default 50) titles go missing at once, no questions are posted and
  a single "Clean-up paused" message explains why: that usually means a library or drive is unmounted.
  Raise the limit and restart for a deliberate large purge.
- Turn it off under Settings. In dry run it only logs what it would ask.

## Status page

Open `http://<nas>:19860` (`STATUS_PORT`) for a read-only page that refreshes every 30 seconds: whether
Plex, NostalgiaTV, Discord and the scans are working (with the reason when not), last and next scan,
open questions, the last 50 actions, and the main settings. `/api/status` returns the same as JSON for
Homepage or Home Assistant. It shows no tokens or passwords; keep the port on your LAN.

## Connection alerts

Before every scan the allocator checks that Plex and NostalgiaTV answer an authenticated request.

- A service failing 3 checks in a row (Settings) posts one alert, for example
  "**Plex problem**: is not reachable at http://...". While anything is down it checks every minute
  and does not scan.
- It posts "**Plex is working again** (was down for 12 min)" on recovery, and a reminder every 6 hours
  (Settings) while a problem lasts.
- Scan or clean-up errors with both servers up are retried quietly once; two in a row post an
  "Allocator scans problem" alert, with details in the container log.
- A rejected token or login is reported as such (HTTP 401/403).
- Alerts raised while Discord is unreachable are posted once it is back. If the container itself stops,
  nothing can post: watch the container with your usual monitoring for that.

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
- Profile: with one profile (the usual case) it is found automatically and used for every call. With
  several, set `NTV_PROFILE_ID`; the start-up log lists the IDs if it cannot decide.
- If Configure has no login set (open mode), leave `NTV_API_KEY`, `NTV_USERNAME` and `NTV_PASSWORD` blank.
- If it has a login, set `NTV_USERNAME` and `NTV_PASSWORD` to the Configure login.

### 4. Deploy in Dockge
Every push to `main` builds `ghcr.io/d4rkm3ch/ntv-allocator:latest` with GitHub Actions
(`.github/workflows/image.yml`), so the NAS only pulls an image.

1. The image is public, so no registry login is needed.
2. Create the folders `/mnt/Swimming/Sandboxes/docker/ntv-allocator/config` and `.../data`, owned by your PUID/PGID.
3. In Dockge, create a stack named `ntv-allocator`, paste `compose.yaml`, and put the values from
   `.env.example` in the stack's `.env` box.
4. Deploy, then open the status page (`http://<nas>:19860`). Create the admin password with the setup code
   printed in the container log. A new install starts in dry run: questions are posted in Discord but
   nothing changes in NostalgiaTV. When the Discord messages look right, turn dry run off under Settings.
5. To update later: Dockge > Update (pulls the new `latest`).

## Editing the rules

`rules.yaml` is copied to `/mnt/Swimming/Sandboxes/docker/ntv-allocator/config/rules.yaml` on first start;
edit that copy and restart the container. The file is checked on start-up: a rule that names a channel
which is not in the catalogue stops the container with a clear error instead of misplacing titles.

- `title_overrides` pins exact titles, for example `"Some Show (2027)": [app_ef_x]`.
- Mark a show rule `auto: true` to stop being asked about matches.
- Add a channel to the catalogue only if it already exists and is visible in NostalgiaTV.

## Files

- `assets/`: the logo (colour SVG and PNGs) and single-colour variants in blue, grey, off-white and black, matching NostalgiaTV's alternate logos. Use
  `assets/logo-512.png` as the Discord bot's avatar.
- `app/main.py`: scan loop and Discord interaction
- `app/rules.py`: placement rules engine
- `app/clients.py`: Plex and NostalgiaTV API clients
- `app/state.py`: SQLite record of decisions (`/data/state.db`)
- `tests/test_allocator.py`: end-to-end checks against fake Plex, NostalgiaTV and Discord
  (`python tests/test_allocator.py`)

## Notes

- Fixed in v1.3: earlier versions sent no profile ID, so NostalgiaTV put placements on a legacy internal
  profile instead of yours. On first start, v1.3 forgets those records and re-checks recent titles,
  placing them on your real profile.

- Verified live on 2026-11-11: X-Men placed automatically (Superhero, Action, Lackluster); The Office (US)
  approved through Discord.
- If NostalgiaTV refuses a title for a channel, the reason is logged and posted in Discord. A title refused
  on every channel is not recorded as placed and is asked about again after the re-ask period.
- One title failing to evaluate (for example a Plex lookup error) is logged and retried next scan; the
  rest of the scan continues. A failed Approve shows an error in Discord and leaves the question open.
- State lives in `/data/state.db`. Deleting it makes the allocator re-check everything; nothing is
  placed twice because titles that already have channels are skipped.
