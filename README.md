# Draft Cup Signup Manager

A Discord bot that runs signups for the Bannerlord Draft Cup and exports the result for
**DraftCupAuctionApp**.

- [`signup-bot-spec.md`](signup-bot-spec.md): what the bot does (signup flow, admin tools, exports).
- [`draftcup-bot-spec.md`](draftcup-bot-spec.md): the `.draftcup.json` format the auction app imports.

## Status

Implemented: everything in the spec. Organisers run the tournament from a pinned dashboard in the
admin channel (buttons and forms). The public signup post only appears once signups open. There are
captain review cards, grouped admin notifications, and CSV, player list and tournament exports with a
tracker that flags stale exports.

It has unit tests but hasn't been run against Discord yet: the first run in a test server is the
real check.

## Discord application setup

1. Create an application at <https://discord.com/developers/applications> and add a bot.
2. On the **Bot** tab, copy the token and enable the **Server Members Intent** (the bot uses it
   to notice signed-up members leaving).
3. Start the bot once (see below): it logs its invite link at startup, with the right scopes
   (`bot`, `applications.commands`) and permissions (`2251799813803008`): *View Channel*,
   *Send Messages*, *Embed Links*, *Attach Files*, *Read Message History* and *Pin Messages*
   (to pin the dashboard). Open the link once per server (test and real).
4. The admin channel is usually private: add the bot's role to it (**Edit Channel → Permissions**)
   with the permissions above. `/setup` refuses channels the bot can't use and says what to allow;
   at startup, the console warns about any server whose channels became unusable.

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

Anyone with *Manage Server* is an organiser. More can be added from the dashboard.

1. `/setup signup_channel:#signups admin_channel:#draftcup-admin`. The bot posts and pins the
   **dashboard** in the admin channel. Nothing appears in the signup channel yet.
2. On the dashboard:
   - **⚙️ Tournament**: title, format, team size, divisions.
   - **📅 Dates**: signup close day, auction day, tournament day. Pick the week, then the day.
     Signups close at 23:59 Europe/Paris by default (**🔧 More settings** changes the time,
     timezone and rules link).
   - **👮 Organisers**: roles and members who can use the dashboard.
3. **🟢 Open signups** checks that everything is set, then publishes the signup post. Its counts
   update live, and it switches to "closed" automatically at the close time.
4. While signups run, the admin channel shows one card per captain candidate (buttons: a
   division, Pool, Reset) and a line per change. **🎖️ Captains** decides for several at once,
   and **🔎 Manage a signup** edits someone's signup.
5. The export buttons post their files in the admin channel. The tournament file needs every
   captain candidate picked or moved to the pool.
6. **🗃️ New tournament** archives everything for the next cup.

Quick commands for organisers: `/signup view|edit|add|role|remove`, `/captain`, `/captains`,
`/export csv|players|tournament`. Commands on a signup autocomplete on nickname or Discord
username.

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
| `draftcup/events.py` | What follows a change: admin line, captain card, dashboard and public post refresh. |
| `draftcup/notify.py` | Admin channel feed (grouped lines) and change wording. |
| `draftcup/views/signup_post.py` | The public signup post and its persistent buttons. |
| `draftcup/views/signup_flow.py` | Agreement step, signup modal, "My signup". |
| `draftcup/views/captain_card.py` | Captain review cards and captain decisions. |
| `draftcup/views/captains_list.py` | `/captains` bulk view. |
| `draftcup/views/dashboard.py` | Pinned dashboard (status embed and buttons), refresh scheduling. |
| `draftcup/views/dashboard_actions.py` | What each dashboard button opens: forms, date picker, panels. |
| `draftcup/actions.py` | Settings updates, open/close/reopen/reset, exports (shared by buttons and commands). |
| `draftcup/timeutil.py` | Day-only dates, close moment, timezones. |
| `draftcup/cogs/admin.py` | `/setup`. |
| `draftcup/cogs/management.py` | `/signup …`, `/captain`, `/captains`, `/export …`. |
| `draftcup/cogs/lifecycle.py` | Scheduled close, stale export notices, member leave/join. |
