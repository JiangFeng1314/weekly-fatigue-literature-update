#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Zotero Weekly Sync.

Imports cloud weekly literature batches (already produced upstream) into a local
Windows Zotero installation.  Stdlib-only.

Read model:
  * /api/users/0/... is read-only.
Write model (Zotero 9.x local Connector):
  * POST /connector/saveItems      -> create items in a save session
  * POST /connector/updateSession  -> move session items to a target collection
  * POST /connector/saveAttachment -> attach a PDF child to a session item
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


CONNECTOR_API_VERSION = "3"
SKIP_ITEM_TYPES = {"attachment", "note", "annotation"}
COUNT_KEYS = [
    "CREATED",
    "EXISTING",
    "COLLECTION_ADDED",
    "PDF_ATTACHED",
    "PDF_ALREADY_EXISTS",
    "PDF_REQUIRES_ACCESS",
    "PDF_UNAVAILABLE",
    "CLASSIFICATION_ERROR",
    "NEEDS_REVIEW",
    "FAILED",
]


def _reconfigure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


class FatalError(Exception):
    """Configuration or infrastructure error that stops the whole run."""


class BatchControlJsonMissing(FatalError):
    """The authoritative control JSON for a discovered batch is missing."""


# --------------------------------------------------------------------------- #
# Config / paths
# --------------------------------------------------------------------------- #


def load_config(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise FatalError(f"config not found: {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        raise FatalError(f"invalid config {p}: {exc}") from exc


def find_inbox(config: Dict[str, Any]) -> Optional[Path]:
    candidates: List[Path] = []
    configured = config.get("google_drive_inbox")
    if configured:
        candidates.append(Path(os.path.expandvars(str(configured))).expanduser())

    user = Path(os.path.expanduser("~"))
    candidates.extend(
        [
            user / "Documents" / "Zotero周报同步" / "待入库",
            user / "Zotero周报同步" / "待入库",
            user / "OneDrive" / "Zotero周报同步" / "待入库",
            Path("G:/Zotero周报同步/待入库"),
            Path("D:/Zotero周报同步/待入库"),
            Path("E:/Zotero周报同步/待入库"),
        ]
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def discover_batch(inbox: Path, config: Dict[str, Any]) -> Optional[Tuple[str, Path, str]]:
    """Discover a batch strictly from its authoritative control JSON.

    ``Zotero_本周入库清单_YYYYMMDD.json`` is the only classification control
    file.  RIS / CSV / PDF CSV files are deliberately ignored here and can
    never become the classification source, regardless of directory
    enumeration order.
    """
    control_pattern = re.compile(r"^Zotero_本周入库清单_(\d{8})\.json$")
    dates: List[str] = []
    for file in inbox.iterdir():
        if not file.is_file():
            continue
        match = control_pattern.match(file.name)
        if match:
            dates.append(match.group(1))

    if not dates:
        return None

    batch_date = max(dates)
    control_json = inbox / f"Zotero_本周入库清单_{batch_date}.json"
    if not control_json.is_file():
        raise BatchControlJsonMissing(control_json)
    print(f"Batch control file: {control_json}")
    return batch_date, control_json, "json"


# --------------------------------------------------------------------------- #
# Batch parsing
# --------------------------------------------------------------------------- #


def _first(raw: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = raw.get(key)
        if value is not None and value != "":
            return value
    return None


def normalize_authors(value: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []

    def add_one(author: Any) -> None:
        if isinstance(author, dict):
            first = author.get("firstName") or author.get("givenName") or ""
            last = author.get("lastName") or author.get("familyName") or author.get("name") or ""
            creator_type = str(author.get("creatorType") or "author")
            if not first and not last:
                return
            out.append({"firstName": str(first), "lastName": str(last), "creatorType": creator_type})
            return
        if isinstance(author, (list, tuple)):
            for item in author:
                add_one(item)
            return
        if isinstance(author, str):
            for part in re.split(r"[;\n]", author):
                part = part.strip().strip(".,")
                if not part:
                    continue
                first, last = _split_creator_name(part)
                out.append({"firstName": first, "lastName": last, "creatorType": "author"})

    if isinstance(value, str) or isinstance(value, list):
        add_one(value)
    return out


def _split_creator_name(name: str) -> Tuple[str, str]:
    name = name.strip()
    if "," in name:
        last, first = name.split(",", 1)
        return first.strip(), last.strip()
    parts = name.split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]


def normalize_tags(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [p.strip() for p in re.split(r"[,;，；\n]", value) if p.strip()]
    if isinstance(value, list):
        out: List[str] = []
        for item in value:
            if isinstance(item, str):
                out.extend(normalize_tags(item))
            elif isinstance(item, dict):
                tag = item.get("tag") or item.get("name")
                if tag:
                    out.append(str(tag))
        return list(dict.fromkeys(out))
    return []


def json_raw_to_record(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    title_value = _first(raw, "title", "标题", "Title", "name")
    title = str(title_value).strip() if title_value is not None else ""
    if not title:
        # Keep records that carry other literature-control markers so a missing
        # title is surfaced by schema validation instead of being dropped.
        has_marker = any(
            raw.get(key) is not None
            for key in (
                "doi", "DOI",
                "main_category", "mainCategory", "category", "分类",
                "target_collection", "targetCollection",
                "cumulative_id", "cumulativeId",
            )
        )
        if not has_marker:
            return None

    pages = _first(raw, "pages", "page")
    article_number = _first(raw, "article_number", "articleNumber", "article_no")
    return {
        "title": title,
        "authors": normalize_authors(
            _first(raw, "authors", "author", "creators", "Authors", "Author", "作者")
        ),
        "journal": _first(raw, "journal", "publicationTitle", "journalTitle", "source", "期刊", "Journal"),
        "year": _first(raw, "year", "pub_year", "date", "Year", "年份"),
        "volume": _first(raw, "volume", "Volume"),
        "issue": _first(raw, "issue", "Issue"),
        "pages": pages,
        "article_number": article_number,
        "doi": _first(raw, "doi", "DOI"),
        "url": _first(raw, "url", "link", "URL"),
        "pdf_url": _first(raw, "pdf_url", "pdfUrl", "fulltext_url", "fulltextUrl"),
        "pdf_access_status": _first(raw, "pdf_access_status", "pdfAccessStatus", "access_status"),
        "main_category": _first(raw, "main_category", "mainCategory", "category", "分类"),
        "target_collection": _first(raw, "target_collection", "targetCollection"),
        "tags": normalize_tags(_first(raw, "tags", "keywords", "Tags")),
        "source_week": _first(raw, "source_week", "sourceWeek", "week"),
        "cumulative_id": _first(raw, "cumulative_id", "cumulativeId", "id"),
    }


def parse_json_records(path: Path) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    raws: List[Any] = []
    if isinstance(data, list):
        raws = data
    elif isinstance(data, dict):
        for key in ("records", "items", "data", "entries", "文献", "results", "papers"):
            if isinstance(data.get(key), list):
                raws = data[key]
                break
        else:
            if "title" in data:
                raws = [data]

    records: List[Dict[str, Any]] = []
    for raw in raws:
        record = json_raw_to_record(raw)
        if record:
            records.append(record)
    return records


def parse_ris_file(path: Path) -> List[Dict[str, Any]]:
    raw_records: List[Dict[str, List[str]]] = []
    current: Optional[Dict[str, List[str]]] = None

    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        match = re.match(r"^([A-Za-z0-9]{2})\s{0,2}-\s?(.*)$", line)
        if not match:
            continue
        tag, value = match.group(1).upper(), match.group(2).strip()
        if tag == "TY":
            if current is not None:
                raw_records.append(current)
            current = {}
        if current is None:
            current = {}
        if tag == "ER":
            raw_records.append(current)
            current = None
            continue
        current.setdefault(tag, []).append(value)

    if current is not None:
        raw_records.append(current)
    return raw_records


def ris_to_record(raw: Dict[str, List[str]]) -> Optional[Dict[str, Any]]:
    title = " ".join(raw.get("TI", [])).strip()
    if not title:
        return None

    start_page = _ris_first(raw, "SP")
    end_page = _ris_first(raw, "EP")
    pages = None
    if start_page or end_page:
        pages = f"{start_page or ''}-{end_page or ''}"

    return {
        "title": title,
        "authors": normalize_authors(raw.get("AU", [])),
        "journal": _ris_first(raw, "JO", "JF", "T2"),
        "year": _ris_first(raw, "PY", "Y1"),
        "volume": _ris_first(raw, "VL"),
        "issue": _ris_first(raw, "IS"),
        "pages": pages,
        "article_number": None,
        "doi": _ris_first(raw, "DO"),
        "url": _ris_first(raw, "UR", "L1"),
        "pdf_url": None,
        "pdf_access_status": None,
        "main_category": None,
        "target_collection": None,
        "tags": normalize_tags(raw.get("KW", [])),
        "source_week": None,
        "cumulative_id": None,
    }


def _ris_first(raw: Dict[str, List[str]], *tags: str) -> Optional[str]:
    for tag in tags:
        values = raw.get(tag)
        if values:
            return values[0]
    return None


def load_records(path: Path, kind: str) -> List[Dict[str, Any]]:
    if kind == "json":
        return parse_json_records(path)
    return [ris_to_record(r) for r in parse_ris_file(path) if ris_to_record(r)]


# --------------------------------------------------------------------------- #
# Normalization / classification / dedupe
# --------------------------------------------------------------------------- #


def normalize_doi(value: Any) -> Optional[str]:
    if not value:
        return None
    s = str(value).strip()
    s = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:|DOI:)", "", s, flags=re.IGNORECASE)
    s = s.strip().strip('"').strip()
    s = s.rstrip("/").strip()
    s = s.lower()
    return s or None


def normalize_title(value: Any) -> str:
    if not value:
        return ""
    s = unicodedata.normalize("NFKC", str(value))
    s = s.casefold()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


CONTROL_REQUIRED_FIELDS = ("title", "main_category", "target_collection")


def _missing_control_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (list, tuple, dict)) and not value:
        return True
    return False


def validate_control_record(rec: Dict[str, Any]) -> Optional[str]:
    """Return a schema error for a control record, or None when valid.

    The authoritative JSON must provide, for every record, at least ``title``,
    ``main_category`` and ``target_collection``.  Validation failures report the
    actual parsed values so the bad record can be found and fixed upstream.
    """
    missing = [
        field
        for field in CONTROL_REQUIRED_FIELDS
        if _missing_control_value(rec.get(field))
    ]
    if not missing:
        return None
    actual = {
        field: rec.get(field)
        for field in ("title", "doi", "main_category", "target_collection")
    }
    return f"schema missing={missing} actual={actual}"


def classify_record(
    rec: Dict[str, Any], config: Dict[str, Any]
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    schema_error = validate_control_record(rec)
    if schema_error:
        return None, None, None, "CLASSIFICATION_ERROR", schema_error

    raw_category = rec.get("main_category")
    if raw_category is None:
        return None, None, None, "CLASSIFICATION_ERROR", "main_category missing"

    if isinstance(raw_category, bool):
        category = None
    elif isinstance(raw_category, int):
        category = str(raw_category)
    elif isinstance(raw_category, float) and raw_category.is_integer():
        category = str(int(raw_category))
    else:
        category = str(raw_category).strip()

    collection_map = config.get("collection_map") or {}
    if category not in collection_map:
        return None, None, None, "CLASSIFICATION_ERROR", f"invalid main_category: {raw_category!r}"

    entry = collection_map[category]
    target_collection = rec.get("target_collection")
    if not target_collection or str(target_collection).strip() != entry.get("name"):
        return (
            None,
            None,
            None,
            "CLASSIFICATION_ERROR",
            "target_collection missing or does not match mapped collection name",
        )
    return category, entry.get("key"), entry.get("target_id"), None, None


def collection_name(config: Dict[str, Any], category: str) -> Optional[str]:
    entry = (config.get("collection_map") or {}).get(category)
    return entry.get("name") if entry else None


def build_library_index(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    return top_item_map(fetch_all_items(config))


def find_matches(rec: Dict[str, Any], index: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    doi = normalize_doi(rec.get("doi"))
    title = normalize_title(rec.get("title"))
    if doi:
        candidates = [item for item in index if item["doi_norm"] == doi]
        if candidates:
            return candidates
    if title:
        return [item for item in index if item["title_norm"] == title]
    return []


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #


def zotero_reachable(config: Dict[str, Any]) -> bool:
    parsed = urllib.parse.urlparse(config.get("zotero_base_url") or "http://127.0.0.1:23119")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 23119
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def http_request(
    url: str,
    method: str = "GET",
    data: Optional[bytes] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 20,
) -> Tuple[int, Dict[str, str], bytes]:
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            response_headers = {k.lower(): v for k, v in response.headers.items()}
            body = response.read()
            return status, response_headers, body
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:  # noqa: BLE001
            body = b""
        headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        return exc.code, headers, body
    except Exception as exc:  # noqa: BLE001
        raise FatalError(f"{method} {url} failed: {exc}") from exc


def fetch_json(
    url: str, params: Optional[Dict[str, str]] = None, timeout: int = 20
) -> Any:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    status, _headers, body = http_request(url, "GET", None, {}, timeout)
    if status != 200:
        snippet = body[:300].decode("utf-8", "replace")
        raise FatalError(f"HTTP {status} GET {url}: {snippet}")
    return json.loads(body.decode("utf-8"))


def fetch_all_items(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    base = (config.get("zotero_base_url") or "").rstrip("/")
    timeout = int(config.get("request_timeout_seconds") or 20)
    items: List[Dict[str, Any]] = []
    start = 0
    limit = 100
    while True:
        page = fetch_json(
            base + "/api/users/0/items",
            {"format": "json", "limit": str(limit), "start": str(start)},
            timeout,
        )
        if not isinstance(page, list):
            raise FatalError("unexpected items response shape")
        items.extend(page)
        if len(page) < limit:
            break
        start += limit
    return items


def top_item_map(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        data = item.get("data") or {}
        if data.get("itemType") in SKIP_ITEM_TYPES:
            continue
        doi = data.get("DOI")
        out.append(
            {
                "key": item.get("key"),
                "title": data.get("title") or "",
                "title_norm": normalize_title(data.get("title") or ""),
                "doi_norm": normalize_doi(doi) if doi else None,
            }
        )
    return out


def fetch_collection_top_item_keys(config: Dict[str, Any], collection_key: str) -> set[str]:
    base = (config.get("zotero_base_url") or "").rstrip("/")
    timeout = int(config.get("request_timeout_seconds") or 20)
    keys: set[str] = set()
    start = 0
    limit = 100
    quoted = urllib.parse.quote(collection_key, safe="")
    while True:
        page = fetch_json(
            f"{base}/api/users/0/collections/{quoted}/items/top",
            {"format": "json", "limit": str(limit), "start": str(start)},
            timeout,
        )
        for item in page:
            if isinstance(item, dict) and item.get("key"):
                keys.add(item["key"])
        if len(page) < limit:
            break
        start += limit
    return keys


def has_pdf_child(config: Dict[str, Any], item_key: str) -> bool:
    base = (config.get("zotero_base_url") or "").rstrip("/")
    timeout = int(config.get("request_timeout_seconds") or 20)
    quoted = urllib.parse.quote(item_key, safe="")
    start = 0
    limit = 100
    while True:
        page = fetch_json(
            f"{base}/api/users/0/items/{quoted}/children",
            {"format": "json", "limit": str(limit), "start": str(start)},
            timeout,
        )
        for child in page:
            data = (child.get("data") or {}) if isinstance(child, dict) else {}
            if data.get("itemType") != "attachment":
                continue
            content_type = str(data.get("contentType") or "").lower()
            filename = str(data.get("filename") or data.get("title") or "").lower()
            if content_type == "application/pdf" or filename.endswith(".pdf"):
                return True
        if len(page) < limit:
            break
        start += limit
    return False


# --------------------------------------------------------------------------- #
# Connector write protocol
# --------------------------------------------------------------------------- #


def connector_save_items(
    config: Dict[str, Any], session_id: str, items: List[Dict[str, Any]]
) -> Tuple[int, Dict[str, str], bytes]:
    base = (config.get("zotero_base_url") or "").rstrip("/")
    payload = {"sessionID": session_id, "items": items, "uri": ""}
    headers = {
        "Content-Type": "application/json",
        "X-Zotero-Connector-API-Version": CONNECTOR_API_VERSION,
    }
    return http_request(
        base + "/connector/saveItems",
        "POST",
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers,
        int(config.get("request_timeout_seconds") or 20),
    )


def connector_update_session(
    config: Dict[str, Any],
    session_id: str,
    target_id: str,
    tags: Optional[List[str]] = None,
    note: str = "",
) -> Tuple[int, Dict[str, str], bytes]:
    base = (config.get("zotero_base_url") or "").rstrip("/")
    payload = {"sessionID": session_id, "target": target_id, "tags": tags or [], "note": note}
    headers = {"Content-Type": "application/json"}
    return http_request(
        base + "/connector/updateSession",
        "POST",
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers,
        int(config.get("request_timeout_seconds") or 20),
    )


def connector_save_attachment(
    config: Dict[str, Any],
    session_id: str,
    parent_item_id: str,
    pdf_url: str,
    title: str,
    pdf_bytes: bytes,
) -> Tuple[int, Dict[str, str], bytes]:
    base = (config.get("zotero_base_url") or "").rstrip("/")
    metadata = json.dumps(
        {"sessionID": session_id, "parentItemID": parent_item_id, "url": pdf_url, "title": title},
        ensure_ascii=False,
    )
    headers = {
        "Content-Type": "application/pdf",
        "X-Zotero-Connector-API-Version": CONNECTOR_API_VERSION,
        "X-Metadata": metadata,
    }
    return http_request(
        base + "/connector/saveAttachment",
        "POST",
        pdf_bytes,
        headers,
        int(config.get("download_timeout_seconds") or 60),
    )


def build_save_item(rec: Dict[str, Any], connector_id: str) -> Dict[str, Any]:
    return {
        "itemType": "journalArticle",
        "id": connector_id,
        "title": rec.get("title") or "(untitled)",
        "creators": rec.get("authors") or [],
        "publicationTitle": rec.get("journal") or "",
        "date": str(rec.get("year")) if rec.get("year") is not None else "",
        "volume": rec.get("volume") or "",
        "issue": rec.get("issue") or "",
        "pages": rec.get("pages") or "",
        "DOI": rec.get("doi") or "",
        "url": rec.get("url") or "",
        "accessDate": "CURRENT_TIMESTAMP",
        "tags": rec.get("tags") or [],
        "attachments": [],
        "notes": [],
    }


# --------------------------------------------------------------------------- #
# PDF download / validation
# --------------------------------------------------------------------------- #


def safe_filename(value: str) -> str:
    value = re.sub(r"[^\w\-]+", "_", value, flags=re.UNICODE).strip("_")
    return value[:100] or "document"


def download_pdf(
    config: Dict[str, Any], url: str, batch_date: str, title: str, downloads_root: Path
) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    timeout = int(config.get("download_timeout_seconds") or 60)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CodexZoteroSync/1.0"}
    status, response_headers, body = http_request(url, "GET", None, headers, timeout)
    if status != 200:
        return None, None, f"http_{status}"
    if not body:
        return None, None, "empty_body"

    content_type = str(response_headers.get("content-type") or "").lower()
    prefix = body[:512].lower()
    if "text/html" in content_type or b"<html" in prefix:
        return None, None, "html_response_not_pdf"
    if not body.startswith(b"%PDF-") and "application/pdf" not in content_type:
        return None, None, "not_pdf"

    dest_dir = downloads_root / batch_date
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (safe_filename(title) + ".pdf")
    tmp = tempfile.NamedTemporaryFile(delete=False, dir=str(dest_dir), suffix=".tmp")
    try:
        tmp.write(body)
        tmp.close()
        shutil.move(tmp.name, str(dest))
    except Exception as exc:  # noqa: BLE001
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        return None, None, f"write_failed: {exc}"
    return body, str(dest), None


def pdf_plan_label(rec: Dict[str, Any]) -> str:
    status = rec.get("pdf_access_status")
    if rec.get("pdf_url"):
        if status in (None, "", "available"):
            return "available"
        return str(status)
    if status == "requires_access":
        return "requires_access"
    if status == "unavailable":
        return "unavailable"
    return "-"


# --------------------------------------------------------------------------- #
# Logging / archive / state
# --------------------------------------------------------------------------- #


def new_counts() -> Dict[str, int]:
    return {key: 0 for key in COUNT_KEYS}


def make_log_entry(
    rec: Dict[str, Any],
    batch_date: str,
    main_category: Optional[str],
    target_collection: Optional[str],
    target_collection_key: Optional[str],
    zotero_item_key: Optional[str],
    action: str,
    pdf_status: str,
    error: Optional[str],
) -> Dict[str, Any]:
    return {
        "batch_date": batch_date,
        "source_week": rec.get("source_week"),
        "cumulative_id": rec.get("cumulative_id"),
        "title": rec.get("title"),
        "doi": rec.get("doi"),
        "main_category": main_category,
        "target_collection": target_collection,
        "target_collection_key": target_collection_key,
        "zotero_item_key": zotero_item_key,
        "action": action,
        "pdf_status": pdf_status,
        "error": error,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def write_logs(
    root: Path, batch_date: str, entries: List[Dict[str, Any]], counts: Dict[str, int]
) -> None:
    logs_dir = root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{batch_date}.log"
    json_path = logs_dir / f"{batch_date}.json"

    with log_path.open("w", encoding="utf-8") as handle:
        handle.write(f"# Zotero Weekly Sync {batch_date}\n")
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        handle.write(json.dumps({"summary": counts}, ensure_ascii=False) + "\n")

    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(
            {"batch_date": batch_date, "counts": counts, "records": entries},
            handle,
            ensure_ascii=False,
            indent=2,
        )


def update_processed(root: Path, batch_date: str, counts: Dict[str, int]) -> None:
    state_path = root / "processed_batches.json"
    state: Dict[str, Any] = {"batches": {}}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            state = {"batches": {}}
    state.setdefault("batches", {})[batch_date] = {
        "status": "ok",
        "counts": counts,
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def archive_batch(config: Dict[str, Any], inbox: Path, batch_date: str) -> None:
    configured = config.get("google_drive_archive")
    if configured:
        archive_root = Path(os.path.expandvars(str(configured))).expanduser()
    else:
        archive_root = inbox.parent / "已处理"
    dest = archive_root / batch_date
    dest.mkdir(parents=True, exist_ok=True)

    moved = 0
    for file in inbox.iterdir():
        if file.is_file() and file.name.startswith("Zotero_") and batch_date in file.name:
            shutil.move(str(file), str(dest / file.name))
            moved += 1
    print(f"ARCHIVED {moved} file(s) -> {dest}")


def print_counts(counts: Dict[str, int]) -> None:
    for key in COUNT_KEYS:
        if counts.get(key):
            print(f"{key}: {counts[key]}")


# --------------------------------------------------------------------------- #
# New-item creation flow
# --------------------------------------------------------------------------- #


def find_new_item_key(
    rec: Dict[str, Any],
    before_keys: set[str],
    after_top: List[Dict[str, Any]],
    used_keys: set[str],
) -> Optional[str]:
    doi = normalize_doi(rec.get("doi"))
    title = normalize_title(rec.get("title"))
    for item in after_top:
        if item["key"] in before_keys or item["key"] in used_keys:
            continue
        if doi and item["doi_norm"] == doi:
            return item["key"]
    for item in after_top:
        if item["key"] in before_keys or item["key"] in used_keys:
            continue
        if title and item["title_norm"] == title:
            return item["key"]
    return None


def plan_new_attachment(
    config: Dict[str, Any],
    rec: Dict[str, Any],
    session_id: str,
    root: Path,
    batch_date: str,
) -> Dict[str, Any]:
    status = rec.get("pdf_access_status")
    url = rec.get("pdf_url")
    if not url:
        if status == "requires_access":
            return {"pdf": "PDF_REQUIRES_ACCESS", "error": None}
        if status == "unavailable":
            return {"pdf": "PDF_UNAVAILABLE", "error": None}
        return {"pdf": "-", "error": None}
    if status == "requires_access":
        return {"pdf": "PDF_REQUIRES_ACCESS", "error": None}
    if status == "unavailable":
        return {"pdf": "PDF_UNAVAILABLE", "error": None}

    pdf_bytes, pdf_path, error = download_pdf(
        config, url, batch_date, rec.get("title") or "document", root / "downloads"
    )
    if pdf_bytes is None:
        return {"pdf": "-", "error": f"pdf_download_failed: {error}"}

    attachment_status, _headers, _body = connector_save_attachment(
        config,
        session_id,
        rec.get("_connector_id") or "",
        url,
        (rec.get("title") or "document") + ".pdf",
        pdf_bytes,
    )
    if attachment_status in (200, 201):
        return {"pdf": "PDF_ATTACHED", "error": None, "pdf_path": pdf_path}
    return {"pdf": "-", "error": f"saveAttachment HTTP {attachment_status}"}


def classify_existing_pdf(
    rec: Dict[str, Any], has_pdf: bool, in_collection: bool
) -> Tuple[str, str, Optional[str], bool]:
    """Classify an already-existing Zotero item.

    Returns ``(action, pdf_status, error, blocking)``.  PDF access status must
    drive the outcome instead of treating every missing PDF as a review error.
    """
    if not in_collection:
        return "NEEDS_REVIEW", "-", "missing_target_collection", True

    if has_pdf:
        return "EXISTING", "PDF_ALREADY_EXISTS", None, False

    access_status = rec.get("pdf_access_status")
    if access_status == "requires_access":
        return "EXISTING", "PDF_REQUIRES_ACCESS", None, False
    if access_status == "unavailable":
        return "EXISTING", "PDF_UNAVAILABLE", None, False

    return "NEEDS_REVIEW", "-", "missing_pdf", True


# --------------------------------------------------------------------------- #
# Dry run
# --------------------------------------------------------------------------- #


def run_dry(config: Dict[str, Any], batch_date: str, records: List[Dict[str, Any]]) -> int:
    print(f"DRY-RUN batch {batch_date}: {len(records)} record(s)")
    index: Optional[List[Dict[str, Any]]] = None
    try:
        index = build_library_index(config)
    except FatalError as exc:
        print(f"WARNING: library unavailable ({exc}); dedupe skipped")

    counts = new_counts()
    for rec in records:
        category, collection_key, target_id, status, error = classify_record(rec, config)
        pdf_label = pdf_plan_label(rec)
        title = rec.get("title")
        doi = rec.get("doi")
        if status:
            print(f"- [{status}] Title={title!r} DOI={doi!r} Error={error}")
            counts["CLASSIFICATION_ERROR"] += 1
            continue
        if index is None:
            print(
                f"- [UNKNOWN] Title={title!r} DOI={doi!r} Category={category} "
                f"Target={collection_name(config, category)!r} Planned=create-new PDF={pdf_label}"
            )
            continue
        candidates = find_matches(rec, index)
        if len(candidates) > 1:
            print(
                f"- [NEEDS_REVIEW] Title={title!r} DOI={doi!r} Category={category} "
                f"Target={collection_name(config, category)!r} Planned=review-ambiguous PDF={pdf_label}"
            )
            counts["NEEDS_REVIEW"] += 1
        elif len(candidates) == 1:
            existing = candidates[0]
            in_collection = existing["key"] in fetch_collection_top_item_keys(config, collection_key)
            has_pdf = has_pdf_child(config, existing["key"])
            action, pdf_status, error, blocking = classify_existing_pdf(
                rec, has_pdf, in_collection
            )
            counts[action] += 1
            if action == "EXISTING":
                if pdf_status == "PDF_ALREADY_EXISTS":
                    counts["PDF_ALREADY_EXISTS"] += 1
                elif pdf_status == "PDF_REQUIRES_ACCESS":
                    counts["PDF_REQUIRES_ACCESS"] += 1
                elif pdf_status == "PDF_UNAVAILABLE":
                    counts["PDF_UNAVAILABLE"] += 1
            print(
                f"- [{action}] Title={title!r} DOI={doi!r} Category={category} "
                f"Target={collection_name(config, category)!r} "
                f"action={action} pdf_status={pdf_status} error={error or ''}"
            )
        else:
            print(
                f"- [CREATED] Title={title!r} DOI={doi!r} Category={category} "
                f"Target={collection_name(config, category)!r} Planned=create-new PDF={pdf_label}"
            )
            counts["CREATED"] += 1

    print("DRY-RUN summary")
    print_counts(counts)
    return 0


# --------------------------------------------------------------------------- #
# Real run
# --------------------------------------------------------------------------- #


def run_real(
    config: Dict[str, Any],
    root: Path,
    inbox: Path,
    batch_date: str,
    batch_path: Path,
    kind: str,
    records: List[Dict[str, Any]],
) -> int:
    if not zotero_reachable(config):
        print("ZOTERO_NOT_RUNNING")
        return 1

    try:
        index = build_library_index(config)
    except FatalError as exc:
        print(f"ZOTERO_API_ERROR: {exc}")
        return 1

    before_keys = {item["key"] for item in index}
    entries: List[Dict[str, Any]] = []
    counts = new_counts()
    blocking_errors = 0
    pending: List[Dict[str, Any]] = []

    for seq, rec in enumerate(records):
        rec["_seq"] = seq
        category, collection_key, target_id, status, error = classify_record(rec, config)
        if status:
            counts["CLASSIFICATION_ERROR"] += 1
            blocking_errors += 1
            entries.append(
                make_log_entry(
                    rec,
                    batch_date,
                    None,
                    rec.get("target_collection"),
                    None,
                    None,
                    "CLASSIFICATION_ERROR",
                    "-",
                    error,
                )
            )
            continue

        rec["_main_category"] = category
        rec["_collection_key"] = collection_key
        rec["_target_id"] = target_id
        candidates = find_matches(rec, index)

        if len(candidates) > 1:
            counts["NEEDS_REVIEW"] += 1
            blocking_errors += 1
            entries.append(
                make_log_entry(
                    rec,
                    batch_date,
                    category,
                    collection_name(config, category),
                    collection_key,
                    None,
                    "NEEDS_REVIEW",
                    "-",
                    "multiple_dedupe_candidates",
                )
            )
            continue

        if len(candidates) == 1:
            existing = candidates[0]
            in_collection = existing["key"] in fetch_collection_top_item_keys(config, collection_key)
            has_pdf = has_pdf_child(config, existing["key"])
            action, pdf_status, error, blocking = classify_existing_pdf(
                rec, has_pdf, in_collection
            )
            counts[action] += 1
            if action == "EXISTING":
                if pdf_status == "PDF_ALREADY_EXISTS":
                    counts["PDF_ALREADY_EXISTS"] += 1
                elif pdf_status == "PDF_REQUIRES_ACCESS":
                    counts["PDF_REQUIRES_ACCESS"] += 1
                elif pdf_status == "PDF_UNAVAILABLE":
                    counts["PDF_UNAVAILABLE"] += 1
            elif blocking:
                blocking_errors += 1
            entries.append(
                make_log_entry(
                    rec,
                    batch_date,
                    category,
                    collection_name(config, category),
                    collection_key,
                    existing["key"],
                    action,
                    pdf_status,
                    error,
                )
            )
            continue

        pending.append(rec)

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for rec in pending:
        groups.setdefault(rec["_target_id"], []).append(rec)

    results: Dict[int, Dict[str, Any]] = {}
    for target_id, group in groups.items():
        session_id = "codex-" + uuid.uuid4().hex
        for rec in group:
            rec["_connector_id"] = "codex-" + uuid.uuid4().hex

        items = [build_save_item(rec, rec["_connector_id"]) for rec in group]
        save_status, _headers, _body = connector_save_items(config, session_id, items)
        if save_status not in (200, 201):
            for rec in group:
                results[rec["_seq"]] = {
                    "pdf": "-",
                    "error": f"saveItems HTTP {save_status}",
                }
            continue

        update_status, _headers, _body = connector_update_session(config, session_id, target_id, [])
        if update_status not in (200,):
            for rec in group:
                results[rec["_seq"]] = {
                    "pdf": "-",
                    "error": f"updateSession HTTP {update_status}",
                }
            continue

        for rec in group:
            results[rec["_seq"]] = plan_new_attachment(config, rec, session_id, root, batch_date)

    after_top: List[Dict[str, Any]] = []
    try:
        after_top = top_item_map(fetch_all_items(config))
    except FatalError as exc:
        for rec in pending:
            results.setdefault(
                rec["_seq"],
                {"pdf": "-", "error": f"verification_api_error: {exc}"},
            )

    used_keys: set[str] = set()
    for rec in pending:
        result = results.get(rec["_seq"], {"pdf": "-", "error": None})
        if result.get("error") and result.get("action_failed", True):
            counts["FAILED"] += 1
            blocking_errors += 1
            entries.append(
                make_log_entry(
                    rec,
                    batch_date,
                    rec["_main_category"],
                    collection_name(config, rec["_main_category"]),
                    rec["_collection_key"],
                    None,
                    "FAILED",
                    result.get("pdf") or "-",
                    result.get("error"),
                )
            )
            continue

        key = find_new_item_key(rec, before_keys, after_top, used_keys)
        if not key:
            counts["FAILED"] += 1
            blocking_errors += 1
            entries.append(
                make_log_entry(
                    rec,
                    batch_date,
                    rec["_main_category"],
                    collection_name(config, rec["_main_category"]),
                    rec["_collection_key"],
                    None,
                    "FAILED",
                    result.get("pdf") or "-",
                    "item_not_found_after_save",
                )
            )
            continue

        used_keys.add(key)
        in_collection = key in fetch_collection_top_item_keys(config, rec["_collection_key"])
        pdf_status = result.get("pdf") or "-"
        if in_collection:
            counts["CREATED"] += 1
            counts["COLLECTION_ADDED"] += 1
            if pdf_status == "PDF_ATTACHED":
                counts["PDF_ATTACHED"] += 1
            elif pdf_status == "PDF_REQUIRES_ACCESS":
                counts["PDF_REQUIRES_ACCESS"] += 1
            elif pdf_status == "PDF_UNAVAILABLE":
                counts["PDF_UNAVAILABLE"] += 1
            entries.append(
                make_log_entry(
                    rec,
                    batch_date,
                    rec["_main_category"],
                    collection_name(config, rec["_main_category"]),
                    rec["_collection_key"],
                    key,
                    "CREATED",
                    pdf_status,
                    result.get("error"),
                )
            )
        else:
            counts["FAILED"] += 1
            blocking_errors += 1
            entries.append(
                make_log_entry(
                    rec,
                    batch_date,
                    rec["_main_category"],
                    collection_name(config, rec["_main_category"]),
                    rec["_collection_key"],
                    key,
                    "FAILED",
                    pdf_status,
                    "collection_verification_failed",
                )
            )

    write_logs(root, batch_date, entries, counts)
    print(f"BATCH {batch_date} processed ({len(entries)} record log entries)")
    print_counts(counts)

    if blocking_errors == 0:
        archive_batch(config, inbox, batch_date)
        update_processed(root, batch_date, counts)
    else:
        print("ARCHIVE_SKIPPED_ERRORS")
    return 0


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def build_arg_parser() -> argparse.ArgumentParser:
    default_config = Path(__file__).resolve().parent.parent / "config.json"
    parser = argparse.ArgumentParser(description="Zotero Weekly Literature Sync")
    parser.add_argument("--config", default=str(default_config), help="Path to config.json")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read-only plan: do not create items, attach PDFs, move files, or write logs",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    _reconfigure_stdio()
    args = build_arg_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        root = Path(args.config).resolve().parent
        inbox = find_inbox(config)
        if not inbox:
            print("GOOGLE_DRIVE_NOT_SYNCED")
            return 0
        batch = discover_batch(inbox, config)
        if not batch:
            print("NO_BATCH")
            return 0
        batch_date, batch_path, kind = batch
        records = load_records(batch_path, kind)
        if not records:
            print("EMPTY_BATCH")
            return 0
        if args.dry_run:
            return run_dry(config, batch_date, records)
        return run_real(config, root, inbox, batch_date, batch_path, kind, records)
    except BatchControlJsonMissing as exc:
        print(f"BATCH_CONTROL_JSON_MISSING: {exc}")
        return 0
    except FatalError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
