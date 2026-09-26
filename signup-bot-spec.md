# Draft Cup Signup Manager: bot specification

A Discord bot that runs signups for a Bannerlord Draft Cup, lets organisers pick captains and assign them to divisions, and exports:

- CSV files with the full signup data, for manual checks;
- a player list, for late signups;
- the final `.draftcup.json` tournament file for **DraftCupAuctionApp**.

The output file format is defined in [`draftcup-bot-spec.md`](draftcup-bot-spec.md). This document covers everything upstream of it.

## 1. Scope and principles

- **One tournament per Discord server** at a time. One bot process serves several servers (e.g. a test server next to the real one); each has its own config, admins, channels and data.
- **Discord only**, no web interface. Users interact through buttons, modals and ephemeral replies. Organisers configure and run everything from a **dashboard** in the admin channel (buttons and forms), plus a few slash commands for quick data access.
- **Two channels:**
  - **Signup channel:** nothing until signups open; then one public signup post with buttons, edited in place (live counts, closed). Every reply to a user is **ephemeral**.
  - **Admin channel:** the pinned dashboard, captain review cards, notifications, exports and an audit trail.
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

Organisers set everything with dashboard buttons (§6.1); no setting is typed as a command. Defaults are shown.

| Setting | Default | Set with | Notes |
|---|---|---|---|
| `title` | `"Draft Cup"` | ⚙️ Tournament | Max 80 chars. Used in the JSON and the export file names. |
| `format` | `captainPick` | ⚙️ Tournament | `captainPick` or `randomPick` (radio buttons). Shown on the public post. The derived tier (§4.1) is written in every export whatever the format, so it's available if the auctioneer switches format in the app. |
| `team_size` | `6` | ⚙️ Tournament | Players per team, **not counting** the captain: 5–10 (app limit). |
| `division_count` | `2` | ⚙️ Tournament | 1–5. Divisions are named `Division 1…N` until renamed with 🏷️ Divisions. |
| `captains_per_division` | `8` | ⚙️ Tournament | Target number of teams per division. |
| `close_date` | none | 📅 Dates | The day signups close. Required to open signups. |
| `auction_date` | none | 📅 Dates | Auction day (usually a Saturday). Shown in the captain attendance text. |
| `tournament_date` | none | 📅 Dates | Match day (usually a Sunday). Shown in the player attendance text. |
| `rules_url` | none | 🔧 More settings | A link shown on the public post and in the agreement step. |
| `timezone` | `Europe/Paris` | 🔧 More settings | IANA name. CET/CEST by default. |
| `close_time` | `23:59` | 🔧 More settings | Signups close at this time, in `timezone`, on `close_date`. |
| `half_budget_cap` | `true` | 🔧 More settings | Written as `halfBudgetCapAtStart` on every division. |
| organisers | none | 👮 Organisers | Roles and members with admin rights, picked from Discord's role and member menus. |
| `signup_channel`, `admin_channel` | none | `/setup` | The only setup command. |

**Dates** are days, not moments: Discord has no date picker, so the form asks for the **week** (menu of the current and next 24 weeks) and the **day** of that week (radio buttons). Dates in the past are refused. The auction and tournament dates are shown as plain text (e.g. *Saturday 24 October 2026*), so they don't shift with the reader's timezone; the close moment (`close_date` at `close_time`, `timezone`) is shown as a Discord timestamp. The Dates panel warns if signups close after the auction or the auction is after the tournament.

**Admins:** a user is an admin (organiser) if they have one of the organiser roles, are an organiser member, or have Discord's *Manage Server* permission. *Manage Server* works as a bootstrap so the first setup is always possible.

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

Nothing is posted in the signup channel before signups open. When an organiser opens them, the bot posts **one** message there, then only ever edits it:

- an embed with the title, the format, the auction and tournament days, the close moment (with a countdown), the rules link, and **live counts** (*43 players · 17 captain candidates signed up so far*), refreshed a few seconds after each change;
- three buttons: **Sign up as Player**, **Sign up as Captain** and **My signup**;
- when signups close, the same message is edited to say so, with the final counts, and the signup buttons are disabled. Reopening edits it back to open;
- a new tournament (🗃️ New tournament) gets a new post on its first opening. If the post is deleted while signups are open, the bot posts it again.

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

### 6.1 Dashboard

`/setup` posts the **dashboard** in the admin channel and pins it: one message, edited a few seconds after every change, with the live status on top and every organiser action as a button below.

**Status:**

- **State:** preparing (nothing public yet), open (with countdown) or closed.
- **Setup:** format and team size, divisions, close moment, auction and tournament days, rules link, and what's still missing before signups can open.
- **Access:** channels and organisers.
- **Signups:** players, captain candidates split into pending, picked and pool, pool size and class split.
- **Coverage:** needs are computed for **1, 2 … `division_count`** divisions:
  - captains needed = `divisions × captains_per_division`;
  - players needed = `divisions × captains_per_division × team_size`;
  - both are compared with what is available. Example: `2 divisions: captains 17/16 ✅ · players 90/96 ⚠️ (−6)`. Captain candidates count as captains until they're moved to the pool; candidates beyond the captains needed also count as players, since unpicked captains join the pool.
  - picked captains per division, e.g. `Division 1 8/8 · Division 2 5/8`.
- **Exports:** for each export type, the last export time and whether it's up to date or stale (§9).

**Buttons** (only organisers can use them; each opens a form or a private panel):

| Button | What it does |
|---|---|
| ⚙️ Tournament | Form: title, format, team size, number of divisions, teams per division. |
| 📅 Dates | Panel with one button per date (signups close, auction, tournament), each opening the week + day picker. |
| 🏷️ Divisions | Form with one name field per division. |
| 👮 Organisers | Panel with a role menu and a member menu, pre-filled; saved as soon as a menu closes. |
| 🔧 More settings | Form: rules link, timezone, close time, half budget cap. |
| 🟢 Open signups | Shown before the first opening. Lists what's missing, or shows a summary and a confirm button; then publishes the public post (§5.1). |
| 🔒 Close signups | Shown while open. Confirm, then closes immediately. |
| 🟢 Reopen signups | Shown after closing. Opens the date picker for a new close day, then reopens. |
| 🎖️ Captains | The bulk captain panel (§6.2). |
| 🔎 Manage a signup | Pick a member: see their signup with tier and budget, then **Edit**, **Make captain/player**, **Withdraw**, or **Add as player/captain** if they aren't signed up. |
| 📄 Export CSV · 📄 Export player list · 📦 Export tournament file | §9. |
| 🗃️ New tournament | After closing: confirm, then archive every signup and start over (settings, organisers and division names are kept). |

Forms re-check the values and answer with what changed. Settings that change exported content count as changes for the export tracker (§9.4).

### 6.2 Captain review cards

For each captain candidate, the bot posts a **card** in the admin channel showing all their fields plus the computed tier and budget. The card is updated whenever the signup changes. Its buttons:

- **one button per division**, `Division 1 … N`, which sets the status to `picked` in that division;
- **Pool**, which sets the status to `pool`;
- **Reset**, which sets the status back to `pending`.

A division button is refused if the division already has `captains_per_division` captains. The card shows the current status and who set it. If there are too many buttons for one row, they wrap to more rows; Discord allows up to 5 rows of 5 buttons.

The 🎖️ Captains button and `/captains` give the same actions in bulk: an ephemeral summary grouped by status and division, a select menu of candidates (up to 25, pending first), and one button per division plus Pool and Reset that apply to every selected candidate.

### 6.3 Notifications

These go to the admin channel as persistent messages:

- new signups, edits, withdrawals, role switches and captain decisions, one line each. Lines posted within 60 s of the first one are grouped by editing that message; any other bot message (a card, an export, a notice) starts a new group.
- signups opened or closed, whether automatically or by an admin;
- admin actions, with the admin's name (the audit trail);
- a **stale export** notice (§9);
- settings changes, with who made them and the new values;
- a warning when a signed-up user leaves the server. Their signup is kept and flagged in the CSV.

## 7. Admin commands

Configuration happens on the dashboard (§6.1). Slash commands are kept to setup and quick data access; all are restricted to organisers and answer ephemerally.

| Command | Purpose |
|---|---|
| `/setup signup_channel admin_channel` | Registers both channels (checking the bot's permissions there) and posts or moves the dashboard. Posts nothing in the signup channel. |
| `/signup view signup:` | Shows a signup with its tier and budget. `signup:` autocompletes on nickname or Discord username, which also works for members who left. |
| `/signup edit signup:` | Opens the signup modal pre-filled, with no agreement step and no open/closed check. |
| `/signup add user: role:` | Creates a signup for a member, e.g. one posted by DM, by opening the empty modal. The organiser vouches for the agreement. |
| `/signup role signup: role:` | Switches between player and captain. |
| `/signup remove signup:` | Withdraws a signup, after confirmation. |
| `/captain signup: status: [division]` | Same as the card buttons (§6.2). |
| `/captains` | Bulk view and actions (§6.2). |
| `/export csv` · `/export players` · `/export tournament` | Same as the dashboard export buttons (§9). |

Export files are **posted in the admin channel** (not ephemeral) with who requested them, so the team shares one history.

Admin edits aren't limited by the open/closed state and go through the same validation as user signups (§4), including nickname uniqueness.

## 8. Signup lifecycle

```
preparing ──🟢 Open signups──▶ open ──close moment reached or 🔒 Close signups──▶ closed
                                ▲                                                    │
                                └──────────── 🟢 Reopen signups (new close day) ─────┘
closed ──🗃️ New tournament──▶ preparing (empty signup list)
```

- **preparing:** configuration only; nothing is public. Opening requires the channels, a close day whose close moment is in the future, the auction day and the tournament day.
- **open:** users can sign up, edit and withdraw. The close moment can be moved (📅 Dates), but not into the past.
- **closed:** only organisers can change data. Captain picking can happen in any state, but normally happens after closing.
- **Scheduled close:** the bot checks the close moment every 30 s and also at startup, so a close missed while the bot was down still happens when it restarts.

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
- at least one division has captains, and no division has exactly 1. The app can't run an auction with fewer than 2.

**Warnings.** The file is still produced, with these warnings:

- a division has no captains: it is left out of the file (e.g. only one division runs);
- a division has fewer than `captains_per_division` captains;
- the pool is smaller than `picked captains × team_size`.

**Content:** as in the file-format spec:

- `title`, `format`, and `tierMinimums` (defaults), in both formats;
- `players`: the pool, built as in §9.2;
- `divisions`: one per configured division that has captains, each with `name`, `teamSize`, `halfBudgetCapAtStart`, and `captains` (`name`, `class`, `budget`);
- no `id` or `session` fields. The export is always a one-shot import.

The file name is `<title-slug>.draftcup.json`.

### 9.4 Export tracker

- Every change to signup data or captain status increments the tournament **revision** and writes a row to the change log. So do settings that change exported content: `title`, `format`, `team_size`, `half_budget_cap`, `division_count` and division names.
- Every export records its type (`csv`, `players`, `tournament`), the revision it was made at, the time and the admin.
- An export type is **stale** when the current revision is higher than the revision of its latest export. The dashboard always shows this (§6.1).
- When an export **becomes** stale, the bot posts one notice in the admin channel. The notice is sent 5 minutes after the first change, to group bursts, and summarises the changes since that export, e.g. *"Tournament export from 18:42 is stale: +2 players, 1 withdrawal, 1 captain moved to pool."* There is no further notice for that type until it is exported again.
- Changes that don't affect an export's content, such as editing the Steam link after a `players` export, still count. This is conservative but simple.

## 10. Persistence

SQLite, one file, with the path set by an environment variable. Main tables:

| Table | Content |
|---|---|
| `guild_config` | One row per server: the settings in §3, the derived close moment (`closes_at`, UTC), and channel and message IDs (public post, dashboard). |
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
- It runs as a single process under the host's existing process manager (systemd). That process serves every server the bot is invited to, so a test server needs no second instance.

## 12. Out of scope (for now)

- Steam API checks, and any web interface.
- Running more than one tournament per server at a time.
- Auction state and re-imports with stable IDs. Every tournament export is a fresh one-shot import.
- Tier or budget overrides in the bot. These are done in the auction app after import.
