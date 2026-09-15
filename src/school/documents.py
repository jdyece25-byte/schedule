"""Bounded course document collection. Tokens and download links never enter sources.

Canvas metadata is rechecked on every pass. File bytes and extracted text are
cached privately by identity and revision, with a bounded download budget.
"""
from __future__ import annotations

from datetime import datetime, timezone
from collections import Counter
from html.parser import HTMLParser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib

from .sources import (CollectionError, MAX_FILE_BYTES, MAX_TEXT, OFFICE_TYPES, TEXT_TYPES,
                      _pages, _read_document, _source_terms, digest, html_text, normalized_source)

EXTRACTOR_VERSION = 5
MAX_DOWNLOAD_BYTES = 48 * 1024 * 1024
MAX_DOWNLOADS = 32
MAX_COURSE_DOCUMENTS = 150
MAX_HTML_BYTES = 2 * 1024 * 1024
SUPPORTED = TEXT_TYPES | OFFICE_TYPES | {'.pdf', '.hwp', '.zip'}
RETRY_STATES = {'unreadable', 'download_failed', 'download_budget', 'syllabus_unavailable'}


class Links(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.links = base, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        value = attrs.get('src' if tag == 'iframe' else 'href')
        if tag in ('a', 'iframe') and isinstance(value, str) and len(value) <= 8192:
            self.links.append((tag, urllib.parse.urljoin(self.base, value)))


def links(value, base):
    parser = Links(base)
    parser.feed(value if isinstance(value, str) else '')
    return parser.links[:300]


def _valid_url(url):
    try:
        value = urllib.parse.urlsplit(url)
        if (value.scheme != 'https' or not value.hostname or value.username or value.password
                or value.port not in (None, 443) or len(url) > 8192
                or any(ord(c) < 32 or ord(c) == 127 for c in url)):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise CollectionError('unsafe_document_url') from None


def _dropbox(host):
    return host in {'dropbox.com', 'www.dropbox.com', 'dl.dropboxusercontent.com'} or host.endswith('.dl.dropboxusercontent.com')


def _allowed(url, mode, origin):
    value = _valid_url(url)
    if mode == 'dropbox':
        allowed = _dropbox(value.hostname)
    elif mode == 'academic':
        allowed = value.hostname == 'sugang.snu.ac.kr' and value.path == '/sugang/cc/cc103.action'
    else:
        allowed = (value.hostname == 'snu.ac.kr' or value.hostname.endswith('.snu.ac.kr')
                   or (value.hostname == 'kr.object.gov-ncloudstorage.com'
                       and value.path.startswith('/snu-canvas-contents/account_1/')))
    if not allowed:
        raise CollectionError('unsafe_document_redirect')
    return value


class _InspectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _binary_get(url, headers, limit):
    request = urllib.request.Request(url, headers=headers, method='GET')
    try:
        response = urllib.request.build_opener(_InspectRedirect()).open(request, timeout=20)
    except urllib.error.HTTPError as error:
        return error.code, b'', dict(error.headers)
    except (urllib.error.URLError, TimeoutError):
        raise CollectionError('document_download_failed') from None
    with response:
        declared = response.headers.get('Content-Length')
        if declared and declared.isdigit() and int(declared) > limit:
            raise CollectionError('document_size_limit')
        raw = response.read(limit + 1)
        if len(raw) > limit:
            raise CollectionError('document_size_limit')
        return response.status, raw, dict(response.headers)


def download(url, origin, token, *, mode='snu', limit=MAX_FILE_BYTES, transport=None):
    transport = transport or _binary_get
    value = _allowed(url, mode, origin)
    if mode == 'dropbox' and value.hostname in {'dropbox.com', 'www.dropbox.com'}:
        query = dict(urllib.parse.parse_qsl(value.query))
        query['dl'] = '1'
        url = urllib.parse.urlunsplit((value.scheme, value.netloc, value.path, urllib.parse.urlencode(query), ''))
    seen = set()
    for _ in range(6):
        value = _allowed(url, mode, origin)
        if url in seen:
            raise CollectionError('document_redirect_loop')
        seen.add(url)
        headers = {'Accept': 'application/octet-stream', 'User-Agent': 'ScheduleSchool/1'}
        # Academic pages and external educational documents NEVER get eTL auth.
        if mode == 'snu' and token and value.scheme + '://' + value.netloc == origin:
            headers['Authorization'] = 'Bearer ' + token
        status, raw, response_headers = transport(url, headers, limit)
        if status in (301, 302, 303, 307, 308):
            location = next((v for k, v in response_headers.items() if k.lower() == 'location'), '')
            if not location:
                raise CollectionError('document_download_failed')
            url = urllib.parse.urljoin(url, location)
            continue
        if status != 200:
            raise CollectionError('document_http_' + str(status))
        if not isinstance(raw, bytes) or len(raw) > limit:
            raise CollectionError('document_size_limit')
        if mode == 'dropbox' and not raw.startswith(b'%PDF-'):
            raise CollectionError('document_not_pdf')
        return raw
    raise CollectionError('document_redirect_limit')


def _hwp_paragraph(raw):
    if len(raw) % 2:
        raise ValueError('invalid_hwp_text')
    units = struct.unpack('<' + 'H' * (len(raw) // 2), raw)
    plain, pos = [], 0
    # HWP 5.0 control records use eight UTF-16 code units for inline/extended
    # controls. Never decode their opaque payload as visible text.
    wide = {1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23}
    while pos < len(units):
        code = units[pos]
        if code in wide:
            if pos + 8 > len(units):
                raise ValueError('invalid_hwp_control')
            if code == 9:
                plain.append('\t')
            pos += 8
        else:
            if code in (10, 13): plain.append('\n')
            elif code >= 32: plain.append(chr(code))
            pos += 1
    return ''.join(plain)


def hwp_text(path):
    import olefile
    with olefile.OleFileIO(path) as document:
        header = document.openstream('FileHeader').read(256)
        if len(header) < 40 or not header.startswith(b'HWP Document File') or header[35] != 5:
            raise ValueError('unsupported_hwp_version')
        flags = struct.unpack_from('<I', header, 36)[0]
        if flags & 6:
            raise ValueError('protected_hwp')
        sections = sorted((p for p in document.listdir() if len(p) == 2 and p[0] == 'BodyText'
                           and re.fullmatch(r'Section\d+', p[1])), key=lambda p: int(p[1][7:]))
        if not sections or len(sections) > 150:
            raise ValueError('hwp_section_limit')
        parts, expanded = [], 0
        for section in sections:
            raw = document.openstream(section).read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES: raise ValueError('hwp_stream_limit')
            if flags & 1:
                decoder = zlib.decompressobj(-15)
                raw = decoder.decompress(raw, 40 * 1024 * 1024 + 1)
                if not decoder.eof or decoder.unconsumed_tail: raise ValueError('hwp_expansion_limit')
            expanded += len(raw)
            if expanded > 40 * 1024 * 1024: raise ValueError('hwp_expansion_limit')
            pos = 0
            while pos < len(raw):
                if pos + 4 > len(raw): raise ValueError('hwp_record_limit')
                record = struct.unpack_from('<I', raw, pos)[0]; pos += 4
                size, tag = record >> 20, record & 0x3ff
                if size == 0xfff:
                    if pos + 4 > len(raw): raise ValueError('hwp_record_limit')
                    size = struct.unpack_from('<I', raw, pos)[0]; pos += 4
                if pos + size > len(raw): raise ValueError('hwp_record_limit')
                if tag == 67:
                    parts.append(_hwp_paragraph(raw[pos:pos + size]))
                pos += size
            if sum(map(len, parts)) > MAX_TEXT: break
        return '\n'.join(parts)


def private_cache_root():
    configured = os.environ.get('SCHEDULE_SCHOOL_CACHE_DIR')
    if configured: return Path(configured).expanduser().resolve()
    if os.environ.get('LOCALAPPDATA'): return Path(os.environ['LOCALAPPDATA']) / 'ScheduleSchool' / 'document-cache'
    return Path(os.environ.get('RUNNER_TEMP') or os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'schedule-school-documents'


class DocumentCache:
    def __init__(self, root=None, *, transport=None):
        self.root = Path(root) if root is not None else private_cache_root()
        self.transport, self.bytes, self.downloads = transport, 0, 0
        self.hits, self.misses = 0, 0
        self.root.mkdir(parents=True, exist_ok=True)
        baseline = self.root / 'baseline.json'
        try: self.baseline = float(json.loads(baseline.read_text(encoding='utf-8'))['first_collection'])
        except (OSError, ValueError, KeyError, TypeError):
            self.baseline = time.time()
            baseline.write_text(json.dumps({'first_collection': self.baseline}), encoding='utf-8')

    def read(self, identity, revision, filename, url, origin, token, *, mode='snu', size=0):
        extension = Path(urllib.parse.unquote(filename)).suffix.lower()
        if mode == 'academic': extension = '.html'
        if mode == 'dropbox': extension = '.pdf'
        key = digest(identity); record = self.root / (key + '.json')
        source_revision = revision
        metadata_revision = digest(source_revision)
        revision = digest([EXTRACTOR_VERSION, source_revision])
        previous_hash = None
        reusable = None
        if record.is_file() and not record.is_symlink() and record.stat().st_size <= MAX_TEXT * 8:
            try:
                cached = json.loads(record.read_text(encoding='utf-8'))
                previous_hash = cached.get('result', {}).get('file_hash')
                age = time.time() - cached.get('checked_at', 0)
                if cached.get('revision') == revision and (cached['result']['status'] not in RETRY_STATES or age < 900):
                    self.hits += 1
                    return dict(cached['result'])
                same_metadata = cached.get('metadata_revision') == metadata_revision or any(
                    cached.get('revision') == digest([version, source_revision]) for version in (3, 4))
                raw_path = self.root / (key + extension)
                if (same_metadata and previous_hash and raw_path.is_file() and not raw_path.is_symlink()
                        and raw_path.stat().st_size <= MAX_FILE_BYTES):
                    raw = raw_path.read_bytes()
                    if hashlib.sha256(raw).hexdigest() == previous_hash:
                        reusable = raw
            except (OSError, ValueError, KeyError, TypeError):
                pass
        self.misses += 1
        result = {'content': '', 'status': 'parsed'}
        if extension not in SUPPORTED | {'.html'}:
            result['status'] = 'unsupported'
        elif isinstance(size, int) and size > MAX_FILE_BYTES:
            result['status'] = 'too_large'
        elif reusable is None and (self.downloads >= MAX_DOWNLOADS or self.bytes + max(size or 0, 1) > MAX_DOWNLOAD_BYTES):
            result['status'] = 'download_budget'
            return result
        else:
            try:
                if reusable is None:
                    self.downloads += 1
                    raw = download(url, origin, token, mode=mode,
                                   limit=min(MAX_HTML_BYTES if mode == 'academic' else MAX_FILE_BYTES, MAX_DOWNLOAD_BYTES - self.bytes),
                                   transport=self.transport)
                    self.bytes += len(raw)
                else:
                    raw = reusable
                    self.hits += 1
                self.root.mkdir(parents=True, exist_ok=True)
                target = self.root / (key + extension)
                temporary = self.root / (key + '.' + uuid.uuid4().hex + '.tmp')
                temporary.write_bytes(raw); temporary.replace(target)
                result['file_hash'] = hashlib.sha256(raw).hexdigest()
                if previous_hash == result['file_hash']:
                    result['extraction_upgrade'] = True
                if extension == '.html':
                    try: text = raw.decode('utf-8')
                    except UnicodeDecodeError: text = raw.decode('cp949')
                    content = html_text(text)
                    if len(content.strip()) < 80: result['status'] = 'syllabus_unavailable'
                else:
                    content = _read_document(target)
                result['content'] = content[:MAX_TEXT]
                if not content.strip(): result['status'] = 'no_text'
                elif len(content) > MAX_TEXT: result['status'] = 'truncated'
                elif '[UNREADABLE ' in content: result['status'] = 'partial_document'
            except CollectionError as error:
                result['status'] = 'too_large' if error.code == 'document_size_limit' else 'download_failed'
                result['error'] = error.code
            except Exception:
                result['status'] = 'unreadable'
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / (key + '.' + uuid.uuid4().hex + '.tmp')
        temporary.write_text(json.dumps({'revision': revision, 'metadata_revision': metadata_revision,
                                        'checked_at': time.time(), 'result': result}, ensure_ascii=False), encoding='utf-8')
        temporary.replace(record)
        return result


def collect_documents(origin, course, course_id, token, fetch, cache, *, references=(), term=None):
    """Collect syllabus, pages, modules and course files with explicit coverage."""
    sources, issues, coverage, seen, file_ids, external = [], [], {}, set(), set(), []
    before = (cache.bytes, cache.downloads, cache.hits)
    base = f'{origin}/courses/{course_id}'
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}
    fallback_time = '1970-01-01T00:00:00Z'  # Missing metadata cannot churn every poll.

    def emit(kind, identifier, title, content='', *, status='parsed', source_url=base, updated=None, **metadata):
        if term and term.get('start'):
            begin = datetime.fromisoformat(term['start'])
            expected = (begin.year, 1 if begin.month < 7 else 2)
            if _source_terms(title) - {expected}:
                metadata['term_conflict'] = True
                if status == 'parsed': status = 'prior_term'
        try: historical = datetime.fromisoformat((updated or fallback_time).replace('Z', '+00:00')).timestamp() <= cache.baseline
        except (ValueError, TypeError, AttributeError): historical = True
        source = normalized_source(kind, course, f'{course_id}:{identifier}', str(title or '학교 자료'), content,
                                   updated or fallback_time, source_url=source_url, extraction_status=status,
                                   needs_review=status != 'parsed', canvas_course_id=str(course_id),
                                   historical_import=historical, extraction_version=EXTRACTOR_VERSION,
                                   raw_content_hash=metadata.get('file_hash') or hashlib.sha256(content.encode('utf-8')).hexdigest(), **metadata)
        source['external_id'] = str(identifier)
        source['content_hash'] = digest({k: v for k, v in source.items() if k not in
            {'id', 'updated_at', 'content_hash', 'historical_import', 'extraction_upgrade', 'extraction_version'}})
        if source['id'] in seen: return
        seen.add(source['id']); sources.append(source)
        if source['extraction_status'] != 'parsed':
            issues.append({'code': source['extraction_status'], 'source_id': source['id'], 'course': course, 'kind': kind})

    def listing(category, path, params):
        try:
            values = _pages(origin, path, params, token, fetch)
            coverage[category] = {'status': 'ok', 'count': len(values)}
            return values
        except CollectionError as error:
            coverage[category] = {'status': 'unavailable', 'code': error.code}
            issues.append({'code': error.code, 'course': course, 'kind': 'etl_' + category})
            return []
        except Exception:
            coverage[category] = {'status': 'error', 'code': 'document_metadata_failed'}
            issues.append({'code': 'document_metadata_failed', 'course': course, 'kind': 'etl_' + category})
            return []

    def inspect_links(markup, parent_url, parent_title, updated):
        for tag, url in links(markup, base + '/'):
            try: value = _valid_url(url)
            except CollectionError: continue
            match = re.fullmatch(r'(?:/api/v1)?(?:/courses/' + re.escape(str(course_id)) + r')?/files/(\d+)(?:/download)?/?', value.path)
            if value.scheme + '://' + value.netloc == origin and match:
                file_ids.add(match[1])
            elif tag == 'a' and value.path.lower().endswith('.pdf'):
                external.append((url, parent_url, parent_title, updated))

    for reference in references:
        item = reference['item']; kind = reference['kind']
        markup = item.get('message') if kind == 'etl_announcement' else item.get('description')
        inspect_links(markup, base + '/' + ('discussion_topics' if kind == 'etl_announcement' else 'assignments' if kind == 'etl_assignment' else 'quizzes') + '/' + str(item.get('id')),
                      item.get('title') or item.get('name'), item.get('updated_at') or item.get('posted_at'))
        for attachment in item.get('attachments', []) if isinstance(item.get('attachments'), list) else []:
            if isinstance(attachment, dict) and str(attachment.get('id', '')).isdigit(): file_ids.add(str(attachment['id']))

    try:
        item, _ = fetch(origin + f'/api/v1/courses/{course_id}?include[]=syllabus_body', headers)
        if not isinstance(item, dict): raise CollectionError('invalid_api_shape')
        markup = item.get('syllabus_body') or ''
        content = html_text(markup)
        frames = [(tag, url) for tag, url in links(markup, base + '/') if tag == 'iframe']
        status = 'parsed' if content.strip() else 'empty'
        if frames:
            sections = []
            for _, url in frames[:3]:
                value = _valid_url(url)
                if value.hostname == 'sugang.snu.ac.kr' and value.path == '/sugang/cc/cc103.action':
                    # Academic endpoint has no revision metadata: refresh daily.
                    result = cache.read(['academic', course_id, digest(url)], datetime.now(timezone.utc).date().isoformat(),
                                        'syllabus.html', url, origin, '', mode='academic')
                    sections.append(result['content']); status = result['status']
                else: status = 'syllabus_unavailable'
            content = '\n\n'.join(sections)
        inspect_links(markup, base + '/assignments/syllabus', '강의계획서', item.get('updated_at'))
        if content.strip() or frames:
            emit('etl_syllabus', 'syllabus', '강의계획서', content, status=status,
                 source_url=base + '/assignments/syllabus', updated=item.get('updated_at'))
        coverage['syllabus'] = {'status': status, 'count': int(bool(content.strip() or frames)), 'iframes': len(frames)}
    except Exception as error:
        code = error.code if isinstance(error, CollectionError) else 'syllabus_unavailable'
        coverage['syllabus'] = {'status': 'unavailable', 'code': code}
        emit('etl_syllabus', 'syllabus', '강의계획서', status='syllabus_unavailable')

    pages = listing('pages', f'/api/v1/courses/{course_id}/pages', {'per_page': 100})
    page_refs = {str(p.get('page_id')): p for p in pages if isinstance(p.get('page_id'), int)}
    modules = listing('modules', f'/api/v1/courses/{course_id}/modules', {'per_page': 100, 'include[]': 'items'})
    for module in modules[:MAX_COURSE_DOCUMENTS]:
        if module.get('published') is False or not str(module.get('id', '')).isdigit(): continue
        mid = str(module['id']); items = module.get('items')
        if not isinstance(items, list) or len(items) < module.get('items_count', 0):
            items = listing('module_' + mid, f'/api/v1/courses/{course_id}/modules/{mid}/items', {'per_page': 100})
        for item in items[:MAX_COURSE_DOCUMENTS]:
            if not isinstance(item, dict) or item.get('published') is False: continue
            kind, identifier = item.get('type'), str(item.get('id', ''))
            if kind == 'File' and str(item.get('content_id', '')).isdigit(): file_ids.add(str(item['content_id']))
            elif kind == 'Page' and isinstance(item.get('page_url'), str): page_refs[item['page_url']] = item
            elif kind == 'ExternalUrl':
                url = item.get('external_url', '')
                try:
                    value = _valid_url(url)
                    if value.path.lower().endswith('.pdf'): external.append((url, base + '/modules/items/' + identifier, item.get('title'), item.get('updated_at')))
                    else: emit('etl_module', identifier, item.get('title'), status='external_reference', source_url=base + '/modules/items/' + identifier)
                except CollectionError: emit('etl_module', identifier, item.get('title'), status='external_reference')
            elif kind == 'ExternalTool':
                emit('etl_module', identifier, item.get('title'), status='external_tool', source_url=base + '/modules/items/' + identifier)
            elif kind == 'SubHeader':
                emit('etl_module', identifier, item.get('title'), str(item.get('title') or ''), source_url=base + '/modules/items/' + identifier)
    seen_pages = set()
    for reference, page in list(page_refs.items())[:MAX_COURSE_DOCUMENTS]:
        if page.get('published') is False: continue
        try:
            slug = str(page.get('page_id') or page.get('page_url') or page.get('url') or reference)
            if len(slug) > 300 or '/' in slug or slug in ('.', '..'): raise CollectionError('unsafe_page_reference')
            item, _ = fetch(origin + f'/api/v1/courses/{course_id}/pages/' + urllib.parse.quote(slug, safe=''), headers)
            if not isinstance(item, dict) or item.get('published') is False: continue
            pid = str(item.get('page_id') or slug)
            if pid in seen_pages: continue
            seen_pages.add(pid)
            content = html_text(item.get('body'))
            url = base + '/pages/' + urllib.parse.quote(str(item.get('url') or slug), safe='')
            emit('etl_page', pid, item.get('title'), content, status='parsed' if content else 'empty', source_url=url, updated=item.get('updated_at'))
            inspect_links(item.get('body'), url, item.get('title'), item.get('updated_at'))
        except Exception:
            emit('etl_page', str(reference), page.get('title'), status='unreadable')

    files = listing('files', f'/api/v1/courses/{course_id}/files', {'per_page': 100})
    by_id = {str(item['id']): item for item in files if str(item.get('id', '')).isdigit()}
    for identifier in sorted(file_ids - set(by_id))[:MAX_COURSE_DOCUMENTS]:
        try:
            item, _ = fetch(origin + f'/api/v1/courses/{course_id}/files/{identifier}', headers)
            if not isinstance(item, dict) or str(item.get('id')) != identifier: raise ValueError()
            by_id[identifier] = item
        except Exception: emit('etl_file', identifier, '첨부 자료', status='unreadable', source_url=base + '/files/' + identifier)
    # Prioritize compact notices/OT/schedules; large lecture files continue on
    # subsequent passes using cache hits instead of downloading them again.
    ordered = sorted(by_id.items(), key=lambda pair: (not bool(re.search(r'intro|syllabus|schedule|계획|일정|안내|OT', str(pair[1].get('display_name', '')), re.I)), pair[1].get('size') or 0, pair[0]))
    if len(ordered) > MAX_COURSE_DOCUMENTS: issues.append({'code': 'document_count_limit', 'course': course})
    for identifier, item in ordered[:MAX_COURSE_DOCUMENTS]:
        filename = item.get('display_name') or urllib.parse.unquote(item.get('filename') or '첨부 자료')
        if any(item.get(key) is True for key in ('locked_for_user', 'hidden_for_user', 'locked', 'hidden')):
            emit('etl_file', identifier, filename, status='restricted', source_url=base + '/files/' + identifier)
            continue
        revision = [item.get('updated_at'), item.get('modified_at'), item.get('size'), filename]
        download_url = item.get('url')
        try:
            value = _valid_url(download_url)
            if value.scheme + '://' + value.netloc != origin or value.path not in (
                    f'/files/{identifier}/download', f'/courses/{course_id}/files/{identifier}/download'):
                raise CollectionError('unsafe_file_download')
        except CollectionError:
            emit('etl_file', identifier, filename, status='unreadable', source_url=base + '/files/' + identifier)
            continue
        result = cache.read(['canvas', origin, course_id, identifier], revision, filename,
                            download_url, origin, token, size=item.get('size') or 0)
        emit('etl_file', identifier, filename, result['content'], status=result['status'], source_url=base + '/files/' + identifier,
             updated=item.get('updated_at') or item.get('modified_at') or item.get('created_at'),
             extraction_upgrade=result.get('extraction_upgrade', False),
             **({'file_hash': result['file_hash']} if result.get('file_hash') else {}))

    seen_external = set()
    for url, parent, title, updated in external[:MAX_COURSE_DOCUMENTS]:
        try:
            value = _valid_url(url)
            identity = value.hostname + value.path
            if identity in seen_external: continue
            seen_external.add(identity)
            identifier = 'external-' + digest(identity)
            mode = 'dropbox' if _dropbox(value.hostname) else 'snu' if value.hostname.endswith('.snu.ac.kr') else None
            if mode is None:
                emit('etl_file', identifier, title, status='external_reference', source_url=parent)
                continue
            # External links rarely expose a revision. Revalidate daily; stable
            # content_hash excludes the cache revision and collection time.
            revision = [updated, datetime.now(timezone.utc).date().isoformat()]
            result = cache.read(['external', course_id, identity], revision, 'external.pdf', url, origin, '', mode=mode)
            emit('etl_file', identifier, title, result['content'], status=result['status'], source_url=parent, updated=updated,
                 extraction_upgrade=result.get('extraction_upgrade', False),
                 **({'file_hash': result['file_hash']} if result.get('file_hash') else {}))
        except Exception: issues.append({'code': 'external_document_unreadable', 'course': course})
    coverage['attachments'] = {'referenced': len(file_ids), 'files_found': len(by_id)}
    coverage['extraction'] = dict(Counter(source['extraction_status'] for source in sources))
    coverage['downloads'] = {'bytes': cache.bytes - before[0], 'count': cache.downloads - before[1], 'cache_hits': cache.hits - before[2]}
    return {'sources': sources, 'issues': issues, 'coverage': coverage}
