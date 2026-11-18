"""Paginated public-domain image searches against the museums' collection APIs."""
import json
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.parse import unquote, urlencode
from urllib.request import Request, urlopen


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
    return Request(url, headers={"User-Agent": "FrameSync/0.1 (Open Access artwork browser)"})


def fetch_json(url):
    with urlopen(museum_request(url), timeout=12) as response:
        return json.load(response)


def record(source, object_id, title, artist, year, preview, original, link, width=0, height=0):
    return dict(key=f"{source}:{object_id}", source=source, id=object_id, title=title or "Untitled",
                artist=artist or "Unknown artist", year=year or "", preview=preview or original,
                original=original, link=link, width=int(width or 0), height=int(height or 0))


def met_artwork(data):
    if data.get("isPublicDomain") is not True or not data.get("primaryImage"):
        return None
    object_id = data["objectID"]
    return record("met", object_id, data["title"], data["artistDisplayName"], data["objectDate"],
                  data["primaryImageSmall"], data["primaryImage"],
                  f"https://www.metmuseum.org/art/collection/search/{object_id}",
                  data.get("imageWidth"), data.get("imageHeight"))


def met_search(query, offset, filters, cancelled, fetch):
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
    page = fetch(f"{MET}/v1.1/search?{urlencode(params)}")
    ids = page.get("objectIDs") or []
    works = []
    for object_id in ids:
        if cancelled():
            raise InterruptedError("Museum search cancelled.")
        try:
            data = fetch(f"{MET}/v1/objects/{int(object_id)}")
        except HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        if filters.department.casefold() not in (data.get("department") or "").casefold():
            continue
        if filters.artist.casefold() not in (data.get("artistDisplayName") or "").casefold():
            continue
        work = met_artwork(data)
        if work:
            works.append(work)
    end = offset + len(ids)
    return SearchPage(works, end, bool(ids) and end < min(page["total"], 10000))


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
        accession = data["accession_number"]
        works.append(record("cleveland", data["id"], data["title"], artists, data["creation_date"],
                            (images.get("web") or {}).get("url"), image["url"],
                            f"https://www.clevelandart.org/art/{accession}", image.get("width"), image.get("height")))
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
        params = dict(action="query", format="json", prop="imageinfo", titles="|".join(list(by_file)[:50]),
                      iiprop="url|size|mime|extmetadata", iiurlwidth=500,
                      iiextmetadatafilter="LicenseShortName|Copyrighted")
        result = fetch("https://commons.wikimedia.org/w/api.php?" + urlencode(params)).get("query", {})
        for norm in result.get("normalized", []):
            by_file[norm["to"]] = by_file.get(norm["from"], [])
        pages = result.get("pages", {})
        if not pages:
            return
        if cancelled():
            raise InterruptedError("Museum search cancelled.")
        sizes = dict(action="query", format="json", prop="imageinfo", pageids="|".join(k for k in pages if int(k) > 0),
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
