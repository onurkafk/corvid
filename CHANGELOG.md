# Changelog

Notable changes to corvid. Versions follow [Semantic Versioning](https://semver.org).

## 0.4.0 — 2026-11-13

A rewrite. Memories are plain files your agent sees at the start of every session, and search by meaning returns nothing when nothing fits.

### Added

- **Session-start context** for Claude Code and Codex. The agent sees your memory titles, this project's first, and decides when to read one. corvid does not read your prompts.
- **A memory format** your agent writes and you can edit: markdown with a short header for type, tags, project, commit and date.
- **Five memory types:** `decision`, `finding`, `reuse`, `status` and `note`.
- **Status memories:** one per project, holding what is done, what is next and what is blocked, updated in place.
- **Search by meaning** with bekko-embedding-v1-a25m on ONNX Runtime. Multilingual, no PyTorch, about 350 MB, set up by `corvid install`. Results below a similarity cutoff are dropped, so an unrelated search returns nothing.
- **Search across agent notes and past sessions,** read-only. corvid reads the notes Claude Code and Codex keep per project, and your prompts and edited files from their transcripts. Anything that looks like a secret is removed before it is cached.
- **`corvid check`** reports missing project paths, listed files that are gone or changed since the saved commit, broken `superseded_by` links, and duplicate status memories.
- **`corvid list`**, and `corvid search --here` for the current project only.
- **`corvid install --lite`** for keyword search without the model.

### Changed

- The package has no required dependencies. `corvid install` adds the search model to corvid's own environment.
- Memories live in `~/corvid/memories/`. `corvid install` moves 0.3 articles from `~/corvid/wiki/` without overwriting anything, and 0.3 articles are read as they are.
- A failed model install no longer fails `corvid install`. corvid falls back to keyword search and says why.

### Removed

- The SQLite database (`corvid.db`), `INDEX.md`, and the `facts` and `stats` commands. `index` and `index-all` remain as no-ops.
- The search feedback boost, article relations, and extracted facts with confidence levels.
- The PreToolUse reminder hook, replaced by the session-start hook.
- Gemini CLI setup.
- The fastembed and sqlite-vec dependencies.

## 0.3.0 — 2026-04-07

- Search feedback: articles that appear in results rank higher in later searches.
- A PreToolUse hook in Claude Code that reminds the agent to check the wiki before searching files.
- Article relations: markdown links between articles appear as `related` in JSON results.
- Confidence levels on extracted facts.

## 0.2.0 — 2026-04-07

- Hybrid search: keyword and semantic results merged with Reciprocal Rank Fusion.
- `--tags` to filter by tag before searching.
- Temporal facts that track what was superseded, shown by `corvid facts`.
- Snippets that show the paragraph best matching the query.

## 0.1.0 — 2026-04-05

- The `/remember` skill for Claude Code, Codex and Gemini CLI, set up by `corvid install`.
- Markdown articles in `~/corvid/wiki/`, with keyword search (SQLite FTS5) and semantic search (fastembed and sqlite-vec).
