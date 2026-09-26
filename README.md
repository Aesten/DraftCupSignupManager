# Draft Cup Signup Manager

A Discord bot that runs signups for the Bannerlord Draft Cup and exports the result for
**DraftCupAuctionApp**.

- [`signup-bot-spec.md`](signup-bot-spec.md): what the bot does (signup flow, admin tools, exports).
- [`draftcup-bot-spec.md`](draftcup-bot-spec.md): the `.draftcup.json` format the auction app imports.

## Status

Implemented: everything in the spec. The admin channel holds the tournament message (status and
buttons), captain review cards (Accept into a division / Reject), notifications and exports. The
public signup post only appears once signups open.

It has unit tests but hasn't been run against Discord yet: the first run in a test server is the
real check.

## Discord application setup

1. Create an application at <https://discord.com/developers/applications> and add a bot.
2. On the **Bot** tab, copy the token and enable the **Server Members Intent** (the bot uses it
   to notice signed-up members leaving).
3. Start the bot once (see below): it logs its invite link at startup, with the right scopes
   (`bot`, `applications.commands`) and permissions (`117760`): *View Channel*,
   *Send Messages*, *Embed Links*, *Attach Files* and *Read Message History*. Open the link once
   per server (test and real).
4. The admin channel is private: add the bot's role to it (**Edit Channel → Permissions**)
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

1. Create a **private admin channel**. Everyone who can see it is an organiser, so manage the
   organisers with that channel's permissions. Add the bot's role to it (see above).
2. `/setup signup_channel:#signups admin_channel:#draftcup-admin` (needs *Manage Server*). The bot
   posts a welcome message in the admin channel. Nothing appears in the signup channel yet.
3. `/tournament new title:Draft Cup #13` posts the **tournament message** with its buttons
   (`/tournament panel` reposts it if it scrolls away):
   - **⚙️ Settings**: title, team size, divisions, teams per division.
   - **🟢 Open signups**: type the close day (DD/MM/YYYY). Signups close at 23:59 CET/CEST that
     day, and the public post goes up in the signup channel with live counts.
   - While open: **📅 Change close date** and **🔒 Close signups**. After closing: **🟢 Reopen**.
4. Each captain signup posts a card in the admin channel: **Accept** (pick the division) or
   **Reject** (they become a player and get a DM).
   - `/captain list-pending` reposts the undecided cards.
   - `/captain edit` changes a division or revokes a captain.
   - `/captain add` makes a player a captain candidate.
5. `/export what:` posts the CSV, the player list or the tournament file in the admin channel. The
   tournament file needs every captain accepted or rejected.
6. `/tournament new` again for the next cup: the previous one is archived.

Other organiser commands: `/tournament panel` (repost the tournament message) and
`/signup view|edit|add|remove`, which autocomplete on nickname or Discord username.

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
| `draftcup/events.py` | What follows a signup change: admin line, captain card, tournament message and public post refresh. |
| `draftcup/notify.py` | Admin channel feed (grouped lines) and change wording. |
| `draftcup/views/signup_post.py` | The public signup post and its persistent buttons. |
| `draftcup/views/signup_flow.py` | Agreement step, signup modal, "My signup". |
| `draftcup/views/captain_card.py` | Captain review cards, accept/reject/revoke, DMs. |
| `draftcup/views/tournament_panel.py` | The tournament message (status and buttons), its forms, refresh scheduling. |
| `draftcup/actions.py` | Tournament creation, settings, open/close/reopen, exports (shared by buttons and commands). |
| `draftcup/health.py` | Checks that the bot can use its channels. |
| `draftcup/timeutil.py` | Close day parsing (DD/MM/YYYY), close moment, Discord timestamps. |
| `draftcup/cogs/admin.py` | `/setup`, `/tournament new|panel`. |
| `draftcup/cogs/management.py` | `/signup …`, `/captain …`, `/export`. |
| `draftcup/cogs/lifecycle.py` | Scheduled close, stale export notices, member leave/join. |
