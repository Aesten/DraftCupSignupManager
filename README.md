# Draft Cup Signup Manager

A Discord bot that runs signups for the Bannerlord Draft Cup and exports the result for
**DraftCupAuctionApp**.

- [`signup-bot-spec.md`](signup-bot-spec.md): what the bot does (signup flow, admin tools, exports).
- [`draftcup-bot-spec.md`](draftcup-bot-spec.md): the `.draftcup.json` format the auction app imports.

## Status

Implemented so far:

- signup post with **Sign up as Player / Captain** and **My signup** buttons;
- agreement step, 5-field signup modal with validation, edit, role switch and withdraw;
- tier and captain budget computation;
- `/setup`, `/config show|set`, `/admins add|remove`, `/signups open|reopen|close`;
- scheduled close, and a notice in the admin channel for every signup change or member leaving.

Not yet: status board, captain review cards and picking, admin signup commands (`/signup …`),
exports and the export tracker, grouping of admin notifications.

## Discord application setup

1. Create an application at <https://discord.com/developers/applications> and add a bot.
2. On the **Bot** tab, copy the token and enable the **Server Members Intent** (the bot uses it
   to notice signed-up members leaving).
3. Invite the bot with the `bot` and `applications.commands` scopes and these permissions in the
   signup and admin channels: *View Channel*, *Send Messages*, *Embed Links*, *Attach Files*,
   *Read Message History*.

## Running

Requires Python 3.11+.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then set DISCORD_TOKEN
.venv/bin/python -m draftcup
```

Global slash commands can take a moment to show up the first time. Set `DEV_GUILD_ID` to also
sync them to one server instantly.

To run it as a service, see [`deploy/draftcup-signup.service`](deploy/draftcup-signup.service).

## First use in a server

Anyone with *Manage Server* is an admin; more can be added with `/admins add`.

1. `/setup signup_channel:#signups admin_channel:#draftcup-admin`
2. `/config set` for `title`, `timezone`, `tournament_date`, `auction_date` and optionally
   `rules_url`, `team_size`, `division_count`.
3. `/signups open format:<Captain Pick|Random Pick> closes_at:2026-10-16 20:00`

Dates are written `YYYY-MM-DD HH:MM` in the configured timezone.

## Development

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Code layout:

| Path | Content |
|---|---|
| `draftcup/rules.py` | Validation, tier and budget rules (pure functions, unit-tested). |
| `draftcup/db.py` | SQLite schema and queries. |
| `draftcup/models.py` | Data classes and enums. |
| `draftcup/views/signup_post.py` | The public signup post and its persistent buttons. |
| `draftcup/views/signup_flow.py` | Agreement step, signup modal, "My signup". |
| `draftcup/cogs/admin.py` | `/setup`, `/config`, `/admins`, `/signups`. |
| `draftcup/cogs/lifecycle.py` | Scheduled close, member leave/join. |
