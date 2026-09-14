"""Read-only local document and Canvas adapters; raw content stays in private state.

collect_local(root, config) and collect_etl(config, token=None, fetch=None) return
{status, sources, issues}. An injected fetch(url, headers) returns (JSON, headers).
The real transport permits GET only, bounded JSON, and no redirects.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile


# The official https://etl.snu.ac.kr portal links its mobile web here.
DEFAULT_ETL_URL = "https://myetl.snu.ac.kr"
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TEXT = 30_000
MAX_TOTAL_TEXT_BYTES = 3 * 1024 * 1024
MAX_FILES = 500
MAX_PAGES = 15
MAX_API_BYTES = 5 * 1024 * 1024
FOLDERS = {
    "macro": "거시경제이론", "leadership": "공학도의 도전과 리더십",
    "em": "기초전자기학 및 연습", "logic": "논리설계 및 실험",
    "writing": "대글1", "power": "Power System Economics",
}
TEXT_TYPES = {".md", ".txt", ".json"}
OFFICE_TYPES = {".docx", ".pptx", ".hwpx"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def iso(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("Timezone required")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def html_text(value):
    parser = _Text()
    parser.feed(value if isinstance(value, str) else "")
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())


def safe_source_url(value):
    try:
        parts = urllib.parse.urlsplit(value)
        if (parts.scheme == "https" and parts.hostname in
                {"etl.snu.ac.kr", "myetl.snu.ac.kr", "newetl.snu.ac.kr", "oldetl.snu.ac.kr"}
                and not parts.username and not parts.password and parts.port in (None, 443)
                and re.fullmatch(r"/courses/\d+(?:/(?:assignments|quizzes|discussion_topics)/\d+)?/?", parts.path)):
            return "https://" + parts.hostname + parts.path
    except (TypeError, ValueError):
        pass
    return ""


def normalized_source(kind, course, external_id, title, content, updated_at,
                      *, source_url="", due_at=None, local_path="", **metadata):
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    result = {"id": digest([kind, course, str(external_id)]), "kind": kind, "course": course,
              "title": title, "source_url": safe_source_url(source_url), "updated_at": iso(updated_at),
              "content": content[:MAX_TEXT], "local_path": local_path}
    if due_at:
        result["due_at"] = iso(due_at)
    result.update(metadata)
    if len(content) > MAX_TEXT:
        result.update(extraction_status="truncated", needs_review=True)
    # Timestamps alone do not make an unchanged notice a new revision.
    relevant = {key: value for key, value in result.items() if key not in {"id", "updated_at"}}
    result["content_hash"] = digest(relevant)
    return result


def _office_text(path):
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 2000 or sum(entry.file_size for entry in entries) > 40 * 1024 * 1024:
            raise ValueError("archive_limit")
        for entry in entries:
            name = PurePosixPath(entry.filename.replace("\\", "/"))
            if name.is_absolute() or ".." in name.parts:
                raise ValueError("unsafe_archive_path")
        patterns = {".docx": r"word/document\.xml", ".pptx": r"ppt/slides/slide\d+\.xml",
                    ".hwpx": r"Contents/section\d+\.xml"}
        names = [entry.filename for entry in entries if re.fullmatch(patterns[path.suffix.lower()], entry.filename)]
        names.sort(key=lambda value: [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value)])
        parts = []
        for name in names:
            if archive.getinfo(name).file_size > 5 * 1024 * 1024:
                raise ValueError("xml_limit")
            raw = archive.read(name)
            if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                raise ValueError("unsafe_xml")
            element = ET.fromstring(raw)
            for paragraph in element.iter():
                if paragraph.tag.rsplit("}", 1)[-1] == "p":
                    line = "".join(node.text or "" for node in paragraph.iter()
                                   if node.tag.rsplit("}", 1)[-1] == "t")
                    if line.strip():
                        parts.append(line.strip())
        return "\n".join(parts)


def _json_text(value, depth=0):
    if depth > 8:
        raise ValueError("json_depth_limit")
    if isinstance(value, list):
        if len(value) > 1000:
            raise ValueError("json_item_limit")
        return "\n".join(_json_text(item, depth + 1) for item in value)
    if isinstance(value, dict):
        # Exported notice data only: never ingest grade/submission/student lists.
        keys = ("title", "name", "content", "body", "message", "description", "due_at",
                "announcements", "assignments", "quizzes", "items")
        return "\n".join(_json_text(value[key], depth + 1) for key in keys if key in value)
    return value if isinstance(value, str) else ""


def _read_document(path):
    suffix = path.suffix.lower()
    if suffix in TEXT_TYPES:
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp949")
        return _json_text(json.loads(text)) if suffix == ".json" else text
    if suffix in OFFICE_TYPES:
        return _office_text(path)
    if suffix == ".pdf":
        from pypdf import PdfReader  # Optional: missing dependency becomes an explicit review issue.
        # Parser diagnostics may contain original object values; do not expose
        # them in service logs. Extraction failures become generic review issues.
        logger = logging.getLogger("pypdf")
        quiet, previous_propagate = logging.NullHandler(), logger.propagate
        logger.addHandler(quiet)
        logger.propagate = False
        try:
            reader = PdfReader(path)
            if reader.is_encrypted or len(reader.pages) > 150:
                raise ValueError("pdf_encrypted_or_page_limit")
            parts = []
            for page in reader.pages:
                parts.append(page.extract_text() or "")
                if sum(map(len, parts)) > MAX_TEXT:
                    break
            return "\n".join(parts)
        finally:
            logger.removeHandler(quiet)
            logger.propagate = previous_propagate
    raise ValueError("unsupported_format")


def collect_local(root, config):
    """Scan eTL attachment folders and explicitly named notice exports, without OCR."""
    root = Path(root).resolve()
    result = {"status": "ok", "sources": [], "issues": []}
    if not root.is_dir():
        return {"status": "error", "sources": [], "issues": [{"code": "local_root_missing"}]}
    selected = []
    courses = config.get("courses", [])
    for course in courses:
        folder = course.get("folder") or FOLDERS.get(course.get("key"))
        if not isinstance(folder, str) or len(PurePosixPath(folder.replace("\\", "/")).parts) != 1:
            result["issues"].append({"code": "invalid_course_folder", "course": course.get("key")})
            continue
        directory = root / folder
        if not directory.is_dir() or directory.is_symlink() or not directory.resolve().is_relative_to(root):
            result["issues"].append({"code": "course_folder_missing_or_unsafe", "course": course.get("key")})
            continue
        for current, directories, files in os.walk(directory, followlinks=False):
            directories[:] = [name for name in directories if not name.startswith((".", "_source"))
                               and not (Path(current) / name).is_symlink()]
            for name in files:
                path = Path(current) / name
                relative = path.relative_to(root)
                notice = re.search(r"announcement|assignment|quiz|module|schedule|공지|일정", name, re.I)
                if "eTL" in relative.parts or (path.suffix.lower() in TEXT_TYPES and notice):
                    selected.append((path, course["key"], False))
    for path in root.glob("*.md"):
        if "일정" in path.name and "eTL" in path.name:
            selected.append((path, "학기 전체", True))
    selected.sort(key=lambda value: value[0].relative_to(root).as_posix())
    if len(selected) > MAX_FILES:
        result["issues"].append({"code": "file_count_limit", "count": len(selected)})
    seen, remaining_text_bytes = set(), MAX_TOTAL_TEXT_BYTES
    for path, course, summary in selected[:MAX_FILES]:
        relative = path.relative_to(root).as_posix()
        if relative in seen:
            continue
        seen.add(relative)
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            result["issues"].append({"code": "unsafe_local_path", "local_path": relative})
            continue
        try:
            stat = path.stat()
            updated = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
            metadata = {"extraction_status": "summary" if summary else "parsed", "needs_review": summary}
            if stat.st_size > MAX_FILE_BYTES:
                content = ""
                metadata.update(extraction_status="too_large", needs_review=True, size=stat.st_size)
            else:
                metadata["file_hash"] = hashlib.sha256(path.read_bytes()).hexdigest()
                try:
                    content = _read_document(path)
                    if not content.strip():
                        metadata.update(extraction_status="no_text", needs_review=True)
                except Exception:
                    content = ""
                    metadata.update(extraction_status="unsupported" if path.suffix.lower() not in
                                    TEXT_TYPES | OFFICE_TYPES | {".pdf"} else "unreadable", needs_review=True)
            title, due_at = path.name, None
            if path.suffix.lower() == ".json" and metadata["extraction_status"] == "parsed":
                document = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(document, dict):
                    candidate_title = document.get("title") or document.get("name")
                    if isinstance(candidate_title, str) and candidate_title.strip():
                        title = candidate_title
                    try:
                        due_at = iso(document["due_at"]) if document.get("due_at") else None
                    except (AttributeError, ValueError, TypeError):
                        metadata.update(extraction_status="invalid_due_at", needs_review=True)
            encoded = content[:MAX_TEXT].encode("utf-8")
            if len(encoded) > remaining_text_bytes:
                content = encoded[:remaining_text_bytes].decode("utf-8", errors="ignore")
                metadata.update(extraction_status="truncated", needs_review=True)
            remaining_text_bytes -= min(len(encoded), remaining_text_bytes)
            source = normalized_source("local", course, relative, title, content, updated,
                                       local_path=relative, due_at=due_at, **metadata)
            result["sources"].append(source)
            if source["extraction_status"] not in {"parsed", "summary"}:
                result["issues"].append({"code": source["extraction_status"], "source_id": source["id"],
                                         "local_path": relative})
        except (OSError, ValueError, UnicodeError):
            result["issues"].append({"code": "local_read_error", "local_path": relative})
    if result["issues"]:
        result["status"] = "partial"
    return result


class CollectionError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)  # Never include a response body, token or signed URL.


def validated_origin(value):
    url = urllib.parse.urlsplit(value)
    host = url.hostname or ""
    if (url.scheme != "https" or not (host == "snu.ac.kr" or host.endswith(".snu.ac.kr"))
            or url.username or url.password or url.port not in (None, 443)
            or url.path not in ("", "/") or url.query or url.fragment):
        raise CollectionError("invalid_etl_origin")
    return "https://" + host


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CollectionError("redirect_blocked")


def _fetch(url, headers):
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(MAX_API_BYTES + 1)
            if len(raw) > MAX_API_BYTES:
                raise CollectionError("api_size_limit")
            if "text/html" in response.headers.get("Content-Type", "").lower():
                raise CollectionError("auth_required")
            return json.loads(raw), dict(response.headers)
    except urllib.error.HTTPError as error:
        raise CollectionError("auth_required" if error.code in (401, 403) else f"http_{error.code}") from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise CollectionError("api_unavailable") from None


def _pages(origin, path, params, token, fetch):
    url = origin + path + "?" + urllib.parse.urlencode(params, doseq=True)
    base_params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    seen, result = set(), []
    for _ in range(MAX_PAGES):
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parts.query)
        if (validated_origin(parts.scheme + "://" + parts.netloc) != origin or parts.path != path
                or parts.fragment or set(query) - (set(base_params) | {"page"})
                or any(query.get(key) != value for key, value in base_params.items())
                or ("page" in query and (len(query["page"]) != 1 or not query["page"][0].isdigit()))):
            raise CollectionError("unsafe_pagination")
        if url in seen:
            raise CollectionError("pagination_loop")
        seen.add(url)
        payload, headers = fetch(url, {"Authorization": "Bearer " + token, "Accept": "application/json"})
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise CollectionError("invalid_api_shape")
        result.extend(payload)
        if len(result) > 1500:
            raise CollectionError("api_item_limit")
        link = next((value for key, value in headers.items() if key.lower() == "link"), "")
        next_link = re.search(r'<([^>]+)>\s*;\s*rel="?next"?(?:\s|,|;|$)', link)
        if not next_link:
            return result
        url = urllib.parse.urljoin(url, next_link.group(1))
    raise CollectionError("api_page_limit")


def _name(value):
    # Only remove a plain section number, not arbitrary parenthesized course names.
    value = re.sub(r"\s*[([]\s*\d{1,4}\s*[)\]]\s*$", "", str(value))
    return re.sub(r"[\s._·-]+", "", value).casefold()


def _term_matches(item, term):
    start, end = date.fromisoformat(term["start"]), date.fromisoformat(term["end"])
    details = item.get("term") if isinstance(item.get("term"), dict) else {}
    name = str(details.get("name", ""))
    semester = 1 if start.month < 7 else 2
    if re.search(rf"{start.year}\s*(?:년(?:도)?\s*)?[-_/ ]?\s*{semester}\s*(?:학기)?(?:\D|$)", name):
        return True
    if str(start.year) in name and ("spring" if semester == 1 else "fall") in name.lower():
        return True
    for candidate in (details, item):
        try:
            begin = datetime.fromisoformat(candidate["start_at"].replace("Z", "+00:00")).date()
            finish = datetime.fromisoformat(candidate["end_at"].replace("Z", "+00:00")).date()
            if abs((begin - start).days) <= 31 and abs((finish - end).days) <= 45:
                return True
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
    return False


def _api_source(origin, kind, key, course_id, item):
    external = str(item.get("id", ""))
    title = item.get("name") if kind == "etl_assignment" else item.get("title")
    if not external.isdigit() or not isinstance(title, str) or not title.strip():
        raise ValueError("invalid_notice")
    updated = (item.get("updated_at") or item.get("posted_at") or item.get("created_at")
               or datetime.now(timezone.utc).isoformat())
    content = html_text(item.get("message") if kind == "etl_announcement" else item.get("description"))
    route = {"etl_assignment": "assignments", "etl_quiz": "quizzes",
             "etl_announcement": "discussion_topics"}[kind]
    source = normalized_source(kind, key, course_id + ":" + external, title, content, updated,
        source_url=f"{origin}/courses/{course_id}/{route}/{external}",
        due_at=item.get("due_at") if kind != "etl_announcement" else None,
        extraction_status="parsed", needs_review=False, canvas_course_id=course_id,
        related_assignment_id=item.get("assignment_id"),
        publication_state="unpublished" if item.get("published") is False else "visible")
    source["external_id"] = external
    return source


def collect_etl(config, token=None, fetch=None):
    result = {"status": "ok", "sources": [], "issues": [], "courses": []}
    remaining_text_bytes = MAX_TOTAL_TEXT_BYTES
    token = token if token is not None else os.environ.get("ETL_API_TOKEN", "")
    if not token:
        result.update(status="auth_required", issues=[{"code": "auth_required"}])
        return result
    try:
        origin = validated_origin(config.get("etl", {}).get("base_url", DEFAULT_ETL_URL))
        fetch = fetch or _fetch
        catalog = _pages(origin, "/api/v1/courses", {"per_page": 100, "enrollment_type": "student",
                         "enrollment_state": "active", "include[]": "term"}, token, fetch)
        for course in config.get("courses", []):
            aliases = {_name(value) for value in [course["name"], *course.get("aliases", [])]}
            matches = [item for item in catalog if _name(item.get("name", "")) in aliases
                       and _term_matches(item, config["term"])
                       and (course.get("canvas_id") is None or str(item.get("id")) == str(course["canvas_id"]))]
            if len(matches) != 1 or not str(matches[0].get("id", "")).isdigit():
                result["issues"].append({"code": "course_not_found_or_ambiguous", "course": course["key"]})
                continue
            course_id, key = str(matches[0]["id"]), course["key"]
            result["courses"].append({"key": key, "canvas_id": course_id})
            endpoints = [
                ("etl_assignment", f"/api/v1/courses/{course_id}/assignments",
                 {"per_page": 100, "override_assignment_dates": "true"}),
                ("etl_announcement", "/api/v1/announcements", {"per_page": 100,
                 "context_codes[]": "course_" + course_id, "start_date": config["term"]["start"],
                 "end_date": config["term"]["end"]}),
                ("etl_quiz", f"/api/v1/courses/{course_id}/quizzes", {"per_page": 100}),
            ]
            for kind, path, params in endpoints:
                try:
                    items = _pages(origin, path, params, token, fetch)
                    for item in items:
                        if len(result["sources"]) >= MAX_FILES:
                            raise CollectionError("api_source_count_limit")
                        try:
                            source = _api_source(origin, kind, key, course_id, item)
                            encoded = source["content"].encode("utf-8")
                            if len(encoded) > remaining_text_bytes:
                                source.update(content=encoded[:remaining_text_bytes].decode("utf-8", errors="ignore"),
                                              extraction_status="truncated", needs_review=True)
                            remaining_text_bytes -= min(len(encoded), remaining_text_bytes)
                            source["content_hash"] = digest({key: value for key, value in source.items()
                                                             if key not in {"id", "updated_at", "content_hash"}})
                            result["sources"].append(source)
                            if source["extraction_status"] != "parsed":
                                result["issues"].append({"code": source["extraction_status"], "source_id": source["id"]})
                        except (ValueError, TypeError, AttributeError):
                            result["issues"].append({"code": "invalid_notice_fields", "course": key, "kind": kind})
                except CollectionError as error:
                    result["issues"].append({"code": error.code, "course": key, "kind": kind})
                except (ValueError, TypeError, AttributeError):
                    result["issues"].append({"code": "invalid_notice_fields", "course": key, "kind": kind})
    except CollectionError as error:
        result["issues"].append({"code": error.code})
    except (KeyError, TypeError, ValueError, AttributeError):
        result["issues"].append({"code": "invalid_etl_configuration"})
    except Exception:
        result["issues"].append({"code": "api_unavailable"})
    if result["issues"]:
        result["status"] = "auth_required" if any(item["code"] == "auth_required" for item in result["issues"]) else (
            "partial" if result["sources"] else "error")
    return result
