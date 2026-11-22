"""Paginated public-domain image searches against the museums' collection APIs."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
import json
import socket
import ssl
import threading
from time import monotonic
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, getproxies, proxy_bypass

import urllib3
from urllib3.connection import HTTPConnection, HTTPSConnection


try:
    _APP_VERSION = version("framesync")
except PackageNotFoundError:
    _APP_VERSION = "0.1"
_USER_AGENT = f"FrameSync/{_APP_VERSION} (https://github.com/kcverde/framesync; Open Access artwork browser)"

MAX_JSON_BYTES = 16 * 1024 * 1024
JSON_DEADLINE_SECONDS = 15.0

_REQUEST_LOCK = threading.Lock()
_ACTIVE_RESPONSES: dict[int, set] = {}
_CANCELLED_THREADS: set[int] = set()
_REQUEST_CONTEXT = threading.local()
MET_SEARCH_WORKERS = 4


class _HttpsOnlyRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Only HTTPS museum URLs are supported.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = build_opener(_HttpsOnlyRedirectHandler)


def _request_owner():
    return getattr(_REQUEST_CONTEXT, "owner", threading.get_ident())


class _PendingHeaders:
    def __init__(self, sock):
        self.sock = sock

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class _CancellableHeaders:
    @contextmanager
    def _reading_headers(self):
        with tracked_response(_PendingHeaders(self.sock), deadline=_REQUEST_CONTEXT.deadline) as expired:
            try:
                yield
            except Exception as exc:
                if is_thread_cancelled():
                    raise InterruptedError("Museum request cancelled.") from exc
                if expired.is_set():
                    raise TimeoutError("Timed out reading museum response headers.") from exc
                raise
            if is_thread_cancelled():
                raise InterruptedError("Museum request cancelled.")
            if expired.is_set():
                raise TimeoutError("Timed out reading museum response headers.")

    def getresponse(self):
        with self._reading_headers():
            return super().getresponse()

    def _tunnel(self):
        with self._reading_headers():
            return super()._tunnel()


class _MuseumHTTPConnection(_CancellableHeaders, HTTPConnection):
    pass


class _MuseumHTTPSConnection(_CancellableHeaders, HTTPSConnection):
    pass


class _MuseumHTTPPool(urllib3.HTTPConnectionPool):
    ConnectionCls = _MuseumHTTPConnection


class _MuseumHTTPSPool(urllib3.HTTPSConnectionPool):
    ConnectionCls = _MuseumHTTPSConnection


def _new_pool(proxy):
    options = dict(maxsize=1, ssl_context=ssl.create_default_context())
    if proxy:
        parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
        # Proxy credentials are sent only to the proxy, never to the museum host.
        headers = urllib3.make_headers(proxy_basic_auth=f"{unquote(parsed.username or '')}:{unquote(parsed.password or '')}") if parsed.username else None
        manager = urllib3.ProxyManager(f"{parsed.scheme}://{parsed.netloc.rsplit('@', 1)[-1]}",
                                       proxy_headers=headers, **options)
    else:
        manager = urllib3.PoolManager(**options)
    manager.pool_classes_by_scheme = {"http": _MuseumHTTPPool, "https": _MuseumHTTPSPool}
    return manager


@contextmanager
def _pooled_requests(owner):
    # Keep each worker's sockets private until its response has been unregistered.
    pools = {}
    _REQUEST_CONTEXT.owner = owner
    _REQUEST_CONTEXT.pools = pools
    try:
        yield
    finally:
        del _REQUEST_CONTEXT.owner
        del _REQUEST_CONTEXT.pools
        for pool in pools.values():
            pool.clear()


@contextmanager
def _pooled_open(pools, request, timeout, deadline):
    url = request.full_url
    for _ in range(11):
        if is_thread_cancelled():
            raise InterruptedError("Museum request cancelled.")
        validated = museum_request(url)
        proxy = None if proxy_bypass(validated.host) else getproxies().get("https")
        if proxy not in pools:
            pools[proxy] = _new_pool(proxy)
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out opening museum response.")
        _REQUEST_CONTEXT.deadline = deadline
        try:
            response = pools[proxy].request(
                "GET", url, headers=dict(request.header_items()),
                timeout=urllib3.Timeout(total=remaining, connect=timeout, read=timeout),
                preload_content=False, redirect=False, retries=False,
            )
        except Exception as exc:
            if is_thread_cancelled():
                raise InterruptedError("Museum request cancelled.") from exc
            if monotonic() >= deadline:
                raise TimeoutError("Timed out opening museum response.") from exc
            raise
        finally:
            del _REQUEST_CONTEXT.deadline
        try:
            location = response.get_redirect_location()
            if location:
                url = urljoin(url, location)
                continue
            if not 200 <= response.status < 300:
                raise HTTPError(url, response.status, response.reason, response.headers, None)
            yield response
            return
        finally:
            # Do not drain large partial image responses just to recycle a connection.
            response.close()
            response.release_conn()
    raise ValueError("Too many museum redirects.")


def urlopen(request, timeout=None, *, deadline=None):
    pools = getattr(_REQUEST_CONTEXT, "pools", None)
    if pools is not None:
        return _pooled_open(pools, request, timeout, deadline)
    return _OPENER.open(request, timeout=timeout)


def _response_socket(response):
    raw = getattr(response, "fp", None)
    inner = getattr(raw, "raw", raw)
    return getattr(inner, "_sock", None)


def _abort_response(response):
    if isinstance(response, urllib3.HTTPResponse):
        try:
            response.shutdown()
        except (OSError, RuntimeError, ValueError):
            pass
        return
    sock = _response_socket(response)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        return
    if hasattr(response, "close"):
        try:
            response.close()
        except Exception:
            pass


def read_chunk(response, size):
    reader = getattr(response, "read1", response.read)
    return reader(size)


def abort_thread_requests(thread_id: int | None):
    if thread_id is None:
        return
    with _REQUEST_LOCK:
        _CANCELLED_THREADS.add(thread_id)
        for response in _ACTIVE_RESPONSES.get(thread_id, ()):
            _abort_response(response)


def clear_thread_cancellation(thread_id: int):
    with _REQUEST_LOCK:
        _CANCELLED_THREADS.discard(thread_id)


def is_thread_cancelled(thread_id: int | None = None) -> bool:
    ident = _request_owner() if thread_id is None else thread_id
    with _REQUEST_LOCK:
        return ident in _CANCELLED_THREADS


@contextmanager
def tracked_response(response, *, deadline: float | None = None):
    ident = _request_owner()
    expired = threading.Event()
    with _REQUEST_LOCK:
        if ident in _CANCELLED_THREADS:
            _abort_response(response)
            raise InterruptedError("Museum request cancelled.")
        _ACTIVE_RESPONSES.setdefault(ident, set()).add(response)
    timer = None
    if deadline is not None:
        remaining = deadline - monotonic()
        if remaining <= 0:
            expired.set()
            _abort_response(response)
            with _REQUEST_LOCK:
                bucket = _ACTIVE_RESPONSES.get(ident)
                if bucket is not None:
                    bucket.discard(response)
                    if not bucket:
                        _ACTIVE_RESPONSES.pop(ident, None)
            raise TimeoutError("Timed out reading response from museum service.")

        def expire():
            expired.set()
            _abort_response(response)

        timer = threading.Timer(remaining, expire)
        timer.start()
    try:
        yield expired
    finally:
        if timer is not None:
            timer.cancel()
            timer.join()
        with _REQUEST_LOCK:
            bucket = _ACTIVE_RESPONSES.get(ident)
            if bucket is not None:
                bucket.discard(response)
                if not bucket:
                    _ACTIVE_RESPONSES.pop(ident, None)


PAGE_SIZE = 12
MAX_IMPORT_BYTES = 128 * 1024 * 1024
MET = "https://collectionapi.metmuseum.org/public/collection"
CLEVELAND = "https://openaccess-api.clevelandart.org/api/artworks/"
CHICAGO = "https://api.artic.edu/api/v1/artworks/search"
SOURCES = {"met": "The Met", "cleveland": "Cleveland", "chicago": "Chicago"}


@dataclass
class SearchPage:
    works: list
    next_offset: int
    more: bool
    excluded_by_filters: bool = False


@dataclass(frozen=True)
class Filters:
    source: str = "met"
    field: str = "Keyword"
    artist: str = ""
    medium: str = ""
    department: str = ""
    region: str = ""
    start: int | None = None
    end: int | None = None
    orientation: str = "Any"
    fits: bool = False

    @classmethod
    def from_form(cls, values):
        data = dict(values)
        for field in ("start", "end"):
            value = str(data.get(field, "")).strip()
            if value:
                try:
                    data[field] = int(value)
                except ValueError as exc:
                    raise ValueError("Enter calendar years as whole numbers.") from exc
            else:
                data[field] = None
        if data["start"] is not None and data["end"] is not None and data["start"] > data["end"]:
            raise ValueError("The first year must come before the last year.")
        return cls(**data)


def museum_request(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Only HTTPS museum URLs are supported.")
    return Request(url, headers={"User-Agent": _USER_AGENT})


def fetch_json(url):
    deadline = monotonic() + JSON_DEADLINE_SECONDS
    with urlopen(museum_request(url), timeout=12, deadline=deadline) as response, tracked_response(response, deadline=deadline) as expired:
        chunks = []
        received = 0
        try:
            while True:
                if is_thread_cancelled():
                    raise InterruptedError("Museum search cancelled.")
                if expired.is_set() or monotonic() > deadline:
                    raise TimeoutError("Timed out reading response from museum service.")
                chunk = read_chunk(response, 64 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > MAX_JSON_BYTES:
                    raise ValueError("Museum response exceeded size limit.")
                chunks.append(chunk)
        except Exception as exc:
            if is_thread_cancelled():
                raise InterruptedError("Museum search cancelled.") from exc
            if expired.is_set():
                raise TimeoutError("Timed out reading response from museum service.") from exc
            raise
        if is_thread_cancelled():
            raise InterruptedError("Museum search cancelled.")
        if expired.is_set():
            raise TimeoutError("Timed out reading response from museum service.")
        return json.loads(b"".join(chunks))


def record(source, object_id, title, artist, year, preview, original, link, width=0, height=0):
    return dict(key=f"{source}:{object_id}", source=source, id=object_id, title=title or "Untitled",
                artist=artist or "Unknown artist", year=year or "", preview=preview or original,
                original=original, link=link, width=int(width or 0), height=int(height or 0))


def met_artwork(data):
    if data.get("isPublicDomain") is not True or not data.get("primaryImage"):
        return None
    object_id = data.get("objectID")
    if object_id is None:
        return None
    return record("met", object_id, data.get("title"), data.get("artistDisplayName"), data.get("objectDate"),
                  data.get("primaryImageSmall"), data["primaryImage"],
                  f"https://www.metmuseum.org/art/collection/search/{object_id}",
                  data.get("imageWidth"), data.get("imageHeight"))


def met_search(query, offset, filters, cancelled, fetch, dimensions=None):
    limit = min(PAGE_SIZE, 10000 - offset)
    term = query or filters.artist or filters.medium or filters.department or filters.region or "*"
    params = dict(q=term, hasImages="true", offset=offset, limit=limit)
    if filters.field == "Title" and query:
        params["title"] = "true"
    if filters.field == "Subject" and query:
        params["tags"] = "true"
    if not query and filters.artist:
        params["artistOrCulture"] = "true"
    if filters.medium:
        params["medium"] = filters.medium
    if filters.region:
        params["geoLocation"] = filters.region
    if filters.start is not None or filters.end is not None:
        params.update(dateBegin=filters.start if filters.start is not None else -100000,
                      dateEnd=filters.end if filters.end is not None else 9999)
    owner = _request_owner()
    with _pooled_requests(owner):
        if cancelled():
            raise InterruptedError("Museum search cancelled.")
        page = fetch(f"{MET}/v1.1/search?{urlencode(params)}")
    ids = page.get("objectIDs") or []
    department, artist = filters.department.casefold(), filters.artist.casefold()

    def fetch_objects(batch):
        works = []
        excluded_by_filters = False
        with _pooled_requests(owner):
            for object_id in batch:
                if cancelled():
                    raise InterruptedError("Museum search cancelled.")
                try:
                    data = fetch(f"{MET}/v1/objects/{int(object_id)}")
                except HTTPError as exc:
                    if exc.code == 404:
                        continue
                    raise
                work = met_artwork(data)
                if not work:
                    continue
                if (department not in (data.get("department") or "").casefold()
                        or artist not in (data.get("artistDisplayName") or "").casefold()):
                    excluded_by_filters = True
                    continue
                if dimensions and not (work["width"] and work["height"]):
                    work["width"], work["height"] = dimensions(work["original"], cancelled)
                works.append(work)
        return works, excluded_by_filters

    works = []
    excluded_by_filters = False
    if ids:
        batch_size = (len(ids) + MET_SEARCH_WORKERS - 1) // MET_SEARCH_WORKERS
        batches = [ids[start:start + batch_size] for start in range(0, len(ids), batch_size)]
        with ThreadPoolExecutor(max_workers=MET_SEARCH_WORKERS) as executor:
            for batch, excluded in executor.map(fetch_objects, batches):
                works.extend(batch)
                excluded_by_filters |= excluded
    end = offset + len(ids)
    return SearchPage(works, end, bool(ids) and end < min(page["total"], 10000), excluded_by_filters)


def cleveland_search(query, offset, filters, cancelled, fetch):
    params = dict(cc0="", has_image=1, skip=offset, limit=PAGE_SIZE)
    params["title" if filters.field == "Title" else "q"] = query
    for key, value in [("artists", filters.artist), ("medium", filters.medium),
                       ("department", filters.department), ("culture", filters.region)]:
        if value:
            params[key] = value
    if filters.start is not None:
        params["created_after"] = filters.start - 1
    if filters.end is not None:
        params["created_before"] = filters.end + 1
    page = fetch(f"{CLEVELAND}?{urlencode(params)}")
    works = []
    for data in page["data"]:
        images = data.get("images") or {}
        candidates = (images.get(key) or {} for key in ("full", "print", "web"))
        image = next((variant for variant in candidates if variant.get("url") and
                      int(variant.get("filesize") or 0) <= MAX_IMPORT_BYTES and
                      int(variant.get("width") or 0) * int(variant.get("height") or 0) <= 89_000_000), None)
        if not image or data.get("share_license_status") != "CC0":
            continue
        artists = ", ".join(c["description"] for c in data.get("creators") or [] if c.get("description"))
        accession = data.get("accession_number") or ""
        link = f"https://www.clevelandart.org/art/{quote(str(accession), safe='')}" if accession else "https://www.clevelandart.org/art/collection/search"
        works.append(record("cleveland", data["id"], data.get("title"), artists, data.get("creation_date"),
                            (images.get("web") or {}).get("url"), image["url"],
                            link, image.get("width"), image.get("height")))
    end = offset + len(page["data"])
    return SearchPage(works, end, bool(page["data"]) and end < page["info"]["total"])


def commons_jpeg(image, variant):
    metadata = image.get("extmetadata", {})
    license_name = metadata.get("LicenseShortName", {}).get("value")
    if (license_name not in ("Public domain", "CC0") or
            metadata.get("Copyrighted", {}).get("value", "").lower() != "false" or image.get("mime") != "image/jpeg"):
        return None
    width, height = image["width"], image["height"]
    download = image["url"]
    if width > 3840 and variant.get("thumbwidth") == 3840 and variant.get("thumburl"):
        width, height = variant["thumbwidth"], variant["thumbheight"]
        download = variant["thumburl"]
    elif image["size"] > MAX_IMPORT_BYTES:
        return None
    if not 0 < width * height <= 89_000_000:
        return None
    return dict(original=download, preview=image.get("thumburl") or download, width=width,
                height=height, imageCredit=image["descriptionurl"], license=license_name)


def chicago_reproductions(works, fetch, cancelled):
    """Find openly licensed Commons images by exact Art Institute artwork ID."""
    ids = {str(int(work["id"])): work for work in works}
    if not ids:
        return
    values = " ".join(f'"{key}"' for key in ids)
    query = f"SELECT ?id ?image WHERE {{ VALUES ?id {{ {values} }} ?art wdt:P4610 ?id; wdt:P18 ?image. }}"
    try:
        data = fetch("https://query.wikidata.org/sparql?" + urlencode(dict(query=query, format="json")))
        if cancelled():
            raise InterruptedError("Museum search cancelled.")
        by_file = {}
        for row in data.get("results", {}).get("bindings", []):
            work = ids.get(row["id"]["value"])
            _, marker, filename = row["image"]["value"].partition("/Special:FilePath/")
            if work and marker:
                title = "File:" + unquote(filename).replace("_", " ")
                by_file.setdefault(title, []).append(work)
        if not by_file:
            return
        params = dict(action="query", format="json", prop="imageinfo", redirects="1", titles="|".join(list(by_file)[:50]),
                      iiprop="url|size|mime|extmetadata", iiurlwidth=500,
                      iiextmetadatafilter="LicenseShortName|Copyrighted")
        result = fetch("https://commons.wikimedia.org/w/api.php?" + urlencode(params)).get("query", {})
        for mapping in (*result.get("normalized", []), *result.get("redirects", [])):
            source_works = by_file.get(mapping["from"], [])
            if source_works:
                target_works = by_file.setdefault(mapping["to"], [])
                for work in source_works:
                    if work not in target_works:
                        target_works.append(work)
        pages = result.get("pages", {})
        page_ids = [k for k in pages if str(k).lstrip("-").isdigit() and int(k) > 0]
        if not page_ids:
            return
        if cancelled():
            raise InterruptedError("Museum search cancelled.")
        sizes = dict(action="query", format="json", prop="imageinfo", pageids="|".join(page_ids),
                     iiprop="url", iiurlwidth=3840)
        renders = fetch("https://commons.wikimedia.org/w/api.php?" + urlencode(sizes)).get("query", {}).get("pages", {})
        for page_id, page in pages.items():
            for image in page.get("imageinfo", []):
                variant = next(iter(renders.get(page_id, {}).get("imageinfo", [])), {})
                reproduction = commons_jpeg(image, variant)
                if reproduction:
                    for work in by_file.get(page["title"], []):
                        if reproduction["width"] * reproduction["height"] > work["width"] * work["height"]:
                            work.update(reproduction)
    except InterruptedError:
        raise
    except (OSError, ValueError, KeyError):
        # Optional image services must not prevent browsing the museum's own records.
        return


def chicago_search(query, offset, filters, cancelled, fetch):
    clauses = [{"term": {"is_public_domain": True}}, {"exists": {"field": "image_id"}}]
    fields = "id,title,image_id,thumbnail,artist_title,date_display,is_public_domain"
    for key, value in [("artist_title", filters.artist), ("medium_display", filters.medium),
                       ("department_title", filters.department), ("place_of_origin", filters.region)]:
        if value:
            clauses.append({"match": {key: {"query": value, "operator": "and"}}})
    if filters.start is not None:
        clauses.append({"range": {"date_end": {"gte": filters.start}}})
    if filters.end is not None:
        clauses.append({"range": {"date_start": {"lte": filters.end}}})
    params = dict(page=1 + offset // PAGE_SIZE, limit=PAGE_SIZE, fields=fields)
    if filters.field == "Keyword":
        if query:
            params["q"] = query
    elif query:
        field = "title" if filters.field == "Title" else "subject_titles"
        clauses.append({"match": {field: {"query": query, "operator": "and"}}})
    params["query"] = {"bool": {"filter": clauses}}
    page = fetch(f"{CHICAGO}?{urlencode({'params':json.dumps(params, separators=(',', ':'))})}")
    iiif = page["config"]["iiif_url"].rstrip("/")
    works = []
    for data in page["data"]:
        if data.get("is_public_domain") is not True or not data.get("image_id"):
            continue
        base = f"{iiif}/{data['image_id']}/full/"
        work = record("chicago", data["id"], data["title"], data.get("artist_title"),
                      data.get("date_display"), base + "400,/0/default.jpg", base + "1686,/0/default.jpg",
                      f"https://www.artic.edu/artworks/{data['id']}")
        thumbnail = data.get("thumbnail") or {}
        work.update(placeholder=thumbnail.get("lqip") or "", sourceWidth=thumbnail.get("width") or 0,
                    sourceHeight=thumbnail.get("height") or 0)
        works.append(work)
    chicago_reproductions(works, fetch, cancelled)
    paging = page["pagination"]
    return SearchPage(works, offset + PAGE_SIZE, bool(page["data"]) and paging["current_page"] < paging["total_pages"])
