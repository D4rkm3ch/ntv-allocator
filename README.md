# NostalgiaTV Channel Allocator

Watches Plex and NostalgiaTV, finds shows and movies that have no permanent channel, and places them.
Confident placements happen automatically; anything uncertain is posted to a Discord channel with a
channel picker. Your choice is applied as soon as you press Approve.

## What it does and does not do

- Only adds titles to channels that already exist. It never creates, hides, renames, renumbers or
  removes channels, and never removes a title from a channel.
- New episodes of shows you already have need nothing: channels hold whole shows.
- A title only on New Arrivals or a seasonal channel counts as unplaced.
- Before every change it takes a NostalgiaTV config backup (`/api/config/backups/create`), then
  refreshes only the channels it touched. Restore from Configure > Admin > Automatic Backup if needed.
- YouTube, Classicals, Fan Edits, Reality Shows and Music are ignored (see `ignore_libraries`).

## How a scan works (every 15 minutes by default)

1. Ask NostalgiaTV for unassigned content, and ask Plex for shows and movies added in the last 35 days.
2. Skip anything already handled, and anything that already has a permanent channel.
3. Place each remaining title with `rules.yaml`:
   - automatic: placed, then listed in a "Placed automatically" message
   - not certain: posted as "Where should this go?" with suggestions pre-selected
4. In Discord, adjust the selection (up to 3 channels) and press **Approve**, or press
   **Leave on New Arrivals** to decide later. A title still unassigned when it leaves New Arrivals
   is asked about again.

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

1. One-time registry login on the NAS, because the repository and its image are private. Create a GitHub
   token (Settings > Developer settings > Personal access tokens > Tokens (classic)) with only `read:packages`,
   then on the NAS shell: `docker login ghcr.io -u D4rkm3ch` and paste the token as the password.
   (Alternatively, set the package to public under the repository's Packages; the image contains no secrets.)
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

## Notes

- Tested against simulated Plex and NostalgiaTV servers and the real library inventory, not yet against
  your live server; that is what the dry run is for.
- If NostalgiaTV refuses a title for a channel, the reason is written to the container log.
- State lives in `/data/state.db`. Deleting it makes the allocator re-check everything; nothing is
  placed twice because titles that already have channels are skipped.
