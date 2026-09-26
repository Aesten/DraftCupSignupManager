# Draft Cup Signup Manager: bot specification

A Discord bot that runs signups for a Bannerlord Draft Cup, lets organisers pick captains and assign them to divisions, and exports:

- CSV files with the full signup data, for manual checks;
- a player list, for late signups;
- the final `.draftcup.json` tournament file for **DraftCupAuctionApp**.

The output file format is defined in [`draftcup-bot-spec.md`](draftcup-bot-spec.md). This document covers everything upstream of it.

## 1. Scope and principles

- **One tournament per Discord server** at a time. The bot can be in several servers; each has its own config and data.
- **Discord only**, no web interface. Users interact through buttons, modals and ephemeral replies. Admins also use slash commands and a dedicated admin channel.
- **Two channels:**
  - **Signup channel:** one public signup post with buttons. Every reply to a user is **ephemeral**.
  - **Admin channel:** persistent messages: a live status board, captain review cards, notifications, exports and an audit trail.
- **The bot doesn't handle tiers or budgets after export.** It computes both from the signup data. Any manual adjustment is made in the auction app after import.
- **Stack:** Python 3.11+, discord.py ≥ 2.7 (radio groups in modals), SQLite through `aiosqlite`. It is self-hosted on the organiser's home server.

## 2. Glossary

| Term | Meaning |
|---|---|
| **Signup** | One Discord user's registration, with the role *player* or *captain*. A user has at most one active signup. |
| **Captain candidate** | A signup with the role *captain*. Its **captain status** is `pending`, `picked` (with a division) or `pool` (not picked, so it plays as a player). |
| **Pool** | Every player signup plus every captain candidate with the status `pool`. |
| **Division** | A group of captains that is auctioned separately. Only admins see divisions; users never do. |
| **Revision** | A counter that goes up on every change to tournament data. The export tracker uses it. |

## 3. Tournament configuration

Admins set these values with `/config` (§7). Defaults are shown.

| Setting | Default | Notes |
|---|---|---|
| `title` | `"Draft Cup"` | Max 80 chars. Used in the JSON and the export file names. |
| `format` | `captainPick` | `captainPick` or `randomPick`. Chosen when opening signups (`/signups open format:`), and admins can still change it later. The derived tier (§4.1) is written in every export whatever the format, so it's available if the auctioneer switches format in the app. |
| `captains_per_division` | `8` | Target number of teams per division. |
| `team_size` | `6` | Players per team, **not counting** the captain. Range 5–10 (app limit). |
| `division_count` | `2` | Number of divisions. Divisions are named `Division 1…N` by default and can be renamed. |
| `half_budget_cap` | `true` | Written as `halfBudgetCapAtStart` on every division. |
| `timezone` | `UTC` | IANA name, e.g. `Europe/Paris`. Used to read the dates admins enter. |
| `tournament_date` | none | Match day (usually a Sunday). Shown in the player attendance text. |
| `auction_date` | none | Auction day (usually a Saturday). Shown in the captain attendance text. |
| `closes_at` | none | When signups close automatically. Required to open or reopen signups; admins can move it while signups are open. |
| `rules_url` | none | A link shown in the agreement step. |
| `admin_roles` | none | Discord roles with admin rights. |
| `admin_users` | none | Individual users with admin rights. |
| `signup_channel` | none | Set by `/setup`. |
| `admin_channel` | none | Set by `/setup`. |

**Admins:** a user is an admin if they have one of `admin_roles`, are listed in `admin_users`, or have Discord's *Manage Server* permission. *Manage Server* works as a bootstrap so the first setup is always possible.

## 4. Signup data

| Field | Input | Validation / normalisation |
|---|---|---|
| **Role** | Chosen by the button clicked (§5.1) | `player` or `captain`. |
| **Nickname** | Text input | Trimmed; runs of spaces collapsed to one. Allowed characters: `A–Z a–z 0–9`, space, `_`, `-`. Length 2–24. Must be **unique case-insensitively** across all active signups, players and captains alike. It is used as the name in the auction app. |
| **Steam profile** | Text input | Format check only. It must match `https://steamcommunity.com/id/<custom>` or `https://steamcommunity.com/profiles/<17-digit id starting 7656>`. `http`, `www.` and a trailing `/` are accepted, and the link is stored as `https://steamcommunity.com/.../`. The bot doesn't make any network call. |
| **Class** | Radio group | Exactly one of `inf`, `arc`, `cav`. |
| **Highest division played** | Text input, optional, 1 char | A letter from `A` to `Z`, stored in uppercase, or empty when the player has never played competitive. |
| **IGL** | Radio group | `yes` or `no`: whether the player can lead in game. |
| **Attendance / rules agreement** | Agreement step (§5.1) | A timestamp recording when the user accepted. The accepted text differs by role (§5.1). |

The bot also stores the Discord user ID, the username at signup time, `created_at` and `updated_at`.

### 4.1 Derived values

**Tier**, from the highest division played. It is computed and exported in both formats:

| Division | A | B | C | D | anything else / empty |
|---|---|---|---|---|---|
| Tier | 1 | 2 | 3 | 4 | 5 |

**Captain budget** = 20.0 + division bonus + class modifier:

| Division | A | B | C | D | anything else / empty |
|---|---|---|---|---|---|
| Bonus | +0.0 | +1.0 | +2.0 | +3.0 | +4.0 |

| Class | inf | arc | cav |
|---|---|---|---|
| Modifier | 0.0 | −1.0 | −1.5 |

Budgets therefore range from 18.5 (A cav) to 24.0 (E-or-lower inf), which is always inside the app's 0.1–30 range. The formula values (base 20.0, bonuses, class modifiers) are constants in one module so they're easy to change later.

A captain candidate moved to the pool is exported as a player with the tier derived from their own division letter.

## 5. User flows (signup channel)

### 5.1 Signup post

The bot posts and maintains **one** message in the signup channel. The message:

- has an embed with the title, the format, tournament and auction dates (shown as Discord timestamps), the close time, and a rules link;
- has three buttons: **Sign up as Player**, **Sign up as Captain** and **My signup**;
- is edited in place when the config or the open/closed state changes. When signups are closed, the buttons are disabled and the embed says so.

The buttons use persistent `custom_id`s so they keep working after a bot restart.

**Signup flow** (Discord modals hold at most 5 fields, so the agreement is its own step):

1. The user clicks **Sign up as Player** or **Sign up as Captain**.
2. The bot replies with an ephemeral **agreement step** that shows the rules link and a statement with an **I agree and can attend** button:
   - Player: *"I agree to the rules and can attend the tournament on <tournament_date>."*
   - Captain: *"I agree to the rules and can attend both the auction on <auction_date> and the tournament on <tournament_date>. If I'm not picked as captain, I'll play as a player."*
3. Clicking the button opens the **modal**, titled *Player signup* or *Captain signup*, with 5 fields: Nickname, Steam profile, Class (radio), Highest division (optional), IGL (radio).
4. On submit, the bot validates every field (§4):
   - **Error:** an ephemeral message lists every problem and offers a **Try again** button, which reopens the modal pre-filled with what the user typed.
   - **Success:** an ephemeral confirmation shows a summary of the signup. The admin channel gets a notice (§6.3).

### 5.2 Editing and withdrawing (while signups are open)

- **My signup** shows the user's current signup (ephemeral), with **Edit**, **Withdraw** and, depending on the role, **Switch to captain** or **Switch to player**. It says so if the user has no signup.
- Clicking a signup button when already signed up with the **same role** opens the modal pre-filled (same as **Edit**).
- Clicking a signup button for the **other role** asks the user to confirm the switch. Switching goes through the agreement step again, since the text differs, and then opens the pre-filled modal.
- Switching or editing a captain candidate who is already `picked` or `pool` resets them to `pending`, and admins are notified.
- **Withdraw** asks for confirmation, then marks the signup as withdrawn. It is soft-deleted and kept for the audit trail. The nickname becomes available again.

### 5.3 After closing

Every button replies with *"Signups are closed, contact an organiser."* **My signup** still shows the user's data, read-only. Only admins can change signups at this point (§7).

## 6. Admin channel

### 6.1 Status board

The bot keeps **one pinned message** in the admin channel and edits it on every change. It shows:

- **State:** draft, open (closes at …) or closed.
- **Counts:** players, and captain candidates split into pending, picked and pool.
- **Class split** of the pool: inf, arc, cav.
- **Coverage:** needs are computed for **1, 2 … `division_count`** divisions:
  - captains needed = `divisions × captains_per_division`;
  - players needed = `divisions × captains_per_division × team_size`;
  - both are compared with what is available. Example: `2 divisions: captains 17/16 ✅ · players 90/96 ⚠️ (−6)`. Captain candidates count as captains for this until they're moved to the pool.
- **Divisions:** picked captains per division, e.g. `Division 1: 8/8 · Division 2: 5/8`.
- **Export freshness:** for each export type, the last export time and whether it's up to date or stale (§9).

### 6.2 Captain review cards

For each captain candidate, the bot posts a **card** in the admin channel showing all their fields plus the computed tier and budget. The card is updated whenever the signup changes. Its buttons:

- **one button per division**, `Division 1 … N`, which sets the status to `picked` in that division;
- **Pool**, which sets the status to `pool`;
- **Reset**, which sets the status back to `pending`.

A division button is refused if the division already has `captains_per_division` captains. The card shows the current status and who set it. If there are too many buttons for one row, they wrap to more rows; Discord allows up to 5 rows of 5 buttons.

`/captains list` gives the same actions in bulk, as an ephemeral summary with a select menu per status, for admins who prefer not to scroll through cards.

### 6.3 Notifications

These go to the admin channel as persistent messages:

- new signups, edits, withdrawals and role switches, one line each. A burst of changes within 60 s is grouped into one message.
- signups opened or closed, whether automatically or by an admin;
- admin actions, with the admin's name (the audit trail);
- a **stale export** notice (§9);
- a warning when a signed-up user leaves the server. Their signup is kept and flagged in the CSV.

## 7. Admin commands

All commands are slash commands restricted to admins, and all replies are ephemeral unless stated otherwise.

| Command | Purpose |
|---|---|
| `/setup signup_channel admin_channel` | Registers both channels and posts the signup post and the status board. |
| `/config show` · `/config set <key> <value>` | Views or edits the settings in §3. Dates are entered as `YYYY-MM-DD HH:MM` in the configured timezone. |
| `/admins add/remove role:` · `/admins add/remove user:` | Manages admin roles and users. |
| `/division rename index name` | Renames a division. |
| `/signups open format: closes_at:` | Opens signups for the chosen format (`captainPick` or `randomPick`) and schedules the close. Both are required; `closes_at` must be in the future. The format and close time are shown on the signup post. |
| `/signups close` | Closes signups immediately, before the scheduled time. |
| `/signups reopen closes_at:` | Reopens after a close, with a new close time (required, in the future). |
| `/signup view user:` | Shows a signup. The user can be picked as a Discord member, or by nickname with autocomplete. |
| `/signup edit user:` | Opens the signup modal pre-filled, with no agreement step and no open/closed check. |
| `/signup add user:` | Creates a signup for a member, e.g. one posted by DM. It asks for the role, then opens the modal. |
| `/signup role user: role:` | Switches between player and captain. |
| `/signup remove user:` | Withdraws a signup, after confirmation. |
| `/captain set user: status: [division]` | Same as the card buttons (§6.2). |
| `/captains list` | Bulk view and actions (§6.2). |
| `/export csv` | Returns `players.csv` and `captains.csv` (§9.1). Always available. |
| `/export players` | Returns the player list JSON (§9.2). Always available. |
| `/export tournament` | Returns `<title>.draftcup.json` (§9.3). Only available when its prerequisites are met. |
| `/tournament reset` | Archives the current tournament and starts an empty one with the same config. It needs typed confirmation. |

Export files are **posted in the admin channel** (not ephemeral) with who requested them, so the team shares one history.

Admin edits aren't limited by the open/closed state and go through the same validation as user signups (§4), except that admins may override nickname uniqueness after confirming.

## 8. Signup lifecycle

```
draft ──/signups open──▶ open ──closes_at reached or /signups close──▶ closed
                          ▲                                             │
                          └──────────────── /signups reopen ────────────┘
```

- **draft:** configuration only, and the signup buttons are disabled. Opening requires choosing a format and a close date.
- **open:** users can sign up, edit and withdraw.
- **closed:** only admins can change data. Captain picking can happen in any state, but normally happens after closing.
- **Scheduled close:** the bot checks `closes_at` every 30 s and also at startup, so a close missed while the bot was down still happens when it restarts.

## 9. Exports and the export tracker

### 9.1 CSV (manual assessment)

Two UTF-8 files with a BOM, so Excel opens them correctly, and comma separators.

- **`players.csv`**: every active player signup, plus captain candidates in the pool. The `source` column holds `player` or `captain-pool`.
- **`captains.csv`**: every active captain candidate, whatever their status.

Columns: `nickname, discord_id, discord_username, steam_url, class, highest_division, tier, igl, source/captain_status, division, budget (captains only), agreed_at, created_at, updated_at, left_server`.

### 9.2 Player list (late signups)

`{ "players": [ { "name", "classes": [class], "tier" } ] }`, following §6 of the file-format spec. It holds the pool: players plus pool captains. Captain candidates still `pending` are left out, and a warning lists them. `tier` is always included (§3, `format`).

### 9.3 Tournament file

**Prerequisites.** The export is refused, with a clear list of what's missing, unless:

- every active captain candidate is `picked` (and so has a division) or `pool`. No candidate is `pending`.
- every division has at least 2 captains. The app can't run an auction with fewer.

**Warnings.** The file is still produced, with these warnings:

- a division has fewer than `captains_per_division` captains;
- the pool is smaller than `picked captains × team_size`.

**Content:** as in the file-format spec:

- `title`, `format`, and `tierMinimums` (defaults), in both formats;
- `players`: the pool, built as in §9.2;
- `divisions`: one per configured division that has captains, each with `name`, `teamSize`, `halfBudgetCapAtStart`, and `captains` (`name`, `class`, `budget`);
- no `id` or `session` fields. The export is always a one-shot import.

The file name is `<title-slug>.draftcup.json`.

### 9.4 Export tracker

- Every change to signup data or captain status increments the tournament **revision** and writes a row to the change log.
- Every export records its type (`csv`, `players`, `tournament`), the revision it was made at, the time and the admin.
- An export type is **stale** when the current revision is higher than the revision of its latest export. The status board always shows this (§6.1).
- When an export **becomes** stale, the bot posts one notice in the admin channel. The notice is sent 5 minutes after the first change, to group bursts, and summarises the changes since that export, e.g. *"Tournament export from 18:42 is stale: +2 players, 1 withdrawal, 1 captain moved to pool."* There is no further notice for that type until it is exported again.
- Changes that don't affect an export's content, such as editing the Steam link after a `players` export, still count. This is conservative but simple.

## 10. Persistence

SQLite, one file, with the path set by an environment variable. Main tables:

| Table | Content |
|---|---|
| `guild_config` | One row per server: the settings in §3, plus channel and message IDs (signup post, status board). |
| `tournament` | `id`, `guild_id`, `state`, `revision`, `created_at`, `archived_at`. |
| `division` | `tournament_id`, `index`, `name`. |
| `signup` | The fields in §4 plus `role`, `captain_status`, `division_index`, `status_set_by`, `withdrawn_at`, `left_server`, `review_message_id`. |
| `change_log` | `tournament_id`, `revision`, `time`, `actor_id`, `signup_id`, `kind`, `details` (JSON). |
| `export_log` | `tournament_id`, `type`, `revision`, `time`, `actor_id`. |

Nickname uniqueness is enforced in code, case-insensitively and only among active signups. It is backed by a unique index on `(tournament_id, lower(nickname))` restricted to rows where `withdrawn_at IS NULL`.

## 11. Deployment and operations

- Configuration through environment variables: `DISCORD_TOKEN`, `DATABASE_PATH`, `LOG_LEVEL`.
- Gateway intents: `guilds` and `members`, to detect members leaving and resolve display names. No message content intent is needed.
- Slash commands are synced globally at startup.
- Buttons use persistent views, so they keep working after a restart.
- It runs as a single process under the host's existing process manager (systemd or Docker, like the other bots).

## 12. Out of scope (for now)

- Steam API checks, and any web interface.
- Running more than one tournament per server at a time.
- Auction state and re-imports with stable IDs. Every tournament export is a fresh one-shot import.
- Tier or budget overrides in the bot. These are done in the auction app after import.
