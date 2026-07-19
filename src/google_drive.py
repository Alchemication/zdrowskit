"""Fetch Auto Export JSON files from Google Drive.

Google Drive folder names are labels and are not used for routing. Callers
provide the stable folder IDs for metrics and workouts, and this module writes
the files into the local layout consumed by :mod:`assembler`::

    Metrics/*.json
    Workouts/*.json
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2 import service_account

logger = logging.getLogger(__name__)

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
MANIFEST_NAME = ".drive-fetch-manifest.json"


class DriveFetchError(RuntimeError):
    """Raised when Drive data cannot be fetched or validated."""


@dataclass(frozen=True)
class DriveItem:
    """Google Drive file metadata."""

    id: str
    name: str
    mime_type: str
    modified_time: str | None
    md5_checksum: str | None
    size: str | None


@dataclass(frozen=True)
class DriveExportFolder:
    """One remote Auto Export folder and its local destination."""

    id: str
    local_name: str
    payload_key: str


@dataclass
class FetchStats:
    """Counters for one Google Drive fetch."""

    folders_seen: int = 0
    json_files_seen: int = 0
    downloaded: int = 0
    skipped: int = 0
    bytes_downloaded: int = 0


def _safe_name(name: str) -> str:
    """Return a filesystem-safe filename for a Drive object.

    Args:
        name: Drive object name.

    Returns:
        A safe local filename.
    """
    safe = name.replace("/", "_").replace("\x00", "_").strip()
    if safe in {"", ".", ".."}:
        return f"_{safe or 'unnamed'}"
    return safe


def _load_manifest(data_dir: Path) -> dict[str, dict[str, str | None]]:
    """Load previous download metadata.

    Args:
        data_dir: Local Auto Export data root.

    Returns:
        Manifest keyed by Drive file ID.

    Raises:
        DriveFetchError: If the manifest is not a JSON object.
    """
    path = data_dir / MANIFEST_NAME
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DriveFetchError(f"Cannot read Drive manifest {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise DriveFetchError(f"Invalid Drive manifest format: {path}")
    return {
        str(file_id): metadata
        for file_id, metadata in raw.items()
        if isinstance(metadata, dict)
    }


def _save_manifest(
    data_dir: Path,
    manifest: dict[str, dict[str, str | None]],
) -> None:
    """Persist download metadata atomically.

    Args:
        data_dir: Local Auto Export data root.
        manifest: Manifest keyed by Drive file ID.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / MANIFEST_NAME
    content = json.dumps(manifest, indent=2, sort_keys=True).encode()
    _write_atomic(path, content)


def _access_token(service_account_path: Path) -> str:
    """Return an OAuth access token for a service account.

    Args:
        service_account_path: Path to the service-account JSON key.

    Returns:
        OAuth bearer token.

    Raises:
        DriveFetchError: If authentication returns no token.
    """
    try:
        credentials = service_account.Credentials.from_service_account_file(
            service_account_path,
            scopes=[DRIVE_READONLY_SCOPE],
        )
        credentials.refresh(Request())
    except Exception as exc:
        raise DriveFetchError(
            f"Google service-account authentication failed: {exc}"
        ) from exc
    if not credentials.token:
        raise DriveFetchError(
            "Google authentication succeeded but returned no access token."
        )
    return credentials.token


def _api_json(
    path: str,
    *,
    token: str,
    params: dict[str, str],
    timeout: float,
) -> dict:
    """Call a Google Drive JSON endpoint.

    Args:
        path: API path beginning with ``/``.
        token: OAuth bearer token.
        params: Query-string parameters.
        timeout: HTTP timeout in seconds.

    Returns:
        Parsed response object.

    Raises:
        DriveFetchError: If Google returns an HTTP error or invalid JSON.
    """
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{DRIVE_API_BASE}{path}?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise DriveFetchError(
            _format_google_http_error("Drive API request", exc)
        ) from exc
    except (
        urllib.error.URLError,
        TimeoutError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise DriveFetchError(f"Drive API request failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise DriveFetchError("Drive API returned an invalid response object.")
    return payload


def _download_blob(file_id: str, *, token: str, timeout: float) -> bytes:
    """Download one Drive blob.

    Args:
        file_id: Google Drive file ID.
        token: OAuth bearer token.
        timeout: HTTP timeout in seconds.

    Returns:
        File bytes.

    Raises:
        DriveFetchError: If the download fails.
    """
    query = urllib.parse.urlencode({"alt": "media"})
    request = urllib.request.Request(
        f"{DRIVE_API_BASE}/files/{urllib.parse.quote(file_id)}?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.read()
    except urllib.error.HTTPError as exc:
        raise DriveFetchError(_format_google_http_error("Drive download", exc)) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise DriveFetchError(f"Drive download failed: {exc}") from exc


def _format_google_http_error(action: str, exc: urllib.error.HTTPError) -> str:
    """Return a concise message for a Google JSON HTTP error.

    Args:
        action: Human-readable action label.
        exc: HTTP error raised by urllib.

    Returns:
        One-line diagnostic, including Google's activation URL when present.
    """
    detail = exc.read().decode("utf-8", errors="replace")
    try:
        payload = json.loads(detail)
    except json.JSONDecodeError:
        return f"{action} failed: HTTP {exc.code}: {detail}"

    error = payload.get("error", {})
    message = error.get("message", detail)
    activation_url = None
    for item in error.get("details", []):
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata", {})
        if isinstance(metadata, dict) and metadata.get("activationUrl"):
            activation_url = metadata["activationUrl"]
            break

    suffix = f" Enable it: {activation_url}" if activation_url else ""
    return f"{action} failed: HTTP {exc.code}: {message}.{suffix}"


def _children(folder_id: str, *, token: str, timeout: float) -> list[DriveItem]:
    """List direct children of a Drive folder.

    Args:
        folder_id: Google Drive folder ID.
        token: OAuth bearer token.
        timeout: HTTP timeout in seconds.

    Returns:
        Direct children sorted by name.
    """
    query = f"'{folder_id}' in parents and trashed = false"
    fields = "nextPageToken,files(id,name,mimeType,modifiedTime,md5Checksum,size)"
    page_token: str | None = None
    items: list[DriveItem] = []

    while True:
        params = {
            "q": query,
            "fields": fields,
            "pageSize": "1000",
            "orderBy": "name_natural",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if page_token:
            params["pageToken"] = page_token

        response = _api_json("/files", token=token, params=params, timeout=timeout)
        for raw in response.get("files", []):
            items.append(
                DriveItem(
                    id=raw["id"],
                    name=raw["name"],
                    mime_type=raw["mimeType"],
                    modified_time=raw.get("modifiedTime"),
                    md5_checksum=raw.get("md5Checksum"),
                    size=raw.get("size"),
                )
            )

        page_token = response.get("nextPageToken")
        if not page_token:
            return items


def _manifest_matches(
    *,
    item: DriveItem,
    target: Path,
    metadata: dict[str, str | None] | None,
) -> bool:
    """Return whether an existing local file matches Drive metadata.

    Args:
        item: Drive file metadata.
        target: Local destination path.
        metadata: Previous manifest entry.

    Returns:
        True when the local file can be skipped.
    """
    if metadata is None or not target.exists():
        return False
    if item.size is not None and target.stat().st_size != int(item.size):
        return False
    if item.md5_checksum:
        return metadata.get("md5Checksum") == item.md5_checksum
    return (
        metadata.get("modifiedTime") == item.modified_time
        and metadata.get("size") == item.size
    )


def _record_metadata(item: DriveItem, relative_path: Path) -> dict[str, str | None]:
    """Build a manifest entry for one downloaded file.

    Args:
        item: Drive file metadata.
        relative_path: Destination relative to the local data root.

    Returns:
        JSON-serialisable manifest entry.
    """
    return {
        "name": item.name,
        "path": str(relative_path),
        "modifiedTime": item.modified_time,
        "md5Checksum": item.md5_checksum,
        "size": item.size,
    }


def _write_atomic(target: Path, content: bytes) -> None:
    """Write bytes to a target through a temporary sibling.

    Args:
        target: Destination path.
        content: File bytes.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(f"{target.suffix}.tmp")
    tmp.write_bytes(content)
    tmp.replace(target)


def _validate_export_json(content: bytes, *, expected_key: str, name: str) -> None:
    """Validate that a downloaded file has the expected Auto Export schema.

    Args:
        content: Downloaded JSON bytes.
        expected_key: Expected list under ``data``.
        name: Display name used in errors.

    Raises:
        DriveFetchError: If the JSON or expected payload is invalid.
    """
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DriveFetchError(f"Drive file {name!r} is not valid JSON: {exc}") from exc

    data = payload.get("data") if isinstance(payload, dict) else None
    records = data.get(expected_key) if isinstance(data, dict) else None
    if not isinstance(records, list):
        raise DriveFetchError(
            f"Drive file {name!r} does not contain data.{expected_key}; "
            "check that the Metrics and Workouts folder IDs are not swapped."
        )


def fetch_health_export(
    *,
    metrics_folder_id: str,
    workouts_folder_id: str,
    data_dir: Path,
    service_account_path: Path,
    force: bool = False,
    dry_run: bool = False,
    timeout: float = 30.0,
) -> FetchStats:
    """Fetch metric and workout JSON files into an importable local cache.

    Args:
        metrics_folder_id: Drive ID of the Auto Export metrics folder.
        workouts_folder_id: Drive ID of the Auto Export workouts folder.
        data_dir: Local data root that will contain Metrics and Workouts.
        service_account_path: Path to the service-account JSON key.
        force: Whether to redownload files already current in the manifest.
        dry_run: Whether to list work without downloading or writing files.
        timeout: HTTP timeout in seconds.

    Returns:
        Aggregate fetch counters.

    Raises:
        DriveFetchError: If credentials, Drive access, or an export payload is invalid.
    """
    if not service_account_path.is_file():
        raise DriveFetchError(
            f"Google service-account JSON not found: {service_account_path}"
        )
    if metrics_folder_id == workouts_folder_id:
        raise DriveFetchError(
            "Metrics and Workouts must use different Drive folder IDs."
        )

    folders = (
        DriveExportFolder(metrics_folder_id, "Metrics", "metrics"),
        DriveExportFolder(workouts_folder_id, "Workouts", "workouts"),
    )
    token = _access_token(service_account_path)
    manifest = _load_manifest(data_dir)
    stats = FetchStats()
    seen_targets: dict[Path, str] = {}

    for folder in folders:
        stats.folders_seen += 1
        logger.debug("Listing Drive folder for %s", folder.local_name)
        folder_json_count = 0
        for item in _children(folder.id, token=token, timeout=timeout):
            if item.mime_type == FOLDER_MIME_TYPE:
                logger.debug("Skipping nested Drive folder: %s", item.name)
                continue

            safe_name = _safe_name(item.name)
            if not safe_name.lower().endswith(".json"):
                logger.debug("Skipping non-JSON Drive file: %s", item.name)
                continue

            folder_json_count += 1
            stats.json_files_seen += 1
            relative_path = Path(folder.local_name) / safe_name
            target = data_dir / relative_path
            previous_id = seen_targets.get(relative_path)
            if previous_id is not None and previous_id != item.id:
                raise DriveFetchError(
                    f"Drive folder contains duplicate JSON name {item.name!r}; "
                    "rename or remove one copy before importing."
                )
            seen_targets[relative_path] = item.id

            if not force and _manifest_matches(
                item=item,
                target=target,
                metadata=manifest.get(item.id),
            ):
                stats.skipped += 1
                logger.debug("Already current: %s", relative_path)
                continue

            if dry_run:
                stats.downloaded += 1
                print(f"would download {relative_path}")
                continue

            logger.info("Downloading %s", relative_path)
            content = _download_blob(item.id, token=token, timeout=timeout)
            _validate_export_json(
                content,
                expected_key=folder.payload_key,
                name=item.name,
            )
            _write_atomic(target, content)
            manifest[item.id] = _record_metadata(item, relative_path)
            stats.downloaded += 1
            stats.bytes_downloaded += len(content)

        if folder_json_count == 0:
            raise DriveFetchError(
                f"No JSON files found in the configured {folder.local_name} Drive folder."
            )

    if not dry_run:
        _save_manifest(data_dir, manifest)
    return stats


def print_fetch_summary(stats: FetchStats, *, dry_run: bool, data_dir: Path) -> None:
    """Print a concise fetch result for a CLI caller.

    Args:
        stats: Completed fetch counters.
        dry_run: Whether no writes were performed.
        data_dir: Local data root.
    """
    action = "Would download" if dry_run else "Downloaded"
    print(
        f"{action} {stats.downloaded} file(s); "
        f"skipped {stats.skipped}; "
        f"saw {stats.json_files_seen} JSON file(s) in {stats.folders_seen} folder(s)."
    )
    if not dry_run:
        print(f"Data directory: {data_dir}")
