#!/usr/bin/env python3
"""corvid: remember what you built and decided, across every project and agent.

Memories are plain markdown files in ~/corvid/memories/<category>/<slug>.md.
At session start a hook lists them for your agent; the agent decides when to read one or
run `corvid search`. corvid also searches, read-only, the notes Claude Code and Codex keep
per project and your past sessions. It has no model of its own: the agent does the
understanding.

Commands:
  corvid install [--agent claude|codex] [--lite]
                                           set up the skill, the hook and the search model
  corvid uninstall [--agent claude|codex]  remove them (your memories stay)
  corvid search "<words>" [--here] [--tags a,b] [--limit N] [--json]
  corvid list [--json]                     all memories, this project's first
  corvid check                             find memories that need attention

Environment: CORVID_HOME (default ~/corvid), CORVID_NOTES=off, CORVID_HISTORY=off,
CORVID_SEMANTIC=off (keyword search only).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

__version__ = "0.4.0"


# ── Paths ──────────────────────────────────────────────────────
# Read at call time so CORVID_HOME and HOME can change between calls (tests).

def corvid_home() -> Path:
    return Path(os.environ.get("CORVID_HOME") or Path.home() / "corvid").expanduser()


def memories_root() -> Path:
    return corvid_home() / "memories"


def _tilde(path) -> str:
    """Show a path with ~ for the home directory."""
    s, home = str(path), str(Path.home())
    if s == home or s.startswith(home + os.sep):
        return "~" + s[len(home):]
    return s


def _plural(n: int) -> str:
    return f"{n} memory" if n == 1 else f"{n} memories"


def _old_wiki_files() -> List[Path]:
    """Articles still in 0.3's ~/corvid/wiki, which corvid no longer reads."""
    old = corvid_home() / "wiki"
    try:
        return sorted(old.rglob("*.md")) if old.is_dir() else []
    except OSError:
        return []


def _move_old_wiki() -> Optional[str]:
    """0.3 kept memories in ~/corvid/wiki. Move them into the memories folder; never overwrite:
    an article whose name is already taken there stays where it is."""
    old, new = corvid_home() / "wiki", memories_root()
    if not old.is_dir():
        return None
    if not new.exists():
        old.rename(new)
        return f"moved {_tilde(old)} to {_tilde(new)}"
    moved = kept = 0
    for path in _old_wiki_files():
        target = new / path.relative_to(old)
        if target.exists():
            kept += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        path.rename(target)
        moved += 1
    for folder in sorted((d for d in old.rglob("*") if d.is_dir()), reverse=True) + [old]:
        try:
            folder.rmdir()                               # only if empty
        except OSError:
            pass
    parts = []
    if moved:
        parts.append(f"moved {moved} article{'s' if moved != 1 else ''} from {_tilde(old)} "
                     f"to {_tilde(new)}")
    if kept:
        parts.append(f"{kept} stayed in {_tilde(old)} because a memory with the same name "
                     f"exists: compare them and keep one")
    return "; ".join(parts) or None


_FOLD_CASE = sys.platform in ("darwin", "win32")      # default filesystems ignore case


def _within(cwd: str, project: str) -> bool:
    """True if cwd is the project directory or inside it."""
    try:
        c, p = os.path.realpath(cwd), os.path.realpath(project)
        if _FOLD_CASE:
            c, p = c.lower(), p.lower()
        return os.path.commonpath([c, p]) == p
    except ValueError:
        return False


# ── Memories ──────────────────────────────────────────────────────
TYPES = ("decision", "finding", "reuse", "status", "note")


@dataclass
class Memory:
    path: Path
    rel: str                      # path relative to the memories folder; the id
    category: str
    title: str
    type: str
    tags: List[str]
    project: Optional[str]        # absolute path, or None
    updated: str                  # YYYY-MM-DD
    superseded_by: Optional[str]
    body: str
    source: str = "corvid"        # "corvid", or "claude"/"codex" for read-only agent notes
    commit: Optional[str] = None  # git commit of project when saved or last checked
    legacy: bool = False          # a 0.3 article: no frontmatter


def _split_frontmatter(text: str) -> Tuple[Optional[List[str]], str]:
    lines = text.split("\n")
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return lines[1:i], "\n".join(lines[i + 1:])
    return None, text


def _parse_frontmatter(lines: List[str]) -> dict:
    """Flat key: value pairs. Nested keys are read as top-level; folded (>, |) values are
    joined; a list written one "- item" per line reads like [item, item]."""
    meta, i = {}, 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split(":", 1)
        value = re.sub(r"\s+#.*$", "", value).strip()      # drop inline comments
        if value in (">", ">-", "|", "|-"):
            block = []
            while i < len(lines) and (not lines[i].strip() or lines[i][:1] in " \t"):
                block.append(lines[i].strip())
                i += 1
            value = " ".join(b for b in block if b)
        elif not value:                                    # a YAML list, one "- item" per line
            items = []
            while i < len(lines) and re.match(r"\s*-\s+\S", lines[i]):
                items.append(re.sub(r"\s+#.*$", "", lines[i].split("-", 1)[1]).strip())
                i += 1
            value = ", ".join(items)
        meta.setdefault(key.strip().lower(), value)
    return meta


def _unquote(value: str) -> str:
    return value.strip().strip("'\"").strip()


def _parse_list(value: str) -> List[str]:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    items = (_unquote(v).lower() for v in value.split(","))
    return [v for v in items if v]


def parse_memory(path: Path, root: Path) -> Optional[Memory]:
    """Read one memory. Returns None if the file can't be read as UTF-8."""
    try:
        text = path.read_text(encoding="utf-8")
        mtime = path.stat().st_mtime
    except (OSError, UnicodeDecodeError):
        return None
    fm, body = _split_frontmatter(text)
    meta = _parse_frontmatter(fm) if fm is not None else {}

    heading = re.search(r"^#\s+(.+?)\s*$", body, re.M)
    title = heading.group(1) if heading else path.stem.replace("-", " ").replace("_", " ")

    tags = _parse_list(meta["tags"]) if meta.get("tags") else []
    if fm is None and heading:                        # 0.3 article: "tags:" line after the title
        after = body[heading.end():].lstrip("\n").split("\n", 1)[0]
        m = re.match(r"tags:\s*(.+)", after, re.I)
        if m:
            tags = _parse_list(m.group(1))

    memory_type = _unquote(meta.get("type", "")).lower()
    project = _unquote(meta.get("project", "")) or None
    if project:                                       # a relative path would depend on the cwd
        project = os.path.expanduser(project)
        project = os.path.abspath(project) if os.path.isabs(project) else None
    updated = _unquote(meta.get("updated", ""))
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", updated):
            raise ValueError(updated)
        date.fromisoformat(updated)
    except ValueError:                                # missing or not a real date
        updated = date.fromtimestamp(mtime).isoformat()

    rel = path.relative_to(root).as_posix()
    return Memory(
        path=path, rel=rel, category=rel.split("/")[0] if "/" in rel else "",
        title=title, type=memory_type if memory_type in TYPES else "note", tags=tags,
        project=project, updated=updated,
        superseded_by=_unquote(meta.get("superseded_by", "")) or None,
        body=body,
        commit=_unquote(meta.get("commit", "")) or None, legacy=fm is None,
    )


def load_memories(root: Path, unreadable: Optional[list] = None) -> List[Memory]:
    """All memories under root, sorted by id. Skips hidden folders and unreadable files."""
    if not root.is_dir():
        return []
    memories = []
    for path in sorted(root.rglob("*.md")):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        memory = parse_memory(path, root)
        if memory:
            memories.append(memory)
        elif unreadable is not None:
            unreadable.append(path.relative_to(root).as_posix())
    return memories


def _enabled(var: str) -> bool:
    return os.environ.get(var, "").strip().lower() not in ("off", "0", "false", "no")


# ── Agent notes (read-only: Claude Code and Codex write these) ──
AGENT_NAMES = {"claude": "Claude Code", "codex": "Codex"}


def _claude_projects() -> Path:
    return Path.home() / ".claude" / "projects"


def _decode_slug(name: str) -> Optional[str]:
    """Claude Code names a project folder after its path, with separators turned into '-'.
    Rebuild the path from existing folders; None if nothing on disk fits."""
    parts = name.split("-")[1:] if name.startswith("-") else []

    def walk(base: str, rest: List[str], depth: int) -> Optional[str]:
        if not rest:
            return base
        if depth > 40:
            return None
        for n in range(len(rest), 0, -1):                  # longest folder name first
            for joiner in ("-", ".", "_", " "):
                candidate = os.path.join(base, joiner.join(rest[:n]))
                if candidate != base and os.path.isdir(candidate):
                    found = walk(candidate, rest[n:], depth + 1)
                    if found:
                        return found
        return None

    return walk(os.sep, parts, 0) if parts else None


def _transcript_cwd(folder: Path) -> Optional[str]:
    """The working directory recorded in one of the folder's transcripts."""
    try:
        paths = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for path in paths[:3]:
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f):
                    if i > 200:
                        break
                    if '"cwd"' not in line:
                        continue
                    try:
                        cwd = json.loads(line).get("cwd")
                    except (ValueError, AttributeError):
                        continue
                    if isinstance(cwd, str) and cwd:
                        return cwd
        except OSError:
            continue
    return None


def _parse_note(path: Path, source: str, project: Optional[str]) -> Optional[Memory]:
    try:
        text = path.read_text(encoding="utf-8")
        mtime = path.stat().st_mtime
    except (OSError, UnicodeDecodeError):
        return None
    fm, body = _split_frontmatter(text)
    meta = _parse_frontmatter(fm) if fm is not None else {}
    name = _unquote(meta.get("name", ""))
    heading = re.search(r"^#\s+(.+?)\s*$", body, re.M)
    title = (_unquote(meta.get("description", "")) or (heading.group(1) if heading else "")
             or name or path.stem.replace("-", " ").replace("_", " "))
    words = query_terms((name or path.stem).replace("-", " ").replace("_", " "))
    return Memory(path=path, rel=_tilde(path), category=source, title=title[:200].strip(),
                  type="note", tags=words, project=project,
                  updated=date.fromtimestamp(mtime).isoformat(), superseded_by=None,
                  body=body, source=source)


def load_agent_notes() -> List[Memory]:
    """Notes Claude Code (auto memory) and Codex keep. corvid only reads them."""
    if not _enabled("CORVID_NOTES"):
        return []
    notes: List[Memory] = []
    base = _claude_projects()
    try:
        folders = sorted(f for f in base.iterdir() if f.is_dir()) if base.is_dir() else []
    except OSError:
        folders = []
    for folder in folders:
        mem = folder / "memory"
        try:
            files = sorted(p for p in mem.glob("*.md")
                           if p.name != "MEMORY.md" and not p.name.startswith("."))
        except OSError:
            continue
        if not files:
            continue
        project = _transcript_cwd(folder) or _decode_slug(folder.name)
        notes += [n for n in (_parse_note(p, "claude", project) for p in files) if n]
    codex = Path.home() / ".codex" / "memories"
    if codex.is_dir():
        for path in sorted(codex.rglob("*.md")):
            if not any(part.startswith(".") for part in path.relative_to(codex).parts):
                note = _parse_note(path, "codex", None)
                if note:
                    notes.append(note)
    return notes


# ── Search ─────────────────────────────────────────────────────
def query_terms(text: str) -> List[str]:
    """Lowercase words of a query (2+ characters), deduplicated, at most 32. No stop-word list:
    BM25 already gives words found in most memories almost no weight."""
    seen, terms = set(), []
    for token in re.findall(r"[^\W_]+", text.lower()):
        if len(token) >= 2 and token not in seen:
            seen.add(token)
            terms.append(token)
    return terms[:32]


def build_index(memories: List[Memory]) -> sqlite3.Connection:
    """In-memory FTS5 index of the memories. Raises sqlite3.OperationalError without FTS5."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE memories USING fts5("
                 "title, tags, body, tokenize = 'porter unicode61')")
    conn.executemany("INSERT INTO memories(rowid, title, tags, body) VALUES (?, ?, ?, ?)",
                     [(i, c.title, " ".join(c.tags), c.body) for i, c in enumerate(memories)])
    return conn


def best_paragraph(body: str, terms: List[str], max_len: int = 300) -> str:
    """The paragraph sharing the most words with the query."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    text_only = ["\n".join(line for line in p.splitlines()          # no headings or tags lines
                           if not re.match(r"\s*(#+\s|tags:)", line, re.I)) for p in paragraphs]
    paragraphs = [p for p in text_only if p.strip()] or paragraphs
    if not paragraphs:
        return ""
    best = max(paragraphs, key=lambda p: sum(t in p.lower() for t in terms))
    best = re.sub(r"\s+", " ", best)
    return best if len(best) <= max_len else best[:max_len - 1].rstrip() + "…"


def search(memories: List[Memory], query: str, *, cwd: Optional[str] = None,
           tags: Optional[List[str]] = None, limit: int = 10,
           title_tags_only: bool = False,
           include_superseded: bool = True) -> List[Tuple[Memory, float]]:
    """Rank memories for a query. Title matches weigh 10, tags 5, body 1."""
    terms = query_terms(query)
    if not terms or not memories:
        return []
    expr = " OR ".join('"%s"' % t for t in terms)
    if title_tags_only:
        expr = "{title tags} : (%s)" % expr
    conn = build_index(memories)
    try:
        rows = conn.execute("SELECT rowid, -bm25(memories, 10.0, 5.0, 1.0) FROM memories "
                            "WHERE memories MATCH ?", (expr,)).fetchall()
    finally:
        conn.close()
    wanted = [t.lower() for t in (tags or [])]
    hits = []
    for rowid, score in rows:
        memory = memories[rowid]
        if wanted and not all(t in memory.tags for t in wanted):
            continue
        if memory.superseded_by:
            if not include_superseded:
                continue
            score *= 0.5
        if cwd and memory.project and _within(cwd, memory.project):
            score *= 1.5
        hits.append((memory, score))
    hits.sort(key=lambda h: (-h[1], h[0].rel))
    return hits[:limit]


# ── Past sessions (read-only: transcripts Claude Code and Codex already keep) ──
# Transcript formats are internal to each agent. Anything unrecognised is skipped.
HISTORY_VERSION = "3"
HOOK_BUDGET = 0.5                  # seconds a hook may spend updating the cache
CHUNK = 64 * 1024 * 1024           # bytes read per transcript per pass
PROMPT_KEEP = 500                  # characters kept per prompt
HISTORY_MIN_COVER = 0.5            # share of the query's weight one prompt must match

_SECRETS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:[^\s@/]+@"),        # user:password@ in URLs
    re.compile(r"\b(?=[A-Za-z0-9+=_]*\d)(?=[A-Za-z0-9+=_]*[A-Za-z])[A-Za-z0-9+=_]{32,}\b"),
]
_SECRET_VALUE = re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_\-]?key|access[_\-]?key|"
                           r"client[_\-]?secret)(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|\S+)")


def redact(text: str) -> str:
    """Replace things that look like credentials with [redacted]."""
    for pattern in _SECRETS:
        text = pattern.sub("[redacted]", text)
    return _SECRET_VALUE.sub(lambda m: m.group(1) + m.group(2) + "[redacted]", text)


def _clean_prompt(text: str) -> str:
    text = _strip_pasted(text)
    text = re.sub(r"<(private|system-reminder)>.*?(?:</\1>|$)", " ", text, flags=re.S)
    text = re.sub(r"\s+", " ", text).strip()
    return redact(text[:PROMPT_KEEP * 2])[:PROMPT_KEEP].strip()


def history_path() -> Path:
    return corvid_home() / ".cache" / "history.db"


def _transcripts() -> Optional[Dict[str, Tuple[str, Path]]]:
    """Transcript path -> (agent, path). Subagent transcripts are skipped.
    None if the folders couldn't be listed completely."""
    found: Dict[str, Tuple[str, Path]] = {}
    base = _claude_projects()
    try:
        if base.is_dir():
            for folder in base.iterdir():
                if folder.is_dir():
                    for path in folder.glob("*.jsonl"):
                        found[str(path)] = ("claude", path)
        codex = Path.home() / ".codex" / "sessions"
        if codex.is_dir():
            for path in codex.rglob("*.jsonl"):
                found[str(path)] = ("codex", path)
    except OSError:
        return None
    return found


def _day(ts) -> Optional[str]:
    return ts[:10] if isinstance(ts, str) and re.match(r"\d{4}-\d{2}-\d{2}", ts) else None


def _add_prompt(state: dict, ts, text: str) -> None:
    text = _clean_prompt(text or "")
    if text and text not in state["seen"]:
        state["seen"].add(text)
        state["new"].append((_day(ts), text))


def _add_file(state: dict, path) -> None:
    if isinstance(path, str) and path.strip():
        path = path.strip()
        state["files"][path] = state["files"].get(path, 0) + 1


_CLAUDE_EDITS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


# Cheap checks on the raw line, so most lines are never parsed as JSON. They allow spaces
# after the colon, in case an agent stops writing compact JSON.
_CLAUDE_PROMPT = re.compile(rb'"type"\s*:\s*"(?:user|ai-title)"')
_CLAUDE_EDIT = re.compile(rb'"name"\s*:\s*"(?:%s)"' % "|".join(_CLAUDE_EDITS).encode())
_CODEX_OLD_USER = re.compile(rb'"role"\s*:\s*"user"')


def _claude_wanted(line: bytes, state: dict) -> bool:
    if _CLAUDE_PROMPT.search(line):
        return True
    if b'"tool_use"' in line and _CLAUDE_EDIT.search(line):
        return True
    return not state["project"] and b'"cwd"' in line


def _claude_record(d: dict, state: dict) -> None:
    if not state["project"] and isinstance(d.get("cwd"), str):
        state["project"] = d["cwd"]
    kind, ts = d.get("type"), d.get("timestamp")
    if kind == "ai-title" and isinstance(d.get("aiTitle"), str):
        state["title"] = redact(d["aiTitle"].strip())[:200]
        return
    message = d.get("message") if isinstance(d.get("message"), dict) else {}
    content = message.get("content")
    if kind == "user":
        if (d.get("isMeta") or d.get("isCompactSummary") or d.get("isSidechain")
                or d.get("promptSource") == "system"):
            return
        origin = d.get("origin")
        if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
            return
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                            and isinstance(b.get("text"), str))
        else:
            return
        args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
        if args:
            text = args.group(1)
        elif text.lstrip().startswith(("<", "[Request interrupted", "Caveat:")):
            return
        _add_prompt(state, ts, text)
    elif kind == "assistant" and isinstance(content, list):
        for block in content:
            if (isinstance(block, dict) and block.get("type") == "tool_use"
                    and block.get("name") in _CLAUDE_EDITS and isinstance(block.get("input"), dict)):
                _add_file(state, block["input"].get("file_path") or block["input"].get("notebook_path"))


def _codex_wanted(line: bytes, state: dict) -> bool:
    if (b"UserMessage" in line or b'"user_message"' in line or b"FileChange" in line
            or b"*** Add File:" in line or b"*** Update File:" in line):
        return True
    if line.startswith(b'{"type"') and _CODEX_OLD_USER.search(line):
        return True                                       # older top-level format
    return not state["project"] and (b"session_meta" in line or b"turn_context" in line)


def _codex_record(d: dict, state: dict) -> None:
    kind, ts = d.get("type"), d.get("timestamp")
    payload = d.get("payload") if isinstance(d.get("payload"), dict) else {}
    if kind in ("session_meta", "turn_context") and not state["project"]:
        if isinstance(payload.get("cwd"), str):
            state["project"] = payload["cwd"]
    elif kind == "event_msg" and payload.get("type") == "item_completed":
        item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        if item.get("type") == "UserMessage" and isinstance(item.get("content"), list):
            _add_prompt(state, ts, " ".join(
                b.get("text", "") for b in item["content"]
                if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)))
        elif item.get("type") == "FileChange" and isinstance(item.get("changes"), dict):
            for path in item["changes"]:
                _add_file(state, path)
    elif kind == "event_msg" and payload.get("type") == "user_message":
        if isinstance(payload.get("message"), str):
            _add_prompt(state, ts, payload["message"])
    elif kind == "message" and d.get("role") == "user" and isinstance(d.get("content"), list):
        for block in d["content"]:
            text = block.get("text", "") if isinstance(block, dict) else ""
            if not isinstance(text, str):
                continue
            if not state["project"]:
                cwd = re.search(r"<cwd>(.*?)</cwd>", text, re.S)
                if cwd:
                    state["project"] = cwd.group(1).strip()
            if not text.lstrip().startswith("<"):
                _add_prompt(state, ts, text)
    elif kind == "response_item" and payload.get("type") in ("custom_tool_call", "function_call"):
        patch = payload.get("input") or payload.get("arguments") or ""
        if isinstance(patch, str):
            for m in re.finditer(r"\*\*\* (?:Add|Update) File: ([^\n\\]+)", patch):
                _add_file(state, m.group(1))


_PARSERS = {"claude": (_claude_wanted, _claude_record), "codex": (_codex_wanted, _codex_record)}


def _init_history(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
    if row and row[0] == HISTORY_VERSION:
        return
    conn.execute("DROP TABLE IF EXISTS sessions")
    conn.execute("DROP TABLE IF EXISTS prompts")
    conn.execute("CREATE TABLE sessions(path TEXT PRIMARY KEY, agent TEXT, size INTEGER, "
                 "mtime REAL, offset INTEGER, project TEXT, title TEXT, files TEXT, "
                 "first_day TEXT, last_day TEXT, head TEXT)")
    conn.execute("CREATE VIRTUAL TABLE prompts USING fts5("            # kind: prompt or title
                 "text, path UNINDEXED, day UNINDEXED, kind UNINDEXED, "
                 "tokenize = 'porter unicode61')")
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('version', ?)", (HISTORY_VERSION,))
    conn.commit()


def _read_transcript(conn, key: str, agent: str, path: Path, size: int, mtime: float,
                     row, deadline: Optional[float]) -> None:
    wanted, record = _PARSERS[agent]
    with path.open("rb") as f:                # same start: appended to. Otherwise: replaced
        head = hashlib.sha1(f.read(512)).hexdigest()
    offset = row[4] if row and row[4] <= size and row[10] == head else 0
    state = {"project": None, "title": None, "files": {}, "first": None, "last": None,
             "seen": set(), "new": []}
    if row and offset:
        state.update(project=row[5], title=row[6], files=json.loads(row[7] or "{}"),
                     first=row[8], last=row[9])
        state["seen"] = {r[0] for r in conn.execute(
            "SELECT text FROM prompts WHERE path = ? AND kind = 'prompt'", (key,))}
    elif row:
        conn.execute("DELETE FROM prompts WHERE path = ?", (key,))   # shrank or replaced
    fallback = date.fromtimestamp(mtime).isoformat()
    with path.open("rb") as f:
        f.seek(offset)
        while offset < size:
            data = f.read(CHUNK)
            if not data:
                break
            end = data.rfind(b"\n")
            if end < 0:
                if len(data) < CHUNK:
                    break                                 # last line still being written
                offset += len(data)                       # one giant line: skip it
                continue
            for line in data[:end].split(b"\n"):
                if line and wanted(line, state):
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(d, dict):
                        record(d, state)
            offset += end + 1
            if deadline is not None and time.monotonic() > deadline:
                break
    days = [d or fallback for d, _ in state["new"]]
    conn.executemany("INSERT INTO prompts(text, path, day, kind) VALUES (?, ?, ?, 'prompt')",
                     [(t, key, d) for d, (_, t) in zip(days, state["new"])])
    if state["title"] and (not row or state["title"] != row[6]):
        conn.execute("DELETE FROM prompts WHERE path = ? AND kind = 'title'", (key,))
        title_words = state["title"].replace("-", " ").replace("_", " ")
        conn.execute("INSERT INTO prompts(text, path, day, kind) VALUES (?, ?, ?, 'title')",
                     (title_words, key, max(days) if days else row[9] if row else fallback))
    all_days = days + [d for d in (state["first"], state["last"]) if d]
    first, last = (min(all_days), max(all_days)) if all_days else (None, None)
    conn.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (key, agent, size, mtime, offset, state["project"], state["title"],
                  json.dumps(state["files"]), first, last, head))
    conn.commit()


def update_history(conn: sqlite3.Connection, budget: Optional[float]) -> None:
    """Bring the cache up to date with the transcripts on disk, within budget seconds."""
    deadline = None if budget is None else time.monotonic() + budget
    known = {r[0]: r for r in conn.execute("SELECT * FROM sessions")}
    current = _transcripts()
    if current is None:                                   # incomplete listing: change nothing
        return
    gone = [k for k in known if k not in current]
    for key in gone:                                      # the agent deleted it: so do we
        conn.execute("DELETE FROM prompts WHERE path = ?", (key,))
        conn.execute("DELETE FROM sessions WHERE path = ?", (key,))
    if gone:
        conn.commit()
    todo = []
    for key, (agent, path) in current.items():
        try:
            st = path.stat()
        except OSError:
            continue
        row = known.get(key)
        if row and row[4] >= st.st_size and row[3] == st.st_mtime:
            continue
        todo.append((st.st_mtime, key, agent, path, st.st_size))
    for mtime, key, agent, path, size in sorted(todo, reverse=True):     # newest first
        if deadline is not None and time.monotonic() > deadline:
            break
        try:
            _read_transcript(conn, key, agent, path, size, mtime, known.get(key), deadline)
        except OSError:
            continue


def open_history(budget: Optional[float] = HOOK_BUDGET) -> Optional[sqlite3.Connection]:
    """The history cache, updated within budget (None: no limit). None if off or unavailable."""
    if not _enabled("CORVID_HISTORY"):
        return None
    path = history_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=0.2 if budget is not None else 10)
        _init_history(conn)
    except (sqlite3.Error, OSError):
        return None
    try:
        update_history(conn, budget)
    except sqlite3.OperationalError:                      # locked by another session: use as is
        pass
    except sqlite3.Error:
        return None
    return conn


def history_counts(conn: sqlite3.Connection) -> Tuple[int, int]:
    """(sessions with at least one prompt, prompts)."""
    sessions = conn.execute("SELECT COUNT(DISTINCT path) FROM prompts").fetchone()[0]
    prompts = conn.execute("SELECT COUNT(*) FROM prompts WHERE kind = 'prompt'").fetchone()[0]
    return sessions, prompts


def _rel_files(files: Dict[str, int], project: Optional[str], n: int) -> List[str]:
    """Most-edited files, relative to the project. Agents' own plan and memory files skipped."""
    home = str(Path.home())
    agent_dirs = [os.path.join(home, ".claude"), os.path.join(home, ".codex")]
    out = []
    for path, _ in sorted(files.items(), key=lambda kv: (-kv[1], kv[0])):
        if os.path.isabs(path) and any(_within(path, d) for d in agent_dirs):
            continue
        if project and os.path.isabs(path) and _within(path, project):
            path = os.path.relpath(path, project)
        out.append(_tilde(path))
        if len(out) >= n:
            break
    return out


def search_history(conn: sqlite3.Connection, query: str, *, cwd: Optional[str] = None,
                   here: bool = False, others: bool = False, limit: int = 5) -> List[dict]:
    """Best-matching past sessions, one result per session.

    A prompt counts only if it holds the words carrying at least half of the query's weight,
    where a word found in few prompts weighs more than one found in many. Without this, any
    query with an everyday word in it ("next", "write") returned five unrelated sessions."""
    terms = query_terms(query)
    if not terms:
        return []
    total = conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
    found = {t: {r[0] for r in conn.execute(
        "SELECT rowid FROM prompts WHERE prompts MATCH ?", ('"%s"' % t,))} for t in terms}
    weight = {t: math.log(1 + total / max(len(found[t]), 1)) for t in terms}
    needed = HISTORY_MIN_COVER * sum(weight.values())
    expr = " OR ".join('"%s"' % t for t in terms)
    rows = conn.execute("SELECT rowid, text, path, day, kind FROM prompts WHERE prompts MATCH ? "
                        "ORDER BY rank LIMIT 500", (expr,)).fetchall()
    sessions = {}    # one per session: its best-ranked prompt, the title only as a fallback
    for rowid, text, key, day, kind in rows:
        if sum(weight[t] for t in terms if rowid in found[t]) < needed:
            continue
        best = sessions.get(key)
        if best is None or (best[2] == "title" and kind == "prompt"):
            sessions[key] = (text, day, kind)
    info = {}
    if sessions:
        marks = ",".join("?" * len(sessions))
        info = {r[0]: r for r in conn.execute(
            f"SELECT path, agent, project, title, files, first_day, last_day FROM sessions "
            f"WHERE path IN ({marks})", list(sessions))}
    results = []
    for key, (text, day, kind) in sessions.items():
        row = info.get(key)
        if not row:
            continue
        project = row[2]
        inside = bool(cwd and project and _within(cwd, project))
        if (here and not inside) or (others and inside):
            continue
        files = json.loads(row[4] or "{}")
        results.append({
            "agent": row[1], "agent_name": AGENT_NAMES.get(row[1], row[1]),
            "project": project, "title": row[3], "day": day, "first_day": row[5],
            "last_day": row[6], "prompt": text if kind == "prompt" else "",
            "files": _rel_files(files, project, 5), "transcript": key})
        if len(results) >= limit:
            break
    return results


# ── Context for the session-start hook ─────────────────────────
SESSION_CAP = 2000
SESSION_MAX = 15
_LEADS = "Treat memories, notes and past sessions as leads: check them against the current code."
_SAVE_RULE = ('When this session produces a durable decision, finding, or reusable build, '
              'offer once: "Save \'<title>\' to corvid?" When it changes or contradicts what a '
              'memory says, offer to update that memory instead. Write only if the user agrees '
              '(remember skill).')


def _others(notes: List[Memory], cwd: Optional[str]) -> List[Memory]:
    """Notes known to be from another project. The agent already has this project's own,
    and a note with no known project might be one of them: those are found by search only."""
    return [n for n in notes if n.project and not (cwd and _within(cwd, n.project))]


def session_context(memories: List[Memory], cwd: Optional[str], root: Path,
                    notes: Optional[List[Memory]] = None, sessions: int = 0,
                    stray: int = 0) -> str:
    """Shown at session start: this project's memories first, then recent ones."""
    live = [c for c in memories if not c.superseded_by]
    by_date = sorted(live, key=lambda c: (c.updated, c.rel), reverse=True)
    here = [c for c in by_date if cwd and c.project and _within(cwd, c.project)]
    shown = here[:SESSION_MAX]
    shown += [c for c in by_date if c not in here][:SESSION_MAX - len(shown)]

    other = _others(notes or [], cwd)
    reach = []
    if other:
        projects = len({n.project for n in other})
        reach.append(f"{len(other)} notes your agents kept in {projects} other project"
                     + ("s" if projects != 1 else ""))
    if sessions:
        reach.append(f"{sessions} past sessions")
    footer = ['When the user points at earlier work or starts something that may already '
              'exist, read the matching memory or run corvid search "<words>" before exploring '
              'other projects; if it misses, try other words. For where a project stands, '
              'read its [status] memory, then git log since its commit.'
              + (f" Search also covers {' and '.join(reach)}." if reach else ""),
              _LEADS, _SAVE_RULE]
    if stray:
        footer.append(f"{stray} older article{'s' if stray != 1 else ''} in "
                      f"{_tilde(corvid_home() / 'wiki')} {'is' if stray == 1 else 'are'} not being "
                      f"read. Tell the user once, and offer to run corvid install, which moves "
                      f"them into {_tilde(root)}.")
    if not live:
        return "\n".join([f"corvid has no memories yet ({_tilde(root)})."] + footer)

    header = (f"corvid: {_plural(len(live))} in {_tilde(root)}/"
              + (f" ({len(here)} from this project)" if here else "") + ":")
    lines = [f"- [{c.type}] {c.title} — {c.rel}" for c in shown]
    while True:
        more = len(live) - len(lines)
        tail = [f"…and {more} more (corvid list)"] if more > 0 else []
        text = "\n".join([header] + lines + tail + footer)
        if len(text) <= SESSION_CAP or not lines:
            return text
        lines.pop()


def _strip_pasted(text: str) -> str:
    """Drop pasted blocks (logs, files) so they don't drive the search."""
    return re.sub(r"<pasted_content[^>]*>.*?</pasted_content[^>]*>", " ", text, flags=re.S)


# Phrases that point at earlier work without naming it well enough to match.
def _short(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


# ── Agents ─────────────────────────────────────────────────────
# Claude Code: hook payload and output verified 2026-11-11 (tests/fixtures/payloads).
# Codex: follows its documented hooks.json format (SessionStart, cwd on stdin, the same
# hookSpecificOutput JSON, timeout in seconds); a real payload is still to be captured.
AGENTS = {
    "claude": {
        "name": "Claude Code", "dir": ".claude",
        "skill": ".claude/skills/remember/SKILL.md",
        "config": ".claude/settings.json",
        "events": {"session": "SessionStart"},
        "timeout": 5,                                   # seconds
    },
    "codex": {
        "name": "Codex", "dir": ".codex",
        "skill": ".agents/skills/remember/SKILL.md",
        "config": ".codex/hooks.json",
        "events": {"session": "SessionStart"},
        "timeout": 5,                                   # seconds (Codex's default is 600)
    },
}


def cmd_hook(kind: str, agent: str) -> int:
    """Called by the agent. Prints context JSON or nothing. Never fails, always exits 0."""
    try:
        spec = AGENTS.get(agent)
        if not spec or kind not in spec["events"]:
            return 0
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            data = {}
        cwd = str(data.get("cwd") or os.getcwd())
        root = memories_root()
        memories, notes = load_memories(root), load_agent_notes()
        history = open_history(HOOK_BUDGET)
        try:
            sessions = history_counts(history)[0] if history else 0
            text = session_context(memories, cwd, root, notes, sessions,
                                   stray=len(_old_wiki_files()))
        finally:
            if history:
                history.close()
        if text:
            sys.stdout.write(json.dumps({"hookSpecificOutput": {
                "hookEventName": spec["events"][kind], "additionalContext": text}}) + "\n")
    except Exception:
        if os.environ.get("CORVID_DEBUG"):              # otherwise silent: never block the agent
            traceback.print_exc()
    return 0


# ── Install ────────────────────────────────────────────────────
def _hook_command(kind: str, agent: str) -> str:
    return f'"{sys.executable}" -m corvid hook {kind} --agent {agent}'


def _is_corvid(command: str) -> bool:
    return "corvid hook" in command or "corvid_remind" in command


def _strip_corvid_hooks(config: dict) -> None:
    """Remove every corvid hook (0.3 and 0.4 shapes), keep everything else."""
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept = []
        for group in groups:
            if isinstance(group, dict) and _is_corvid(str(group.get("command", ""))):
                continue                                # 0.3: flat handler in the list
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                handlers = [h for h in group["hooks"] if not (
                    isinstance(h, dict) and _is_corvid(str(h.get("command", ""))))]
                if not handlers and group["hooks"]:
                    continue                            # group held only corvid hooks
                group = dict(group, hooks=handlers)
            kept.append(group)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        del config["hooks"]


def _add_corvid_hooks(config: dict, agent: str) -> None:
    spec = AGENTS[agent]
    hooks = config.setdefault("hooks", {})
    for kind, event in spec["events"].items():
        handler = {"type": "command", "command": _hook_command(kind, agent)}
        if spec["timeout"]:
            handler["timeout"] = spec["timeout"]
        hooks.setdefault(event, []).append({"hooks": [handler]})


def _read_config(path: Path) -> dict:
    """Load a JSON config. Raises ValueError if it isn't a JSON object."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return data


def _write_if_changed(path: Path, text: str) -> bool:
    """Write text if it differs. Backs up an existing file once, to <name>.corvid-bak."""
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == text:
        return False
    if old is not None:
        backup = path.with_name(path.name + ".corvid-bak")
        if not backup.exists():
            shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def _update_config(path: Path, agent: Optional[str]) -> bool:
    """Replace corvid hooks in a config (agent=None only removes them)."""
    config = _read_config(path)
    before = json.loads(json.dumps(config))
    _strip_corvid_hooks(config)
    if agent:
        _add_corvid_hooks(config, agent)
    if config == before:                        # nothing to do: don't reformat the file
        return False
    return _write_if_changed(path, json.dumps(config, indent=2) + "\n")


def _remove_owned(path: Path) -> bool:
    """Delete a file only if corvid wrote it. Removes its folder if left empty."""
    try:
        if not path.is_file() or "corvid" not in path.read_text(encoding="utf-8"):
            return False
        path.unlink()
        if not any(path.parent.iterdir()):
            path.parent.rmdir()
        return True
    except (OSError, UnicodeDecodeError):
        return False


def _legacy_cleanup(agent: str) -> List[str]:
    """Remove what corvid 0.3 installed for this agent."""
    home, done = Path.home(), []
    if agent == "claude":
        old_hook = home / ".claude/hooks/corvid_remind.py"
        if old_hook.exists():
            old_hook.unlink()
            done.append("old 0.3 reminder hook")
    if agent == "codex" and _remove_owned(home / ".codex/skills/remember/SKILL.md"):
        done.append("old 0.3 skill in ~/.codex/skills")
    return done


def _pick_agents(names: Optional[List[str]]) -> List[str]:
    if names:
        return names
    return [a for a, spec in AGENTS.items() if (Path.home() / spec["dir"]).is_dir()]


def cmd_install(names: Optional[List[str]], lite: bool = False) -> int:
    agents = _pick_agents(names)
    if not agents:
        print("No supported agent found (looked for ~/.claude and ~/.codex).")
        print("Install Claude Code or Codex first, or run: corvid install --agent claude")
        return 1
    home, ok = Path.home(), 0
    print(f"\ncorvid {__version__}\n")
    for agent in agents:
        spec = AGENTS[agent]
        config = home / spec["config"]
        try:
            _update_config(config, agent)
        except (ValueError, OSError) as e:
            print(f"  ✗ {spec['name']:12} skipped: can't read {_tilde(config)} ({e})")
            print("    Fix that file, then run corvid install again.")
            continue
        _write_if_changed(home / spec["skill"], _SKILL_MD)
        cleaned = _legacy_cleanup(agent)
        ok += 1
        print(f"  ✓ {spec['name']:12} skill + session-start hook")
        if agent == "codex":
            print("    One-time step: open codex, type /hooks, and approve the corvid hook.")
        for item in cleaned:
            print(f"    removed {item}")

    moved = _move_old_wiki()
    root = memories_root()
    root.mkdir(parents=True, exist_ok=True)
    count = len(load_memories(root))
    if moved:
        print(f"\n  {moved}")
    print(f"\n  Your memories: {_tilde(root)} ({_plural(count)})")
    for line in _reach_report():
        print(f"  {line}")
    print(f"\n  {_setup_semantic(lite)}")
    if (corvid_home() / "corvid.db").exists():
        print(f"  {_tilde(corvid_home() / 'corvid.db')} is no longer used. You can delete it.")
    print("\n  Done. Start a new agent session; corvid will show up in it.\n")
    return 0 if ok == len(agents) else 1


def _reach_report() -> List[str]:
    """What corvid can also search, read-only. Builds the history cache on first run."""
    lines = []
    notes = load_agent_notes()
    for source in ("claude", "codex"):
        group = [n for n in notes if n.source == source]
        if group:
            projects = len({n.project for n in group if n.project})
            where = f" in {projects} project" + ("s" if projects != 1 else "") if projects else ""
            lines.append(f"Also searchable, read-only: {len(group)} {AGENT_NAMES[source]} "
                         f"note{'s' if len(group) != 1 else ''}{where}")
    if not _enabled("CORVID_HISTORY"):
        return lines
    if not history_path().exists():
        print("  Reading past sessions for the first time…", flush=True)
    history = open_history(None)
    if history:
        sessions, prompts = history_counts(history)
        history.close()
        if sessions:
            lines.append(f"Also searchable, read-only: {sessions} past sessions ({prompts} prompts), "
                         f"cached in {_tilde(history_path())}")
    if lines:
        lines.append("Turn these off with CORVID_NOTES=off or CORVID_HISTORY=off.")
    return lines


# What `corvid install` adds for search by meaning, unless --lite. Same as the `semantic` extra.
SEMANTIC_PACKAGES = ["onnxruntime", "tokenizers", "huggingface_hub", "numpy"]


_PIP_ERROR = ""                           # the installer's own error line; "" if none could run


def _pip_install(packages: List[str]) -> bool:
    """Install into the Python corvid runs on (its pipx or venv environment): pip, else uv.
    Installer output is kept quiet; on failure _PIP_ERROR holds its error line."""
    global _PIP_ERROR
    errors = []
    for cmd in ([sys.executable, "-m", "pip", "install", "--quiet", *packages],
                ["uv", "pip", "install", "--quiet", "--python", sys.executable, *packages]):
        try:
            done = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        except OSError:                                     # no such installer
            continue
        if done.returncode == 0:
            return True
        lines = [line.strip() for line in done.stderr.splitlines() if line.strip()]
        if any("no module named pip" in line.lower() for line in lines):
            continue                                        # this environment has no pip: try uv
        errors += lines
    marked = [e for e in errors if e.lower().startswith("error") or " error:" in e.lower()]
    _PIP_ERROR = (marked or errors or [""])[0][:200]
    return False


def _install_failure() -> str:
    """Why the libraries didn't install and what to do, from the installer's error line."""
    why, version = _PIP_ERROR, f"{sys.version_info[0]}.{sys.version_info[1]}"
    low = why.lower()
    if not why:
        return ("this environment has neither pip nor uv to install with. Add one, or install "
                "them yourself: pip install \"corvid-remember[semantic]\"")
    if "externally-managed" in low:
        return ("this Python is managed by the system and refuses installs. Install corvid with "
                "pipx or uv tool instead (pipx install corvid-remember)")
    if any(w in low for w in ("could not find a version", "no matching distribution",
                              "no solution found", "wheel")):
        return (f"ONNX Runtime has no build for Python {version} yet. Reinstall corvid on an "
                f"older Python (pipx install --python python3.12 corvid-remember)")
    return why


def _setup_semantic(lite: bool) -> str:
    """Set up search by meaning: the libraries, then the model (downloaded once). Any problem
    leaves corvid on keyword search; install itself still succeeds."""
    retry = "run corvid install again to retry, or corvid install --lite to skip it"
    if lite:
        return "Search: keyword only (--lite). Run corvid install without --lite to add search by meaning."
    if not _enabled("CORVID_SEMANTIC"):
        return "Search: keyword only (CORVID_SEMANTIC=off)."
    if sys.version_info < (3, 10):
        return ("Search: keyword only. Search by meaning needs Python 3.10+; this is "
                f"{sys.version_info[0]}.{sys.version_info[1]}.")
    import importlib
    import importlib.util
    if any(importlib.util.find_spec(m) is None
           for m in ("onnxruntime", "tokenizers", "huggingface_hub", "numpy")):
        print("  Setting up search by meaning: ONNX Runtime and the bekko-embedding model, about "
              "350 MB, once (corvid install --lite skips this)…", flush=True)
        if not _pip_install(SEMANTIC_PACKAGES):
            return (f"Search: keyword only. Search by meaning was not set up: "
                    f"{_install_failure()}. Then {retry}.")
        importlib.invalidate_caches()
    model = _semantic_model()
    if model is None:
        return f"Search: keyword only. The search model didn't load; {retry}."
    try:
        _embed(model, ["corvid"], query=True)
    except Exception:
        return f"Search: keyword only. The search model didn't run; {retry}."
    return f"Search: by meaning ({SEMANTIC_MODEL}), keyword search as fallback."


def cmd_uninstall(names: Optional[List[str]]) -> int:
    home, failed = Path.home(), 0
    for agent in names or list(AGENTS):
        spec = AGENTS[agent]
        try:
            hooks = _update_config(home / spec["config"], None)
        except (ValueError, OSError) as e:
            print(f"  ✗ {spec['name']:12} can't read {_tilde(home / spec['config'])} ({e})")
            failed += 1
            continue
        skill = _remove_owned(home / spec["skill"])
        _legacy_cleanup(agent)
        status = "removed" if (hooks or skill) else "nothing to remove"
        print(f"  ✓ {spec['name']:12} {status}")
    if not names and history_path().exists():
        try:
            history_path().unlink()
            print(f"  ✓ removed the past-sessions cache {_tilde(history_path())}")
        except OSError:
            pass
    print(f"\n  Your memories in {_tilde(memories_root())} were not touched.\n")
    return 1 if failed else 0


# ── Check ──────────────────────────────────────────────────────
def listed_files(memory: Memory) -> List[str]:
    """Paths listed under '## Files', e.g. '- src/app.tsx: what it is'."""
    section = re.search(r"^##\s+Files\s*$(.*?)(?=^##\s|\Z)", memory.body, re.M | re.S)
    if not section:
        return []
    paths = []
    for line in section.group(1).splitlines():
        m = re.match(r"\s*[-*]\s+`?([^\s`:]+)`?", line)
        if m and ("/" in m.group(1) or "." in m.group(1)):
            paths.append(m.group(1))
    return paths


def _git(project: str, *args: str) -> Optional[str]:
    try:
        out = subprocess.run(["git", "-C", project, *args], capture_output=True, text=True,
                             timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def freshness_issues(memory: Memory) -> List[str]:
    """Listed files that are gone, or changed since the memory's commit."""
    if not memory.project or not os.path.isdir(memory.project):
        return []
    files = listed_files(memory)
    issues = []
    missing = [f for f in files if not os.path.exists(os.path.join(memory.project, f))]
    if missing:
        issues.append(f"{memory.rel}: listed files missing in the project: {', '.join(missing)}")
    if memory.commit and _git(memory.project, "rev-parse", "--git-dir") is not None:
        if _git(memory.project, "cat-file", "-e", memory.commit + "^{commit}") is None:
            issues.append(f"{memory.rel}: commit {memory.commit} not found in {_tilde(memory.project)}")
        elif files:
            present = [f for f in files if f not in missing]
            changed = _git(memory.project, "diff", "--name-only", memory.commit, "--", *present) \
                if present else ""
            changed_list = [c for c in (changed or "").split("\n") if c]
            if changed_list:
                issues.append(f"{memory.rel}: changed since {memory.commit}: "
                              f"{', '.join(changed_list)}. Check the memory still holds, "
                              f"then update commit:")
    return issues


def check_issues(memories: List[Memory], root: Path, unreadable: List[str]) -> List[str]:
    issues = [f"{rel}: can't be read as UTF-8 text" for rel in unreadable]
    by_rel = {c.rel: c for c in memories}
    for c in memories:
        if c.legacy:
            issues.append(f"{c.rel}: old format (no frontmatter)")
        if not c.tags:
            issues.append(f"{c.rel}: no tags")
        if c.project and not os.path.isdir(c.project):
            issues.append(f"{c.rel}: project path missing ({_tilde(c.project)})")
        if c.type in ("reuse", "status") and not c.project:
            issues.append(f"{c.rel}: {c.type} memory without a project (use a full or ~/ path)")
        target = by_rel.get(c.superseded_by) if c.superseded_by else None
        if c.superseded_by and not target:
            issues.append(f"{c.rel}: superseded_by points to a missing memory ({c.superseded_by})")
        elif target and target.superseded_by:           # also catches itself and loops
            issues.append(f"{c.rel}: superseded_by points to a memory that is itself "
                          f"replaced ({c.superseded_by})")
        if not c.superseded_by:
            issues += freshness_issues(c)
    stray = _old_wiki_files()
    if stray:
        issues.append(f"{_tilde(corvid_home() / 'wiki')}: {len(stray)} article"
                      f"{'s' if len(stray) != 1 else ''} corvid does not read. Run corvid install "
                      f"to move them; one whose name a memory already has stays: compare the two, "
                      f"ask the user which to keep")
    status: Dict[str, List[str]] = {}
    for c in memories:
        if c.type == "status" and c.project and not c.superseded_by:
            status.setdefault(os.path.realpath(c.project), []).append(c.rel)
    for project, rels in sorted(status.items()):
        if len(rels) > 1:
            issues.append(f"{', '.join(rels)}: more than one status memory for "
                          f"{_tilde(project)}. Merge them into one")
    return issues


def cmd_check() -> int:
    root, unreadable = memories_root(), []
    if not root.is_dir():
        print(f"No memories folder at {_tilde(root)}. Run: corvid install")
        return 1
    memories = load_memories(root, unreadable)
    issues = check_issues(memories, root, unreadable)
    if not issues:
        print(f"✓ {_plural(len(memories))} in {_tilde(root)}, no issues.")
        return 0
    print(f"{_plural(len(memories))} in {_tilde(root)}, {len(issues)} "
          f"issue{'s' if len(issues) != 1 else ''}:\n")
    for issue in issues:
        print(f"  - {issue}")
    print("\nAsk your agent to fix these, for example: \"fix the corvid check issues\".")
    return 1


# ── Search by meaning (optional) ───────────────────────────────
# Set up by `corvid install` (skipped with --lite). Only corvid search uses it, never the hooks.
# bekko-embedding-v1-a25m as ONNX (spec §12.17): no PyTorch, about 230 MB of model files, 100+
# languages, MIT. It ranks reworded queries as well as the much larger models tried, and its
# scores tell "close" from "unrelated". Results below SEMANTIC_MIN_SCORE are dropped, so a search
# can honestly return nothing. The cutoff is the score only 5% of the author's 300 real, unrelated
# prompts reach (eval/cutoff_check.py). It is absolute on purpose: a relative cutoff shifts as the
# memory folder grows (spec §12.12).
SEMANTIC_MODEL = "hotchpotch/bekko-embedding-v1-a25m"
SEMANTIC_FILE = "onnx/model.onnx"         # the author's default export; only its word table is int8
SEMANTIC_MIN_SCORE = 0.434
SEMANTIC_DOC_CHARS = 1500
SEMANTIC_MAX_TOKENS = 512
SEMANTIC_THREADS = 2                      # a search encodes little; keep the CPU cool
SEMANTIC_CACHE_MAX = 20000                # cached section vectors kept, about 15 MB


class _OnnxEmbedder:
    """A text embedding model from an ONNX file and its tokenizer, through ONNX Runtime: no
    PyTorch. Uses the file's `sentence_embedding` output when it has one, else mean-pools
    `last_hidden_state`. Applies the repo's sentence-transformers prompts, if any."""

    def __init__(self, repo: str, file: str):
        import numpy  # noqa: F401  (first: onnxruntime prints an error when numpy is missing)
        import onnxruntime as ort
        from tokenizers import Tokenizer
        config = _hub_file(repo, "config_sentence_transformers.json", required=False)
        self.prompts = {}
        if config:
            try:
                self.prompts = json.loads(Path(config).read_text(encoding="utf-8")).get("prompts") or {}
            except (OSError, ValueError):
                pass
        _hub_file(repo, file + "_data", required=False)          # external weights, if any
        self.tok = Tokenizer.from_file(_hub_file(repo, "tokenizer.json"))
        self.tok.enable_truncation(SEMANTIC_MAX_TOKENS)
        self.tok.enable_padding()
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = SEMANTIC_THREADS
        opts.log_severity_level = 3
        self.sess = ort.InferenceSession(_hub_file(repo, file), opts,
                                         providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.sess.get_inputs()}
        self.outputs = [o.name for o in self.sess.get_outputs()]

    def encode(self, texts, batch_size=16, prompt_name=None, **_):
        import numpy as np
        prefix = self.prompts.get(prompt_name, "") if prompt_name else ""
        out = []
        for i in range(0, len(texts), batch_size):
            enc = self.tok.encode_batch([prefix + t for t in texts[i:i + batch_size]])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feed = {k: v for k, v in (("input_ids", ids), ("attention_mask", mask),
                                      ("token_type_ids", np.zeros_like(ids))) if k in self.inputs}
            if "sentence_embedding" in self.outputs:
                out.append(self.sess.run(["sentence_embedding"], feed)[0])
                continue
            hidden = self.sess.run(None, feed)[0]
            m = mask[..., None].astype(np.float32)
            out.append((hidden * m).sum(1) / np.maximum(m.sum(1), 1e-9))
        return np.concatenate(out) if out else np.zeros((0, 0), dtype=np.float32)


def _hub_file(repo: str, name: str, required: bool = True) -> Optional[str]:
    """A file from the Hugging Face cache, downloaded only if it isn't there yet, so a search
    makes no network call once the model is set up."""
    from huggingface_hub import hf_hub_download
    for local_only in (True, False):
        try:
            return hf_hub_download(repo, name, local_files_only=local_only)
        except Exception:
            continue
    if required:
        raise FileNotFoundError(f"{repo}/{name}")
    return None


def _semantic_model():
    """The embedding model, or None if it isn't installed, can't load, or CORVID_SEMANTIC=off."""
    if not _enabled("CORVID_SEMANTIC"):
        return None
    for var, value in (("TOKENIZERS_PARALLELISM", "false"), ("HF_HUB_DISABLE_PROGRESS_BARS", "1")):
        os.environ.setdefault(var, value)
    try:
        return _OnnxEmbedder(SEMANTIC_MODEL, SEMANTIC_FILE)
    except Exception:                    # libraries missing, download failed, or a broken file
        return None


def _semantic_text(m: Memory) -> str:
    return "\n".join([m.title, " ".join(m.tags), m.body[:SEMANTIC_DOC_CHARS]])


def _semantic_chunks(m: Memory) -> List[str]:
    """A memory as one text per `## ` section, each led by the title and tags. One vector for a
    long memory covering many topics matches none of them well; the best section counts."""
    head = m.title + "\n" + " ".join(m.tags)
    parts = [p.strip() for p in re.split(r"(?m)^(?=## )", m.body) if p.strip()]
    return [head + "\n" + p[:SEMANTIC_DOC_CHARS] for p in parts] or [head]


def _semantic_cache() -> Path:
    return corvid_home() / ".cache" / ("semantic-" + SEMANTIC_MODEL.replace("/", "--") + ".npz")


def _embed(model, texts: List[str], query: bool):
    """Unit-length vectors. Queries get the model's retrieval instruction; documents none."""
    import numpy as np
    prompts = getattr(model, "prompts", None) or {}
    kw = {"prompt_name": "query"} if query and "query" in prompts else {}
    vecs = np.asarray(model.encode(texts, batch_size=16, show_progress_bar=False, **kw),
                      dtype=np.float32).reshape(len(texts), -1)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return vecs / np.where(norms == 0, 1, norms)


def _encode_docs(model, texts: List[str]):
    """One vector per text, cached on disk by content. Disposable, like the history cache."""
    import numpy as np
    keys = [hashlib.sha1(t.encode("utf-8")).hexdigest() for t in texts]
    cached = {}
    path = _semantic_cache()
    try:
        with np.load(path, allow_pickle=False) as f:
            cached = dict(zip(f["keys"].tolist(), f["vecs"]))
    except (OSError, KeyError, ValueError):
        pass
    missing = sorted({(k, t) for k, t in zip(keys, texts) if k not in cached})
    if missing:
        vecs = _embed(model, [t for _, t in missing], query=False)
        cached.update((k, v.astype(np.float16)) for (k, _), v in zip(missing, vecs))
    if missing:
        # Keep every cached vector, not just this call's: memories and notes are encoded in
        # separate calls, and dropping the other group made every search re-encode it. Edited
        # texts leave old vectors behind; past SEMANTIC_CACHE_MAX the oldest entries go.
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            order = list(cached)[-SEMANTIC_CACHE_MAX:]
            tmp = path.with_name(path.name + ".tmp.npz")
            np.savez(tmp, keys=np.array(order), vecs=np.stack([cached[k] for k in order]))
            os.replace(tmp, path)
        except (OSError, ValueError):
            pass
    return np.stack([cached[k] for k in keys]).astype(np.float32)


def semantic_search(model, items: List[Memory], query: str,
                    limit: int = 5) -> List[Tuple[Memory, float]]:
    """Rank by meaning (cosine similarity). Only items scoring at least SEMANTIC_MIN_SCORE come
    back, so an unrelated query returns nothing."""
    if not items or not query.strip():
        return []
    chunks = [_semantic_chunks(m) for m in items]
    docs = _encode_docs(model, [c for cs in chunks for c in cs])
    flat = docs @ _embed(model, [query], query=True)[0]
    scores, at = [], 0
    for cs in chunks:                                   # a memory scores as its best section
        scores.append(float(flat[at:at + len(cs)].max()))
        at += len(cs)
    hits = [(m, s * (0.9 if m.superseded_by else 1.0)) for m, s in zip(items, scores)]
    hits = [h for h in hits if h[1] >= SEMANTIC_MIN_SCORE]
    hits.sort(key=lambda h: (-h[1], h[0].rel))
    return hits[:limit]


# ── Search and list output ─────────────────────────────────────
def _memory_json(memory: Memory) -> dict:
    return {"id": memory.rel, "title": memory.title, "category": memory.category,
            "type": memory.type, "tags": memory.tags, "filepath": str(memory.path),
            "source_project": memory.project,
            "project_exists": bool(memory.project and os.path.isdir(memory.project)),
            "superseded_by": memory.superseded_by, "updated_at": memory.updated}


def _note_json(note: Memory, terms: List[str]) -> dict:
    return {"title": note.title, "agent": note.source, "filepath": str(note.path),
            "source_project": note.project, "updated_at": note.updated,
            "snippet": best_paragraph(note.body, terms)}


def cmd_search(query: str, tags: Optional[List[str]], limit: int, as_json: bool,
               here: bool = False) -> int:
    cwd = os.getcwd()
    memories = load_memories(memories_root())
    if here:
        memories = [m for m in memories if m.project and _within(cwd, m.project)]
    model = _semantic_model()
    try:
        if model:
            wanted = [t.lower() for t in (tags or [])]
            pool = [m for m in memories if all(t in m.tags for t in wanted)]
            hits = semantic_search(model, pool, query, limit=min(limit, 5))
        else:
            hits = search(memories, query, cwd=cwd, tags=tags, limit=limit)
        notes, sessions = [], []
        if not tags:
            pool = load_agent_notes()
            if here:
                pool = [n for n in pool if n.project and _within(cwd, n.project)]
            notes = [n for n, _ in (semantic_search(model, pool, query, limit=min(limit, 5))
                                    if model else search(pool, query, cwd=cwd, limit=min(limit, 5)))]
            if _enabled("CORVID_HISTORY") and not history_path().exists():
                print("corvid: reading past sessions for the first time…", file=sys.stderr)
            history = open_history(None)
            if history:
                sessions = search_history(history, query, cwd=cwd, here=here, limit=min(limit, 5))
                history.close()
    except sqlite3.OperationalError as e:
        print(f"corvid search needs SQLite with FTS5, which this Python lacks ({e}).",
              file=sys.stderr)
        return 1
    terms = query_terms(query)
    if as_json:
        results = []
        for memory, _ in hits:
            item = _memory_json(memory)
            item.update(snippet=best_paragraph(memory.body, terms),
                        match="semantic" if model else "keyword", related=[])
            results.append(item)
        history_json = [{k: h[k] for k in ("agent", "project", "title", "day", "prompt", "files",
                                           "transcript")} for h in sessions]
        print(json.dumps({"query": query, "count": len(results),
                          "semantic_available": bool(model), "results": results,
                          "notes": [_note_json(n, terms) for n in notes],
                          "history": history_json}, indent=2))
        return 0
    if not (hits or notes or sessions):
        print(f"No results for: {query}"
              + (" (nothing close in meaning; try other words)" if model else ""))
        return 0
    if hits:
        print("Memories")
    for memory, _ in hits:
        meta = " · ".join(x for x in (memory.type, memory.updated,
                                       _tilde(memory.project) if memory.project else "") if x)
        print(f"\n[{memory.category or '-'}] {memory.title}  ({meta})")
        if memory.superseded_by:
            print(f"  superseded by {memory.superseded_by}")
        if memory.project and not os.path.isdir(memory.project):
            print("  ⚠ project path missing")
        print(f"  {best_paragraph(memory.body, terms)}")
        print(f"  {_tilde(memory.path)}")
    if notes:
        print(("\n" if hits else "") + "Agent notes (read-only)")
    for note in notes:
        meta = " · ".join(x for x in (AGENT_NAMES[note.source], note.updated,
                                       _tilde(note.project) if note.project else "") if x)
        print(f"\n{note.title}  ({meta})")
        print(f"  {best_paragraph(note.body, terms)}")
        print(f"  {_tilde(note.path)}")
    if sessions:
        print(("\n" if hits or notes else "") + "Past sessions")
    for h in sessions:
        meta = " · ".join(x for x in (h["agent_name"], h["day"] or "",
                                       _tilde(h["project"]) if h["project"] else "") if x)
        print(f"\n{h['title'] or _short(h['prompt'], 80)}  ({meta})")
        if h["prompt"]:
            print(f"  \"{_short(h['prompt'], 200)}\"")
        if h["files"]:
            print(f"  edited: {', '.join(h['files'])}")
        print(f"  transcript: {_tilde(h['transcript'])}")
    print(f"\n{_plural(len(hits))}, {len(notes)} notes, {len(sessions)} past sessions"
          + ("; memories and notes ranked by meaning, closest first, only close matches shown"
             if model else ""))
    return 0


def cmd_list(as_json: bool) -> int:
    root, cwd = memories_root(), os.getcwd()
    memories = load_memories(root)
    here = [c for c in memories if c.project and _within(cwd, c.project)]
    if as_json:
        print(json.dumps([{k: v for k, v in _memory_json(c).items()
                           if k in ("id", "title", "category", "type", "updated_at",
                                    "source_project", "superseded_by")}
                          for c in here + [c for c in memories if c not in here]], indent=2))
        return 0
    if not memories:
        print(f"No memories yet in {_tilde(root)}.")
        if (corvid_home() / "wiki").is_dir():
            print("Found the 0.3 folder ~/corvid/wiki. Run corvid install to move it.")
        print("Save your first one: tell your agent /remember.")
        return 0
    print(f"{_tilde(root)}: {_plural(len(memories))}")
    groups = ([("this project", here)] if here else []) + [
        (cat or "(top level)", [c for c in memories if c.category == cat and c not in here])
        for cat in sorted({c.category for c in memories})]
    for name, group in groups:
        if group:
            print(f"\n{name}/")
            for c in group:
                mark = "  (superseded)" if c.superseded_by else ""
                print(f"  - [{c.type}] {c.title} — {c.rel}{mark}")
    notes = load_agent_notes()
    if notes:
        print(f"\ncorvid search also reads {len(notes)} notes your agents keep (read-only).")
    return 0


# ── Skill (kept identical to skills/remember/SKILL.md; a test checks) ──
_SKILL_MD = '''---
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
'''


# ── CLI ────────────────────────────────────────────────────────
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="corvid", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"corvid {__version__}")
    sub = parser.add_subparsers(dest="command")

    for name in ("install", "uninstall"):
        p = sub.add_parser(name)
        p.add_argument("--agent", action="append", choices=list(AGENTS))
        if name == "install":
            p.add_argument("--lite", action="store_true",
                           help="skip the search model (about 350 MB): keyword search only")
    p = sub.add_parser("search")
    p.add_argument("query", nargs="+")
    p.add_argument("--here", action="store_true", help="only this project, including past sessions")
    p.add_argument("--tags", default="")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("list")
    p.add_argument("--json", action="store_true")
    sub.add_parser("check")
    p = sub.add_parser("hook")                  # internal; must never exit non-zero.
    # Only `hook session` prints anything; `hook prompt` (an earlier per-prompt hook) is a no-op.
    p.add_argument("kind", nargs="?", default="")
    p.add_argument("--agent", default="")

    for name in ("index", "index-all"):        # 0.3 commands, kept as no-ops
        sub.add_parser(name).add_argument("rest", nargs="*")

    if argv is None:
        argv = sys.argv[1:]
    if argv[:1] == ["hook"]:                    # parse hook args leniently: exit 2 blocks prompts
        try:
            args = parser.parse_args(argv)
        except SystemExit:
            return 0
        return cmd_hook(args.kind, args.agent)

    args = parser.parse_args(argv)
    if args.command == "install":
        return cmd_install(args.agent, lite=args.lite)
    if args.command == "uninstall":
        return cmd_uninstall(args.agent)
    if args.command == "search":
        tags = [t.strip().lower() for t in args.tags.split(",") if t.strip()]
        return cmd_search(" ".join(args.query), tags, args.limit, args.json, args.here)
    if args.command == "list":
        return cmd_list(args.json)
    if args.command == "check":
        return cmd_check()
    if args.command in ("index", "index-all"):
        print("corvid 0.4 no longer needs indexing. Memories are read directly from disk.")
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
