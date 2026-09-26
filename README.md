# Draft Cup Signup Manager

A Discord bot that runs signups for the Bannerlord Draft Cup and exports the result for
**DraftCupAuctionApp**.

- [`signup-bot-spec.md`](signup-bot-spec.md): what the bot does (signup flow, admin tools, exports).
- [`draftcup-bot-spec.md`](draftcup-bot-spec.md): the `.draftcup.json` format the auction app imports.

## Status

Implemented: everything in the spec. That covers the signup post and flow, the admin commands,
captain review cards and `/captains`, the pinned status board, grouped admin notifications,
the CSV, player list and tournament exports, and the export tracker with stale notices.

It has unit tests but hasn't been run against Discord yet: the first run in a test server is the
real check.

## Discord application setup

1. Create an application at <https://discord.com/developers/applications> and add a bot.
2. On the **Bot** tab, copy the token and enable the **Server Members Intent** (the bot uses it
   to notice signed-up members leaving).
3. Invite the bot with the `bot` and `applications.commands` scopes and these permissions in the
   signup and admin channels: *View Channel*, *Send Messages*, *Embed Links*, *Attach Files*,
   *Read Message History*, plus *Manage Messages* in the admin channel to pin the status board.

## Running

Requires Python 3.11+.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then set DISCORD_TOKEN
.venv/bin/python -m draftcup
```

One process serves every server the bot is invited to, each with its own settings and data. To
try things out, invite the same bot to a test server and run `/setup` there too.

To run it as a service, see [`deploy/draftcup-signup.service`](deploy/draftcup-signup.service).

## First use in a server

Anyone with *Manage Server* is an admin; more can be added with `/admins add`.

1. `/setup signup_channel:#signups admin_channel:#draftcup-admin`
2. `/config set` for `title`, `timezone`, `tournament_date`, `auction_date` and optionally
   `rules_url`, `team_size`, `division_count`.
3. `/signups open format:<Captain Pick|Random Pick> closes_at:2026-10-16 20:00`
4. While signups run, the admin channel shows the status board, one card per captain candidate
   (buttons: a division, Pool, Reset) and a line per change. `/captains` decides for several at once.
5. `/export csv` and `/export players` work any time. `/export tournament` works once every captain
   candidate is picked or moved to the pool. Files are posted in the admin channel.
6. `/tournament reset confirm:RESET` archives everything for the next cup.

Dates are written `YYYY-MM-DD HH:MM` in the configured timezone. Organiser commands on a signup
(`/signup view|edit|role|remove`, `/captain`) autocomplete on nickname or Discord username.

## Development

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Code layout:

| Path | Content |
|---|---|
| `draftcup/rules.py` | Validation, tier and budget rules (pure functions). |
| `draftcup/exports.py` | CSV / JSON builders, coverage stats, change summaries (pure functions). |
| `draftcup/db.py` | SQLite schema, migrations and queries. |
| `draftcup/models.py` | Data classes and enums. |
| `draftcup/events.py` | What follows a change: admin line, captain card, status board. |
| `draftcup/notify.py` | Admin channel feed (grouped lines) and change wording. |
| `draftcup/views/signup_post.py` | The public signup post and its persistent buttons. |
| `draftcup/views/signup_flow.py` | Agreement step, signup modal, "My signup". |
| `draftcup/views/captain_card.py` | Captain review cards and captain decisions. |
| `draftcup/views/captains_list.py` | `/captains` bulk view. |
| `draftcup/views/status_board.py` | Pinned status board. |
| `draftcup/cogs/admin.py` | `/setup`, `/config`, `/admins`, `/signups`, `/division`, `/tournament`. |
| `draftcup/cogs/management.py` | `/signup …`, `/captain`, `/captains`, `/export …`. |
| `draftcup/cogs/lifecycle.py` | Scheduled close, stale export notices, member leave/join. |
