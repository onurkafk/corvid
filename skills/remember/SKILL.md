---
name: remember
description: >-
  Save and recall the user's knowledge across projects with corvid. Use when
  the user says /remember, "save this", "remember this", or accepts a "Save
  to corvid?" offer. Also use before exploring another project for something
  already built or decided there, and when the user asks where a project
  stands or what is left to do. Memories live in ~/corvid/memories/.
allowed-tools: Bash(corvid *) Bash(git rev-parse *) Bash(git -C * rev-parse *) Bash(git -C * log *) Read Write Edit Glob Grep
---

# corvid: remember

Each memory is one markdown file at `~/corvid/memories/<category>/<slug>.md`.
The files are the source of truth; there is no index to update.

## Save

Save only when the user says /remember or accepts your offer (at most one
offer per session, for something durable, or to update a memory the session
has changed or contradicted). Never save secrets, credentials or
confidential client details unless the user explicitly asks.

1. Run `corvid search "<topic>"`. If a memory already covers it, update that
   one.
2. One topic per memory. Asked to save several things, write one short memory
   for each, not one overview.
3. What is done and what is next goes only in the project's one `status`
   memory, written when the user asks to save where things stand, what is
   left, or the project as a whole. Update it in place.
4. Before writing, check that every path and name you list exists, copy
   numbers exactly from where they are recorded (a results file, a command's
   output), and take `commit` from `git rev-parse --short HEAD` in the
   project.
5. If it replaces an older memory, set `superseded_by: <category>/<slug>.md`
   on the old one. Delete a memory only when the user asks.
6. Tell the user each memory's path.

```markdown
---
type: <decision | finding | reuse | status | note>
tags: [<keyword>, <keyword>, <another name for it>, <short form>]
project: <path to the project it came from>
commit: <short commit hash>
updated: <YYYY-MM-DD>
---
# <Short, specific title people would search for>

<One or two sentences: what this is and why it matters.>
```

Tags: 3 to 8 lowercase words someone would type to find this. Other names
for the same thing matter most, then specific names (libraries, functions,
settings, errors) and short forms. One or two words each. No generic words (status, notes, project,
feature).

Then the sections for its type:
- `decision`: `## Decision`, `## Why`, `## Rejected`.
- `finding`: `## Finding`, `## Evidence`, `## Implications`.
- `reuse`: a `Works with:` line (versions, tools), then `## Files` (one line
  each: `- <path>: <what it is, and the function, component or config key to
  look for>`), `## Reuse` (the steps, and what to adapt), `## Essential code`
  (the 5 to 15 lines that capture it), `## Gotchas`.
- `status`, one per project, titled `<Project>: status and next steps`:
  `## Done`, `## Next` (in order), `## Blocked or open`, `## Tracked in` (the
  project's own roadmap or to-do files). Rewrite it each time; keep it under
  40 lines.
- `note`: free form. Something still undecided is a note titled `Open: …`.

Use an existing category folder if one fits, and a kebab-case slug. Write
neutrally, not as "I": exact values, commands, versions and paths;
conclusions, not chat logs. A memory with no header is in an older format:
add the header when you touch it.

## Recall

Session start lists memories as `[type] title — path`. corvid does not read
the user's prompts: noticing that a memory is relevant is your job.

- Read the memories whose titles fit, before exploring another project. If
  none fits, run `corvid search "<their words>"` (options: `--here`,
  `--tags a,b`, `--json`). It also covers the notes Claude Code and Codex
  keep and the user's past sessions, and returns only close matches. If it
  misses, try other names for the same thing, or fewer words.
- Where a project stands: read its `status` memory, then run
  `git -C <project> log --oneline <commit>..HEAD` to see what changed since.
- A reuse memory: read only the files under `## Files` (relative to its
  `project`), starting at the names given. Check the versions of the project
  you are in before applying it.
- A past session: open its listed files first; read the transcript only if
  you need the reasoning.
- These are leads. If the code disagrees, trust the code and offer to update
  the memory, with its `commit` and `updated`.
- Never edit the agents' own notes (`~/.claude/projects/*/memory/`,
  `~/.codex/memories/`).
