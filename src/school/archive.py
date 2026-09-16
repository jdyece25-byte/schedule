"""Durable, private local school evidence; this module never edits the calendar.

The normalized API text and extracted attachment text are retained by version.
Downloaded attachment bytes remain in the adjacent ``document-cache`` directory.
Run this file directly with --summary, --query, or --source in a later session.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit


SCHEMA = 1
MAX_CONTENT = 200_000
CATEGORIES = {
    "exam": re.compile(r"시험|중간고사|기말고사|퀴즈|\b(?:exam(?:ination)?s?|midterm|final\s+exam|quiz\w*)\b", re.I),
    "deadline": re.compile(r"마감|제출|보고서|과제|\b(?:deadline|due|submit\w*|assignment|lab\s*report)\b", re.I),
    "cancellation": re.compile(r"휴강|보강|취소|수업\s*변경|강의\s*변경|\b(?:cancel\w*|reschedul\w*|make[- ]?up|no\s+class)\b", re.I),
    "preparation": re.compile(r"준비물|지참|예습|프리랩|사전\s*준비|\b(?:bring|prepar\w*|pre[- ]?lab|required\s+materials?)\b", re.I),
}
SECRET = re.compile(r"github_pat_[\w]+|gh[pousr]_[\w]+|\bbearer\s+\S+|[A-Za-z0-9_+/=-]{48,}", re.I)
CREDENTIAL = re.compile(
    r"\b(authorization|access[_ -]?token|api[_ -]?key|password|secret|cookie)\b\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)", re.I)
URL = re.compile(r"https?://[^\s<>\"']+", re.I)
FIELDS = {
    "id", "content_hash", "kind", "course", "title", "content", "updated_at",
    "source_url", "due_at", "local_path", "extraction_status", "needs_review",
    "canvas_course_id", "external_id", "related_assignment_id", "publication_state",
    "file_hash", "raw_content_hash", "extraction_version", "historical_import",
    "filename", "size", "mime_type", "document_format", "content_type",
}
META_FIELDS = ("title", "due_at", "source_url", "publication_state", "extraction_status")


class ArchiveError(RuntimeError):
    """A safe diagnostic code, without source content or credentials."""


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def default_root():
    explicit = os.environ.get("SCHEDULE_SCHOOL_ARCHIVE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    local = os.environ.get("LOCALAPPDATA")
    return (Path(local) if local else Path.home() / ".local" / "share") / "ScheduleSchool" / "knowledge"


def _safe_url(value):
    try:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return "[private link omitted]"
        # Do not persist signed query strings, fragments, credentials, or opaque
        # authorization material embedded in a download URL's path.
        path = SECRET.sub("[redacted]", parts.path)
        authority = parts.hostname
        if parts.port and parts.port not in (80, 443):
            authority += ":" + str(parts.port)
        return urlunsplit((parts.scheme, authority, path, "", ""))
    except (ValueError, TypeError):
        return "[private link omitted]"


def _clean(value, limit=MAX_CONTENT):
    value = value if isinstance(value, str) else ""
    value = re.sub(r"(?im)^[ \t]*(?:authorization|proxy-authorization|cookie|set-cookie)[ \t]*:[^\r\n]*",
                   "[authorization header omitted]", value)
    value = URL.sub(lambda match: _safe_url(match.group()), value)
    value = SECRET.sub("[redacted]", value)
    value = CREDENTIAL.sub(lambda match: match.group(1) + "=[redacted]", value)
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)[:limit]


def _identifier(value):
    # Identifiers are never interpolated into paths. Hashes are allowed here,
    # while arbitrary opaque strings in actual source text are redacted.
    return value if isinstance(value, str) and re.fullmatch(r"[\w.-]{1,150}", value) and not re.match(
        r"(?:github_pat_|gh[pousr]_)", value, re.I) else ""


def _sanitize(source):
    if not isinstance(source, dict) or not _identifier(source.get("id")):
        raise ArchiveError("archive_invalid_source")
    result = {}
    for key in FIELDS:
        value = source.get(key)
        if key in {"id", "content_hash", "file_hash", "raw_content_hash"}:
            if _identifier(value):
                result[key] = value
        elif isinstance(value, str):
            result[key] = _safe_url(value) if key == "source_url" and value else _clean(value)
        elif isinstance(value, (int, float, bool)) or value is None and key in source:
            result[key] = value
    result["content"] = _clean(source.get("content", ""))
    result["title"] = _clean(source.get("title", ""), 3000)
    result["extraction_status"] = _clean(source.get("extraction_status", "parsed"), 100) or "unknown"
    result.setdefault("content_hash", _digest(result))
    if isinstance(source.get("content"), str) and len(source["content"]) > MAX_CONTENT:
        result["extraction_status"] = "truncated"
    local = result.get("local_path", "")
    if isinstance(local, str) and (re.match(r"^[A-Za-z]:", local) or local.startswith(("/", "\\")) or
                                   ".." in local.replace("\\", "/").split("/")):
        result.pop("local_path", None)
    return result


def evidence(source):
    """Categorize literal source lines, not inferred or approved calendar facts."""
    result = []
    content = source.get("content", "")
    title = source.get("title", "")
    for category, pattern in CATEGORIES.items():
        if pattern.search(title) or category == "exam" and source.get("kind") == "etl_quiz":
            result.append({"category": category, "text": title[:600], "field": "title",
                           "source_id": source["id"], "source_hash": source["content_hash"],
                           "incomplete": len(title) > 600 or source.get("extraction_status") != "parsed"})
    for number, line in enumerate(content.splitlines(), 1):
        text = line.strip()
        for category, pattern in CATEGORIES.items():
            if pattern.search(text):
                result.append({"category": category, "text": text[:600],
                               "line_start": number, "line_end": number,
                               "source_id": source["id"], "source_hash": source["content_hash"],
                               "incomplete": len(text) > 600 or source.get("extraction_status") != "parsed"})
    if source.get("due_at"):
        result.append({"category": "deadline", "text": source["due_at"], "field": "due_at",
                       "source_id": source["id"], "source_hash": source["content_hash"],
                       "incomplete": source.get("extraction_status") != "parsed"})
    return result


def source_changes(previous, current):
    """A bounded literal comparison; removed text never means delete an event."""
    old = previous.get("content", "").splitlines() if previous else []
    new = current.get("content", "").splitlines()
    added, removed, total_added, total_removed = [], [], 0, 0
    for operation, i, j, start, end in difflib.SequenceMatcher(None, old, new, autojunk=True).get_opcodes():
        if operation in ("replace", "delete"):
            total_removed += j - i
            removed.extend({"line": n + 1, "text": old[n][:600]} for n in range(i, min(j, i + 24)))
        if operation in ("replace", "insert"):
            total_added += end - start
            added.extend({"line": n + 1, "text": new[n][:600]} for n in range(start, min(end, start + 24)))
    metadata = [{"field": key, "before": previous.get(key), "after": current.get(key)}
                for key in META_FIELDS if previous and previous.get(key) != current.get(key)]
    return {"kind": "updated" if previous else "new", "previous_hash": previous.get("content_hash") if previous else None,
            "current_hash": current["content_hash"], "added": added[:24], "removed": removed[:24],
            "metadata": metadata, "added_count": total_added, "removed_count": total_removed,
            "limited": total_added > 24 or total_removed > 24 or
                       any(len(line) > 600 for line in old + new)}


def _atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".archive-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_json(path, default=None):
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        raise ArchiveError("archive_read_failed") from None


def _coverage(value, depth=0):
    """Keep the adapter's nested category counts/status, excluding credentials."""
    if depth > 5:
        return None
    if isinstance(value, (int, bool)):
        return value
    if isinstance(value, str):
        return _clean(value, 120)
    if isinstance(value, dict):
        result = {}
        for key, child in list(value.items())[:100]:
            if not isinstance(key, str) or re.search(r"auth|cookie|token|secret|password|headers", key, re.I):
                continue
            sanitized = _coverage(child, depth + 1)
            if sanitized is not None:
                result[_clean(key, 100)] = sanitized
        return result
    return None


class SourceArchive:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else default_root()
        self.root = self.root.expanduser().resolve()
        if any((ancestor / ".git").exists() or ancestor.name.lower() == ".git"
               for ancestor in (self.root, *self.root.parents)):
            raise ArchiveError("archive_git_checkout_forbidden")
        self.index_path = self.root / "index.json"

    @contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "archive.lock"
        with path.open("a+b") as handle:
            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            deadline = time.monotonic() + 10
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (OSError, BlockingIOError):
                    if time.monotonic() >= deadline:
                        raise ArchiveError("archive_busy") from None
                    time.sleep(.025)
            try:
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def _index(self):
        result = _read_json(self.index_path, {"version": SCHEMA, "sources": {}, "collectors": {}})
        if not isinstance(result, dict) or not isinstance(result.get("sources"), dict):
            raise ArchiveError("archive_invalid_index")
        return result

    def _version_path(self, source_id, source_hash):
        return self.root / "versions" / _digest(source_id) / (_digest(source_hash) + ".json")

    def _read_version(self, source_id, source_hash):
        return _read_json(self._version_path(source_id, source_hash)) if source_hash else None

    def record(self, source):
        """Save a normalized version, retaining a usable current on failed reads."""
        current = _sanitize(source)
        source_id, source_hash = current["id"], current["content_hash"]
        now = _stamp()
        with self._locked():
            index = self._index()
            entry = index["sources"].get(source_id, {})
            previous_hash = entry.get("current_hash")
            previous = self._read_version(source_id, previous_hash)
            previous_source = previous.get("source", {}) if previous else None
            usable = bool(current.get("content", "").strip()) or current["extraction_status"] == "parsed"
            facts = evidence(current)
            path = self._version_path(source_id, source_hash)
            if not path.exists():
                _atomic_json(path, {"version": SCHEMA, "recorded_at": now, "source": current,
                                    "evidence": facts, "usable": usable})
            # Empty incomplete/failed responses cannot replace the known text,
            # even when a changed timestamp or source hash signals a new edit.
            failed = not usable
            unchanged = source_hash == previous_hash
            changes = entry.get("changes") if unchanged or failed else source_changes(previous_source, current)
            state = "read_failed" if failed else "unchanged" if unchanged else "changed" if previous else "new"
            entry.update({"id": source_id, "course": current.get("course"), "kind": current.get("kind"),
                          "title": (previous_source or current).get("title") if failed else current.get("title"),
                          "last_seen_at": now, "latest_attempt_hash": source_hash,
                          "source_updated_at": current.get("updated_at"),
                          "last_read_status": current["extraction_status"],
                          "first_seen_at": entry.get("first_seen_at", now),
                          "current_hash": previous_hash if failed else source_hash,
                          "changes": changes})
            versions = list(entry.get("versions", []))
            if source_hash not in versions:
                versions.append(source_hash)
            entry["versions"] = versions
            if failed:
                entry["read_failure"] = {"state": current["extraction_status"], "observed_at": now,
                                         "attempt_hash": source_hash, "previous_preserved": bool(previous)}
            else:
                entry.pop("read_failure", None)
                entry["last_success_at"] = now
                if not unchanged:
                    entry["last_changed_at"] = now
            index["sources"][source_id] = entry
            index["updated_at"] = now
            _atomic_json(self.index_path, index)
            return {"state": state, "source_id": source_id, "source_hash": source_hash,
                    "previous_hash": previous_hash, "changes": changes,
                    "evidence": previous.get("evidence", []) if failed and previous else facts,
                    "current_preserved": failed and bool(previous)}

    def record_collection(self, name, result):
        """Track successful/partial attempts and missing IDs without deletions."""
        name = _identifier(name)
        if not name or not isinstance(result, dict):
            raise ArchiveError("archive_invalid_collection")
        now = _stamp()
        with self._locked():
            index = self._index()
            collectors = index.setdefault("collectors", {})
            previous = collectors.get(name, {})
            source_ids = sorted({_identifier(source.get("id")) for source in result.get("sources", [])
                                 if isinstance(source, dict) and _identifier(source.get("id"))})
            known = sorted(set(previous.get("known_source_ids", [])) | set(source_ids))
            issues = []
            for issue in result.get("issues", []):
                if isinstance(issue, dict):
                    issues.append({key: (_identifier(value) if key == "source_id" else _clean(value, 200)) if isinstance(value, str) else value
                                   for key, value in issue.items() if key in {"code", "course", "kind", "source_id", "count"}
                                   and isinstance(value, (str, int, bool))})
            state = _clean(str(result.get("status", "error")), 60)
            coverage = result.get("coverage", {})
            safe_coverage = _coverage(coverage) if isinstance(coverage, dict) else {}
            complete = state == "ok" and not issues and not safe_coverage.get("api_incomplete")
            status = {"state": state, "last_attempt_at": now,
                      "last_success_at": now if complete else previous.get("last_success_at"),
                      "last_usable_at": now if state in ("ok", "partial") else previous.get("last_usable_at"),
                      "source_count": len(source_ids), "issue_count": len(issues), "issues": issues[:100],
                      "coverage": safe_coverage, "known_source_ids": known,
                      "observed_source_ids": source_ids,
                      "missing_source_ids": sorted(set(known) - set(source_ids)),
                      "missing_is_deletion": False}
            collectors[name] = status
            index["updated_at"] = now
            _atomic_json(self.index_path, index)
            return status

    def summary(self):
        index = self._index()
        entries = list(index["sources"].values())
        return {"version": SCHEMA, "updated_at": index.get("updated_at"),
                "source_count": len(entries), "version_count": sum(len(x.get("versions", [])) for x in entries),
                "read_failure_count": sum(bool(x.get("read_failure")) for x in entries),
                "collectors": index.get("collectors", {}),
                "attachment_bytes": "../document-cache", "calendar_modified": False}

    def read(self, source_id, source_hash=None):
        entry = self._index()["sources"].get(source_id)
        if not entry:
            return None
        selected = source_hash or entry.get("current_hash") or entry.get("latest_attempt_hash")
        version = self._read_version(source_id, selected)
        return {"entry": entry, "version": version} if version else None

    def query(self, text="", *, course=None, category=None, limit=20):
        if category is not None and category not in CATEGORIES:
            raise ArchiveError("archive_invalid_category")
        entries = list(self._index()["sources"].values())
        entries.sort(key=lambda entry: str(entry.get("last_changed_at", "")), reverse=True)
        words = [word.casefold() for word in str(text).split()]
        matches = []
        for entry in entries:
            if course and entry.get("course") != course:
                continue
            record = self._read_version(entry["id"], entry.get("current_hash") or entry.get("latest_attempt_hash"))
            if not record:
                continue
            source = record["source"]
            haystack = (source.get("title", "") + "\n" + source.get("content", "")).casefold()
            if words and not all(word in haystack for word in words):
                continue
            facts = record.get("evidence", [])
            if category:
                facts = [fact for fact in facts if fact.get("category") == category]
                if not facts:
                    continue
            matches.append({"id": entry["id"], "source_hash": source["content_hash"],
                            "course": source.get("course"), "title": source.get("title"),
                            "source_url": source.get("source_url"), "updated_at": source.get("updated_at"),
                            "extraction_status": source.get("extraction_status"),
                            "read_failure": entry.get("read_failure"),
                            "evidence": facts[:80], "evidence_limited": len(facts) > 80,
                            "changes": entry.get("changes")})
            if len(matches) >= max(1, min(int(limit), 200)):
                break
        return matches


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--query", default="")
    parser.add_argument("--course")
    parser.add_argument("--category", choices=tuple(CATEGORIES))
    parser.add_argument("--source")
    parser.add_argument("--hash")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON (default).")
    args = parser.parse_args(argv)
    try:
        archive = SourceArchive(args.root)
        result = archive.read(args.source, args.hash) if args.source else (
            archive.query(args.query, course=args.course, category=args.category, limit=args.limit)
            if args.query or args.course or args.category else archive.summary())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ArchiveError, OSError):
        print(json.dumps({"error": "archive_unavailable"}))
        return 1


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
