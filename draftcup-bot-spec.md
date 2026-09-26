# Draft Cup tournament file: spec for the signup bot

This document is for building a signup bot (Discord) whose backend writes a `.draftcup.json` file. The auctioneer then imports that file into **DraftCupAuctionApp** (the Windows auction tool, v3.1.1+).

## 1. What a draft cup is

A draft cup is a team tournament where the teams are built live by auction:

- **Players** sign up individually. Each player plays one or more **classes**: `inf` (infantry), `arc` (archer), `cav` (cavalry).
- **Captains** each lead a team and get a **budget** in millions (e.g. 20.0), spent in steps of 0.1.
- In the auction, captains bid for players until every team is full. A team has a **team size** of 5–10 players, plus the captain.
- A tournament has one **player pool** and one or more **divisions**. Each division is a separate auction with its own captains, team size and budgets.
  - Divisions are auctioned one at a time, in any order.
  - Whichever division runs first gets the whole pool. Each later one gets the pool minus the players already bought.
- **Half budget cap** (per division, on by default): while it's on, a team can only spend down to half of its starting budget, unless the auctioneer overrides it.

### The two formats

The format is set per tournament.

| Format | How players come up | What players need |
|---|---|---|
| **Random Pick** (`randomPick`, the default) | The app shuffles the pool and players come up in a random order. | A name and one or more classes. |
| **Captain Pick** (`captainPick`) | Captains name the player they want from a board sorted by tier and class. | A name, exactly **one** class, and a **tier** from 1 to 5. |

In Captain Pick, each tier has a **minimum bid** for the whole tournament. The defaults are:

| Tier | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|
| Minimum bid | 2.0 | 1.5 | 1.0 | 0.5 | 0.1 |

Tier 1 is the strongest.

## 2. The bot's job

1. Collect signups:
   - **players**: name, class (or classes), and a tier in Captain Pick (likely assigned by organisers, not self-chosen);
   - **captains**: name, class, budget, and the division they lead.
2. Output **one** `.draftcup.json` file (UTF-8 JSON).
3. The auctioneer imports it with **Import a tournament…** on the start page, or by dropping the file on the window. From there the file is a normal tournament that can be edited in the app. The auction state is added by the app; the bot never writes it.

## 3. File format

The JSON uses camelCase keys. Only `players` and `divisions` are required; every other field has a default. The file is rejected if it isn't a JSON object with both `players` and `divisions` keys (they may be empty arrays).

### Top level

| Key | Type | Required | Default / rules |
|---|---|---|---|
| `title` | string | no | `"New tournament"`. Max 80 chars. |
| `format` | `"randomPick"` \| `"captainPick"` | no | `"randomPick"` |
| `tierMinimums` | array of 5 numbers | no | `[2.0, 1.5, 1.0, 0.5, 0.1]`. Used in Captain Pick (kept in Random Pick); index 0 = tier 1. |
| `players` | array of Player | **yes** | The pool. |
| `divisions` | array of Division | **yes** | One per auction. |
| `id` | GUID string | no | Generated if missing (see §6). |

Do **not** write `session` (auction state), `schemaVersion`, `createdAt` or `updatedAt` unless you're doing re-imports (§6).

### Player

| Key | Type | Required | Rules |
|---|---|---|---|
| `name` | string | yes | Max 40 chars (longer is cut). Should be unique in the pool. |
| `classes` | array of strings | yes in practice | Codes `"inf"`, `"arc"`, `"cav"`. Also accepted, case-insensitive: `infantry`, `archer`, `archers`, `ranged`, `cavalry`. Unknown values are kept as-is (lowercased) and won't match any class column, so only send the three codes. **Captain Pick: exactly one** (only the first is kept). |
| `tier` | integer 1–5 | Captain Pick only | Optional in Random Pick: kept by the import and harmless when unused, so the auction can switch modes in the app. |
| `id` | GUID string | no | Generated if missing. |

### Division

| Key | Type | Required | Default / rules |
|---|---|---|---|
| `name` | string | no | `"Division 1"`. Max 40 chars. |
| `teamSize` | integer | no | `6`. Range 5–10, players per team **not counting** the captain. |
| `halfBudgetCapAtStart` | boolean | no | `true` |
| `upcomingShown` | integer | no | `3`. Range 0–5. Random Pick only (how many upcoming players are shown on stream). |
| `captains` | array of Captain | yes in practice | At least 2 to run the auction. |
| `id` | GUID string | no | Generated if missing. |

### Captain

| Key | Type | Required | Default / rules |
|---|---|---|---|
| `name` | string | yes | Max 40 chars. Unique within the division. |
| `class` | string | no | A **single** class code (`"inf"`, `"arc"`, `"cav"`; the aliases work too). Missing only raises a warning. |
| `budget` | number | no | `20`. Range 0.1–30, one decimal. |
| `id` | GUID string | no | Generated if missing. |

Captains are **not** in the player pool. Don't list a captain as a player too.

## 4. Validation and limits

The importer is lenient: it **clamps or fixes** bad values instead of rejecting the file. Names are trimmed and cut to length, budgets are clamped to 0.1–30, team size to 5–10, invalid tiers are cleared, and class names like `Infantry` become codes. The bot should still send clean data, because a silently "fixed" value is a surprise on stream.

The app won't **start** a division's auction until:

- the division has at least 2 captains, every captain has a name, and no captain name is repeated;
- every budget is in 0.1–30;
- the pool isn't empty;
- in Captain Pick, every available player has a tier (1–5) and a class.

It shows **warnings** (the auction can still start) for:

- a captain with no class;
- two players with the same name (case-insensitive);
- fewer available players than `captains × teamSize` (some teams won't be full).

Files over **20 MB** are refused (a realistic file is a few KB).

## 5. Examples

### Captain Pick

```json
{
  "title": "Draft Cup #12",
  "format": "captainPick",
  "tierMinimums": [2.0, 1.5, 1.0, 0.5, 0.1],
  "players": [
    { "name": "Alice",  "classes": ["inf"], "tier": 1 },
    { "name": "Bob",    "classes": ["arc"], "tier": 2 },
    { "name": "Chloe",  "classes": ["cav"], "tier": 3 },
    { "name": "Dmitri", "classes": ["inf"], "tier": 4 },
    { "name": "Eve",    "classes": ["arc"], "tier": 5 }
  ],
  "divisions": [
    {
      "name": "Division A",
      "teamSize": 5,
      "captains": [
        { "name": "CaptainOne", "class": "cav", "budget": 20 },
        { "name": "CaptainTwo", "class": "inf", "budget": 20 }
      ]
    }
  ]
}
```

### Random Pick (several classes per player, two divisions)

```json
{
  "title": "Draft Cup #13",
  "format": "randomPick",
  "players": [
    { "name": "Alice", "classes": ["inf", "arc"] },
    { "name": "Bob",   "classes": ["cav"] },
    { "name": "Chloe", "classes": ["arc", "cav"] }
  ],
  "divisions": [
    {
      "name": "Division A",
      "teamSize": 6,
      "halfBudgetCapAtStart": true,
      "upcomingShown": 3,
      "captains": [
        { "name": "CaptainOne", "class": "inf", "budget": 20 },
        { "name": "CaptainTwo", "class": "arc", "budget": 18.5 }
      ]
    },
    {
      "name": "Division B",
      "teamSize": 6,
      "captains": [
        { "name": "CaptainThree", "class": "cav", "budget": 15 },
        { "name": "CaptainFour",  "class": "inf", "budget": 15 }
      ]
    }
  ]
}
```

## 6. IDs, re-imports and late signups

- **One-shot import (recommended):** leave out all `id` fields. Every import creates a **new** tournament in the app, and the auctioneer edits it there from then on.
- **Re-importing the same tournament:** when the tournament `id` matches one already on the PC, the app lists what changed and asks whether to update it or import a separate copy. When updating, whichever copy changed each part most recently wins:
  - the pool-level data (`title`, `format`, `tierMinimums`, `players`) follows `poolUpdatedAt`;
  - each division follows its own `updatedAt` (ISO 8601 timestamps).

  A newer bot file therefore **replaces the whole pool**, and any edits made in the app since (tiers, fixes) are lost. Avoid this path unless the bot is the single source of truth. If you use it, keep the tournament, player, division and captain `id`s stable across exports.
- **Late signups after the import:** export a **player list** instead of a tournament. The auctioneer imports it with **Import…** in the pool, which adds only the names not already in the pool (and adds them to a running auction too). Either format works:
  - JSON: `{ "players": [ { "name": "Frank", "classes": ["cav"], "tier": 3 } ] }` (no `divisions` key; `tier` required in Captain Pick, optional in Random Pick);
  - CSV: header `Player,INF,ARC,CAV,Tier`, then rows like `Frank,,,x,3` (an `x` marks each class; the `Tier` column is optional in Random Pick).

## 7. Exporter checklist

- [ ] Top-level object with `players` and `divisions` arrays; UTF-8; camelCase keys.
- [ ] `format` set to `"captainPick"` or `"randomPick"`.
- [ ] Player names ≤ 40 chars, trimmed, unique (case-insensitive).
- [ ] Class codes are `inf` / `arc` / `cav`.
- [ ] Captain Pick: exactly one class and a `tier` 1–5 for every player.
- [ ] Random Pick: `tier` optional (kept by the import, used if the app switches to Captain Pick).
- [ ] Every division has ≥ 2 captains, each with a unique name, one class and a budget in 0.1–30 (one decimal).
- [ ] `teamSize` in 5–10; at least `captains × teamSize` players in the pool.
- [ ] Captains are not also listed as players.
- [ ] No `session` or `id` fields for a one-shot import.
- [ ] File name ends in `.draftcup.json`.
