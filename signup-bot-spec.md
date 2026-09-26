# Draft Cup Signup Manager: bot specification

A Discord bot that runs signups for a Bannerlord Draft Cup, lets organisers pick captains and assign them to divisions, and exports:

- CSV files with the full signup data, for manual checks;
- a player list, for late signups;
- the final `.draftcup.json` tournament file for **DraftCupAuctionApp**.

The output file format is defined in [`draftcup-bot-spec.md`](draftcup-bot-spec.md). This document covers everything upstream of it.

## 1. Scope and principles

- **One tournament per Discord server** at a time. One bot process serves several servers (e.g. a test server next to the real one); each has its own channels and data.
- **Discord only**, no web interface. Users interact through buttons, modals and ephemeral replies. Organisers use the **tournament message** in the admin channel (buttons and forms) and a few slash commands.
- **Two channels:**
  - **Signup channel:** nothing until signups open; then one public signup post with buttons, edited in place (live counts, closed). Every reply to a user is **ephemeral**.
  - **Admin channel:** private. **Anyone who can see it is an organiser**: access is managed with the channel's own permissions. It holds the welcome message, the pinned tournament message, captain review cards, notifications and exports.
- **Rules, dates and organisation are announced in the server's own channels.** The bot doesn't repeat them; users confirm they've read them.
- **Anything the auction app can set is left to it:** format, half budget cap, tier and budget adjustments. The bot computes tiers and budgets from the signup data as a starting point.
- **Stack:** Python 3.11+, discord.py ≥ 2.7 (radio groups in modals), SQLite through `aiosqlite`. It is self-hosted on the organiser's home server.

## 2. Glossary

| Term | Meaning |
|---|---|
| **Signup** | One Discord user's registration, with the role *player* or *captain*. A user has at most one active signup. |
| **Captain candidate** | A signup with the role *captain*. It is either **waiting** for an organiser's decision or **accepted** into a division. A rejected captain becomes a *player* signup. |
| **Pool** | Every player signup (including rejected captains). |
| **Division** | A group of captains that is auctioned separately. Only organisers see divisions; users never do. |
| **Revision** | A counter that goes up on every change to tournament data. The export tracker uses it. |

## 3. Setup and tournament settings

**Server setup, once:** `/setup signup_channel admin_channel` (members with *Manage Server* only). The bot checks it can use both channels, saves them, and posts a **welcome message** in the admin channel explaining that everyone who can see the channel is an organiser, and how to start. No tournament exists yet.

**Tournament settings**, set when the tournament exists (⚙️ Settings on the tournament message):

| Setting | Default | Notes |
|---|---|---|
| `title` | given to `/tournament new` | Max 80 chars. Shown on the public post; used in the export file names and the JSON. |
| `team_size` | `6` | Players per team, **not counting** the captain: 5–10 (app limit). |
| `division_count` | `2` | 1–5. Divisions are called `Division 1…N` (renameable in the auction app). |
| `captains_per_division` | `8` | Teams per division. |
| signup close day | set when opening | Typed as **DD/MM/YYYY** when opening, reopening or changing it. Signups close at **23:59 Paris time (CET/CEST)** that day. |

`team_size`, `division_count` and `captains_per_division` carry over to the next tournament; the title and close day don't. The tournament file always says `captainPick` and leaves the half budget cap to the app's default: both are set in the auction app.

**Organisers** are the members who can see the admin channel, plus anyone with *Manage Server*. The bot checks this on every organiser button and command.

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

A rejected captain is exported as a player with the tier derived from their own division letter.

## 5. User flows (signup channel)

### 5.1 Signup post

Nothing is posted in the signup channel before signups open. When an organiser opens them, the bot posts **one** message there, then only ever edits it:

- an embed with the title, the close moment (with a countdown), **live counts** (*43 players · 17 captain candidates signed up so far*), refreshed a few seconds after each change, and how to sign up;
- three buttons: **Sign up as Player**, **Sign up as Captain** and **My signup**;
- when signups close, the same message is edited to say so, with the final counts, and the signup buttons are disabled. Reopening edits it back to open;
- each tournament gets its own post on its first opening. If the post is deleted while signups are open, the bot posts it again.

The buttons use persistent `custom_id`s so they keep working after a bot restart.

**Signup flow** (Discord modals hold at most 5 fields, so the agreement is its own step):

1. The user clicks **Sign up as Player** or **Sign up as Captain**.
2. The bot replies with an ephemeral **agreement step** with an **I agree and can attend** button:
   - Player: *"I have read the tournament rules and announcements, and I can attend the tournament on the announced date."*
   - Captain: *"I have read the tournament rules and announcements, and I can attend both the auction and the tournament on the announced dates. If I'm not accepted as captain, I'll play as a player."*
3. Clicking the button opens the **modal**, titled *Player signup* or *Captain signup*, with 5 fields: Nickname, Steam profile, Class (radio), Highest division (optional), IGL (radio).
4. On submit, the bot validates every field (§4):
   - **Error:** an ephemeral message lists every problem and offers a **Try again** button, which reopens the modal pre-filled with what the user typed.
   - **Success:** an ephemeral confirmation shows a summary of the signup. The admin channel gets a notice (§6.3).

### 5.2 Editing and withdrawing (while signups are open)

- **My signup** shows the user's current signup (ephemeral), with **Edit**, **Withdraw** and, depending on the role, **Switch to captain** or **Switch to player**. It says so if the user has no signup.
- Clicking a signup button when already signed up with the **same role** opens the modal pre-filled (same as **Edit**).
- Clicking a signup button for the **other role** asks the user to confirm the switch. Switching goes through the agreement step again, since the text differs, and then opens the pre-filled modal.
- Editing an accepted captain signup, or switching to captain, puts it back to **waiting** with Accept/Reject buttons on its card, and organisers are notified.
- **Withdraw** asks for confirmation, then marks the signup as withdrawn. It is soft-deleted and kept for the audit trail. The nickname becomes available again.

### 5.3 After closing

Every button replies with *"Signups are closed, contact an organiser."* **My signup** still shows the user's data, read-only. Only admins can change signups at this point (§7).

## 6. Admin channel

### 6.1 Tournament message

`/tournament new title:` starts a tournament (archiving the previous one, whose signups must be closed) and posts the **tournament message** in the admin channel, pinned. It is edited a few seconds after every change. `/tournament panel` posts it again (the old one loses its buttons), e.g. when it was deleted or scrolled away.

**Status:**

- **State:** preparing (nothing public yet), open (close moment and countdown) or closed.
- **Settings:** team size, divisions and teams per division.
- **Signups:** players with the class split, captain signups: accepted, and **waiting for a decision**.
- **Coverage:** captains accepted per division (`Division 1 5/8 · Division 2 0/8`), and needs for **1, 2 … `division_count`** divisions:
  - captains needed = `divisions × captains_per_division`;
  - players needed = `divisions × captains_per_division × team_size`;
  - both are compared with what is available. Example: `2 div.: captains 17/16 ✅ · players 90/96 ⚠️ (−6)`. Captain signups count as captains; those beyond the captains needed also count as players, since rejected captains play.
- **Exports:** for each export type, the last export time and whether it's up to date or stale (§9).
- **Channels:** a warning if the bot can't use the signup channel.

**Buttons** (organisers only; the set depends on the state):

| Button | State | What it does |
|---|---|---|
| ⚙️ Settings | always | Form: title, team size (radio), divisions (radio), teams per division. |
| 🟢 Open signups | preparing | Form: the close day (DD/MM/YYYY). Opens signups and publishes the public post (§5.1). |
| 📅 Change close date | open | Form: a new close day, pre-filled. |
| 🔒 Close signups | open | Confirm, then closes immediately. |
| 🟢 Reopen signups | closed | Form: a new close day, then reopens. |

### 6.2 Captain review

Every captain signup (a new one, a switch to captain, or `/captain add`) posts a **card** in the admin channel with the signup details, the tier and the computed budget, and two buttons:

- **Accept** → a division menu (each division shows `accepted/captains_per_division`); picking one accepts the captain into it. A full division is refused.
- **Reject** → confirm; the signup becomes a **player** signup (same data) and the member gets a **DM**: *"Your captain signup for <title> wasn't accepted. You're still signed up, as a player."* If their DMs are closed, organisers are told to message them.

Once decided, the card shows the outcome and who decided, without buttons. Changes go through commands:

- `/captain list-pending`: deletes the cards of every captain still waiting and posts fresh ones at the bottom of the channel.
- `/captain edit captain:`: shows the captain with the division menu (accept, or move to another division) and **Revoke** (accepted) or **Reject** (waiting): they become a player and get a DM.
- `/captain add player:`: makes a signed-up player a captain candidate again; a new card is posted and they go through the same review.

### 6.3 Notifications

These go to the admin channel as persistent messages:

- new signups, edits, withdrawals, role switches and captain decisions, one line each. Lines posted within 60 s of the first one are grouped by editing that message; any other bot message (a card, an export, a notice) starts a new group.
- signups opened or closed, whether automatically or by an admin;
- admin actions, with the admin's name (the audit trail);
- a **stale export** notice (§9);
- settings changes, with who made them and the new values;
- a warning when a signed-up user leaves the server. Their signup is kept and flagged in the CSV.

## 7. Commands

| Command | Who | Purpose |
|---|---|---|
| `/setup signup_channel admin_channel` | Manage Server | Channels and welcome message (§3). |
| `/tournament new title:` | organisers | Starts a tournament and posts its message (§6.1). |
| `/tournament panel` | organisers | Reposts the tournament message. |
| `/captain list-pending` · `/captain edit captain:` · `/captain add player:` | organisers | Captain review (§6.2). |
| `/signup view signup:` | organisers | Shows a signup with its tier and budget. `signup:` autocompletes on nickname or Discord username, which also works for members who left. |
| `/signup edit signup:` | organisers | Opens the signup modal pre-filled, with no agreement step and no open/closed check. |
| `/signup add user: role:` | organisers | Creates a signup for a member, e.g. one posted by DM, by opening the empty modal. The organiser vouches for the agreement. |
| `/signup remove signup:` | organisers | Withdraws a signup, after confirmation. |
| `/export what:` | organisers | Posts the CSV, the player list or the tournament file in the admin channel (§9). |

Replies are ephemeral. Export files are **posted in the admin channel** with who requested them, so the team shares one history.

Organiser edits aren't limited by the open/closed state and go through the same validation as user signups (§4), including nickname uniqueness.

## 8. Lifecycle

```
/setup ──▶ no tournament ──/tournament new──▶ preparing ──🟢 Open (close day)──▶ open ──close moment or 🔒──▶ closed
                                                                                  ▲                          │
                                                                                  └──── 🟢 Reopen (new day) ─┘
closed or preparing ──/tournament new──▶ previous one archived, new one preparing
```

- **preparing:** nothing is public. Organisers can adjust settings.
- **open:** users can sign up, edit and withdraw. The close day can be changed (📅), but not into the past.
- **closed:** only organisers can change data. Captain review can happen in any state.
- **Scheduled close:** the bot checks the close moment every 30 s and also at startup, so a close missed while the bot was down still happens when it restarts.

## 9. Exports and the export tracker

### 9.1 CSV (manual assessment)

Two UTF-8 files with a BOM, so Excel opens them correctly, and comma separators.

- **`players.csv`**: every active player signup (rejected captains included).
- **`captains.csv`**: every active captain signup, waiting or accepted, with the division and computed budget.

Columns: `nickname, discord_id, discord_username, steam_url, class, highest_division, tier, igl, source/captain_status, division, budget (captains only), agreed_at, created_at, updated_at, left_server`.

### 9.2 Player list (late signups)

`{ "players": [ { "name", "classes": [class], "tier" } ] }`, following §6 of the file-format spec. It holds the players. Captain signups still waiting for a decision are left out, and a warning lists them. `tier` is always included (§3, `format`).

### 9.3 Tournament file

**Prerequisites.** The export is refused, with a clear list of what's missing, unless:

- every captain signup has been accepted (into a division) or rejected. The error lists the ones still waiting and points to `/captain list-pending`.
- at least one captain has been accepted, and no division has exactly 1 captain. The app can't run an auction with fewer than 2.

**Warnings.** The file is still produced, with these warnings:

- a division has no captains: it is left out of the file (e.g. only one division runs);
- a division has fewer than `captains_per_division` captains;
- there are fewer players than `accepted captains × team_size`.

**Content:** as in the file-format spec:

- `title`, `format` (`captainPick`; switch it in the app for Random Pick), and `tierMinimums` (defaults);
- `players`: every player signup, built as in §9.2;
- `divisions`: one per division that has accepted captains, each with `name`, `teamSize` and `captains` (`name`, `class`, `budget`). `halfBudgetCapAtStart` is left out: the app's default applies, and it's changed there;
- no `id` or `session` fields. The export is always a one-shot import.

The file name is `<title-slug>.draftcup.json`.

### 9.4 Export tracker

- Every change to signup data or captain status increments the tournament **revision** and writes a row to the change log. So do settings that change exported content: `title`, `team_size` and `division_count`.
- Every export records its type (`csv`, `players`, `tournament`), the revision it was made at, the time and the admin.
- An export type is **stale** when the current revision is higher than the revision of its latest export. The tournament message always shows this (§6.1).
- When an export **becomes** stale, the bot posts one notice in the admin channel. The notice is sent 5 minutes after the first change, to group bursts, and summarises the changes since that export, e.g. *"Tournament export from 18:42 is stale: 2 new players, 1 withdrawal, 1 captain decision."* There is no further notice for that type until it is exported again.
- Changes that don't affect an export's content, such as editing the Steam link after a `players` export, still count. This is conservative but simple.

## 10. Persistence

SQLite, one file, with the path set by an environment variable. Main tables:

| Table | Content |
|---|---|
| `guild_config` | One row per server: the settings in §3, the derived close moment (`closes_at`, UTC), and channel and message IDs (public post, tournament message). Columns from earlier designs (dates, rules link, admin role tables) are kept but unused. |
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
