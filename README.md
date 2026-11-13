# corvid

**Memory for your coding agent, across every project.**

[![PyPI](https://img.shields.io/pypi/v/corvid-remember)](https://pypi.org/project/corvid-remember/)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://pypi.org/project/corvid-remember/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/onurkafk/corvid/blob/main/LICENSE)

You built a great sidebar last month. Now you want it in your new app. So you paste the old project's path, your agent digs through it all over again, and you pay for the same work twice.

corvid fixes that. Say `/remember` and your agent saves what you built or decided as a short note. In any other project, it already knows that note exists. It reads it, opens the two or three files it names, and gets to work.

Works with **Claude Code** and **Codex**.

```bash
pipx install corvid-remember
corvid install
```

## What's new in 0.4.0

- **Your agent sees your memories at the start of every session.** No commands, no reminders. It picks the one that fits.
- **Memories point straight at the code:** which project, which files, which function to look for.
- **"Where did we leave this project?"** Save a project's status once and your agent picks up where you stopped.
- **Search that gets what you mean.** Ask for "a left nav that shrinks to icons" and it finds your "collapsible sidebar". Ask for something you never saved and it says so, instead of guessing.
- **It also searches what your agents already remember:** Claude Code's and Codex's own notes and your past sessions.
- **Lighter and simpler.** No database. Your memories are plain markdown files you can open and edit.

Coming from 0.3? Your old notes move over by themselves; see [Upgrading from 0.3](#upgrading-from-03). Full details in the [changelog](https://github.com/onurkafk/corvid/blob/main/CHANGELOG.md).

## How it works

1. **Save.** `/remember the cart drawer we just built`. Your agent writes a short note: what it is, which files matter, what to watch out for.
2. **Switch projects.** Open any other project. Your agent gets the list of your saved notes at the start of the session.
3. **Reuse.** "Add a cart drawer like the one in my shop project." Your agent reads that note and the files it lists, then builds.

| Without corvid | With corvid |
|---|---|
| You paste the old project's path | Your agent already knows what you saved |
| It explores the whole repo to figure out how you did it | It reads one short note |
| You explain again what you decided and why | The decision is written down, with the reason |
| "Where were we?" means scrolling through old chats | It reads the project's status note |

## Install

```bash
pipx install corvid-remember     # or: uv tool install corvid-remember
corvid install
```

That's it. Start a new session in Claude Code or Codex and corvid is there.

- `corvid install` adds the `/remember` skill and a small startup hook to whichever agents you have. It doesn't touch your other settings.
- It also downloads a small search model (about 350 MB, once) that runs on your own computer. Want to skip it? Use `corvid install --lite`.
- **Using Codex?** Type `/hooks` once and approve corvid.

You need Python 3.9 or newer. Search by meaning needs Python 3.10 to 3.14; on other versions corvid still works, with simpler keyword search.

## Things to say

```
/remember the auth flow we just built
/remember why we picked Supabase over Firebase
/remember where this project stands and what's left
```

Then, in any other project:

```
add the auth flow from my other project
why did we pick Supabase again?
where did we leave the shop app?
```

Your agent saves only when you ask. Sometimes it offers ("Save 'Slide-in cart drawer' to corvid?"), at most once a session.

## What a memory looks like

A plain markdown file in `~/corvid/memories/`:

```markdown
---
type: reuse
tags: [cart, drawer, basket, slide-over]
project: ~/code/shop
updated: 2026-11-13
---
# Slide-in cart drawer

A cart panel that slides in from the right and keeps its state across pages.
Works with: Next.js 15, Tailwind 4.

## Files
- src/components/cart-drawer.tsx: `CartDrawer`
- src/lib/cart-store.ts: `useCart`

## Gotchas
- Render the drawer outside the page transition wrapper.
```

Your agent picks one of five types:

| Type | For |
|---|---|
| `reuse` | Something you built that another project could copy |
| `decision` | What you chose, and why |
| `finding` | Something you tested or learned |
| `status` | Where a project stands and what's next. One per project |
| `note` | Anything else |

They're your files. Read them, edit them, delete them, or put the folder in git.

## Search

Your agent searches for you, but you can too:

```
$ corvid search "left nav that collapses to icons"
Memories

[frontend] Collapsible sidebar that keeps icons when collapsed  (reuse · 2026-11-10 · ~/code/dashboard)
  A sidebar that shrinks to a 64px rail with only icons, with tooltips on hover.
  ~/corvid/memories/frontend/collapsible-sidebar.md
```

- **It gets what you mean,** not just the exact words. It works in more than 100 languages.
- **It doesn't guess.** If nothing is close, you get nothing back.
- **It looks in three places:** your memories, your agents' own notes, and your past sessions.
- **It runs on your computer** and takes about a second.

Add `--here` to search only the current project.

## Your data

- **Nothing leaves your computer.** No API calls, no accounts, no LLM. The only download is the search model, once.
- **It only reads your agents' files.** corvid never changes Claude Code's or Codex's notes or chat history.
- **Secrets are stripped.** Anything that looks like a key, token or password is removed before corvid stores anything from your past sessions.

Don't want corvid to read your past sessions or your agents' notes? Set `CORVID_HISTORY=off` or `CORVID_NOTES=off`.

## Commands

| Command | What it does |
|---|---|
| `corvid install` | Set it up (add `--lite` to skip the search model) |
| `corvid search "<words>"` | Find a memory |
| `corvid list` | See all your memories |
| `corvid check` | Find memories that are out of date, like files that moved or changed |
| `corvid uninstall` | Remove corvid from your agents. Your memories stay |

## Upgrading from 0.3

```bash
pipx upgrade corvid-remember     # or: pip install -U corvid-remember
corvid install
```

Your old notes move from `~/corvid/wiki/` to `~/corvid/memories/` and work right away. Nothing is overwritten. To update them to the new format, tell your agent "fix the corvid check issues". You can delete `~/corvid/corvid.db`; it isn't used anymore.

## Earlier releases

0.4 replaced most of these with simpler parts. The [changelog](https://github.com/onurkafk/corvid/blob/main/CHANGELOG.md) has the details.

### What's new in 0.3.0

The retrieval engine learns from usage and enforces itself.

- **Memory feedback loop** — articles that appear in search results get a ranking boost on future searches. The system learns what matters.
- **PreToolUse hook** — `corvid install` injects a Claude Code hook that reminds the agent to check the wiki before grepping files. Enforced, not hoped for.
- **Article relations** — markdown links between articles create navigable edges. JSON search results include `"related"` articles so the agent discovers connected knowledge without extra searches.
- **Confidence scoring** — facts carry confidence levels (low/med/high). Regex-extracted = low, letting agents know what to trust vs verify.

### 0.2.0

- **Reciprocal Rank Fusion** — keyword and semantic results merged by agreement, not appended.
- **Tag pre-filtering** — `--tags deploy,auth` narrows search space before computing similarity.
- **Temporal facts** — structured facts with contradiction detection. `corvid facts` shows current vs superseded.
- **Smart snippets** — best paragraph by query relevance, not first 200 characters.

## Credits

corvid started from Andrej Karpathy's [LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) idea: plain markdown files your agent writes and keeps up to date. Search uses the [bekko-embedding](https://huggingface.co/hotchpotch/bekko-embedding-v1-a25m) model.

## License

MIT
