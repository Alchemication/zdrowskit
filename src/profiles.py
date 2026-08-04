"""Profile configuration, paths, creation, adoption, and Telegram routing."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import APP_HOME

PROFILE_NAME_RE = re.compile(r"^[a-z0-9_-]+$")
DRIVE_FOLDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
PROFILES_FILE = APP_HOME / "profiles.toml"
PROFILES_DIR = APP_HOME / "profiles"
CONTEXT_FILENAMES = (
    "me.md",
    "strategy.md",
    "log.md",
    "history.md",
    "coach_feedback.md",
    "baselines.md",
)


class ProfileConfigError(ValueError):
    """Raised when the profile roster is missing or invalid."""


@dataclass(frozen=True)
class Profile:
    """One isolated health profile and its configured import identity."""

    name: str
    telegram_id: int
    root: Path
    operator: bool = False
    enabled: bool = True
    import_source: str = "http"
    drive_metrics_folder_id: str | None = None
    drive_workouts_folder_id: str | None = None

    @property
    def db(self) -> Path:
        """Return this profile's health database path."""
        return self.root / "health.db"

    @property
    def context(self) -> Path:
        """Return this profile's context directory."""
        return self.root / "ContextFiles"

    @property
    def reports(self) -> Path:
        """Return this profile's generated reports directory."""
        return self.root / "Reports"

    @property
    def nudges(self) -> Path:
        """Return this profile's generated nudges directory."""
        return self.root / "Nudges"

    @property
    def drive_cache(self) -> Path:
        """Return this profile's Google Drive cache directory."""
        return self.root / "Imports" / "google-drive"

    @property
    def http_cache(self) -> Path:
        """Return this profile's bounded HTTP ingest directory."""
        return self.root / "Imports" / "http"

    @property
    def import_archive(self) -> Path:
        """Return this profile's daily raw-payload archive directory.

        Deliberately outside :attr:`http_cache`, which holds only the bounded
        latest payload per kind. The archive keeps one snapshot per kind per
        day so a future parser fix can be replayed without a fresh phone
        export.
        """
        return self.root / "Imports" / "archive"

    @property
    def state(self) -> Path:
        """Return this profile's daemon state path."""
        return self.root / "daemon_state.json"

    @property
    def notification_prefs(self) -> Path:
        """Return this profile's notification preferences path."""
        return self.root / "notification_prefs.json"

    @property
    def model_prefs(self) -> Path:
        """Return this profile's model preferences path."""
        return self.root / "model_prefs.json"


def validate_profile_name(name: str) -> str:
    """Validate and return a filesystem-safe profile name."""
    if not PROFILE_NAME_RE.fullmatch(name):
        raise ProfileConfigError(
            f"Invalid profile name {name!r}; use lowercase letters, digits, "
            "underscores, or hyphens."
        )
    return name


def _optional_folder_id(raw: Any, field: str, name: str) -> str | None:
    """Validate one optional Drive folder ID."""
    if raw is None:
        return None
    if not isinstance(raw, str) or not DRIVE_FOLDER_ID_RE.fullmatch(raw):
        raise ProfileConfigError(
            f"profiles.{name}.{field} must be a valid Google Drive folder ID."
        )
    return raw


def load_profiles(path: Path = PROFILES_FILE) -> dict[str, Profile]:
    """Load and validate the complete profile roster from TOML."""
    if not path.exists():
        raise ProfileConfigError(
            f"Profile roster not found: {path}. Run 'profile adopt NAME' for an "
            "existing installation or 'profile add NAME --telegram-id ID --operator'."
        )
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProfileConfigError(
            f"Could not read profile roster {path}: {exc}"
        ) from exc

    raw_profiles = raw.get("profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise ProfileConfigError(
            f"{path} must contain at least one [profiles.NAME] table."
        )

    roots = path.parent / "profiles"
    profiles: dict[str, Profile] = {}
    telegram_ids: set[int] = set()
    for raw_name, values in raw_profiles.items():
        name = validate_profile_name(str(raw_name))
        if not isinstance(values, dict):
            raise ProfileConfigError(f"profiles.{name} must be a TOML table.")
        telegram_id = values.get("telegram_id")
        if not isinstance(telegram_id, int) or isinstance(telegram_id, bool):
            raise ProfileConfigError(f"profiles.{name}.telegram_id must be an integer.")
        if telegram_id in telegram_ids:
            raise ProfileConfigError(
                f"Telegram ID {telegram_id} is assigned more than once."
            )
        telegram_ids.add(telegram_id)

        operator = values.get("operator", False)
        enabled = values.get("enabled", True)
        if not isinstance(operator, bool) or not isinstance(enabled, bool):
            raise ProfileConfigError(
                f"profiles.{name}.operator and enabled must be booleans."
            )
        source = values.get("import_source", "http")
        if source not in {"http", "local", "google-drive"}:
            raise ProfileConfigError(
                f"profiles.{name}.import_source must be 'http', 'local', or "
                "'google-drive'."
            )
        if source == "local" and not operator:
            raise ProfileConfigError(
                f"profiles.{name} uses local import, which is operator-only."
            )

        metrics_id = _optional_folder_id(
            values.get("drive_metrics_folder_id"),
            "drive_metrics_folder_id",
            name,
        )
        workouts_id = _optional_folder_id(
            values.get("drive_workouts_folder_id"),
            "drive_workouts_folder_id",
            name,
        )
        profiles[name] = Profile(
            name=name,
            telegram_id=telegram_id,
            root=roots / name,
            operator=operator,
            enabled=enabled,
            import_source=source,
            drive_metrics_folder_id=metrics_id,
            drive_workouts_folder_id=workouts_id,
        )

    operators = [profile for profile in profiles.values() if profile.operator]
    if len(operators) != 1:
        raise ProfileConfigError(
            f"{path} must define exactly one operator profile; found {len(operators)}."
        )
    return profiles


def operator_profile(profiles: dict[str, Profile]) -> Profile:
    """Return the roster's sole operator profile."""
    return next(profile for profile in profiles.values() if profile.operator)


def enabled_profiles(profiles: dict[str, Profile]) -> dict[str, Profile]:
    """Return only enabled profiles, preserving roster order."""
    return {name: profile for name, profile in profiles.items() if profile.enabled}


def resolve_profile(
    update: dict[str, Any], profiles: dict[str, Profile]
) -> Profile | None:
    """Resolve an authorized enabled profile from a private Telegram update."""
    message = update.get("message")
    if not isinstance(message, dict):
        callback = update.get("callback_query")
        if not isinstance(callback, dict):
            return None
        message = callback.get("message")
        sender = callback.get("from")
    else:
        sender = message.get("from")
    if not isinstance(message, dict) or not isinstance(sender, dict):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict) or chat.get("type") != "private":
        return None
    chat_id = chat.get("id")
    sender_id = sender.get("id")
    if (
        not isinstance(chat_id, int)
        or not isinstance(sender_id, int)
        or chat_id != sender_id
    ):
        return None
    return next(
        (
            profile
            for profile in profiles.values()
            if profile.enabled and profile.telegram_id == sender_id
        ),
        None,
    )


def _toml_profile_section(
    name: str,
    telegram_id: int,
    *,
    operator: bool,
    import_source: str,
    metrics_id: str | None = None,
    workouts_id: str | None = None,
) -> str:
    """Render one minimal profile table."""
    lines = [f"[profiles.{name}]", f"telegram_id = {telegram_id}"]
    if operator:
        lines.append("operator = true")
    if import_source != "http":
        lines.append(f'import_source = "{import_source}"')
    if metrics_id:
        lines.append(f'drive_metrics_folder_id = "{metrics_id}"')
    if workouts_id:
        lines.append(f'drive_workouts_folder_id = "{workouts_id}"')
    return "\n".join(lines) + "\n"


def _create_profile_tree(profile: Profile) -> None:
    """Create private profile directories and initialize its database."""
    if profile.root.exists():
        raise ProfileConfigError(f"Profile directory already exists: {profile.root}")
    profile.context.mkdir(parents=True, mode=0o700)
    profile.reports.mkdir(mode=0o700)
    profile.nudges.mkdir(mode=0o700)
    profile.drive_cache.mkdir(parents=True, mode=0o700)
    profile.http_cache.mkdir(parents=True, mode=0o700)
    templates = Path(__file__).resolve().parent / "templates" / "context"
    for filename in CONTEXT_FILENAMES:
        source = templates / filename
        destination = profile.context / filename
        if source.exists():
            shutil.copy2(source, destination)
        else:
            destination.write_text("", encoding="utf-8")
    from store import open_db

    conn = open_db(profile.db)
    conn.close()
    os.chmod(profile.root, 0o700)


def _backup_sqlite_database(source: Path, destination: Path) -> None:
    """Copy a consistent SQLite snapshot, including committed WAL contents."""
    source_conn = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(destination_conn)
    finally:
        destination_conn.close()
        source_conn.close()


def add_profile(
    name: str,
    telegram_id: int,
    *,
    operator: bool = False,
    path: Path = PROFILES_FILE,
    import_source: str = "http",
    metrics_id: str | None = None,
    workouts_id: str | None = None,
) -> Profile:
    """Append a roster entry and create its isolated filesystem tree."""
    name = validate_profile_name(name)
    if (
        not isinstance(telegram_id, int)
        or isinstance(telegram_id, bool)
        or telegram_id <= 0
    ):
        raise ProfileConfigError("Telegram ID must be a positive integer.")
    if import_source not in {"http", "local", "google-drive"}:
        raise ProfileConfigError(
            "Import source must be 'http', 'local', or 'google-drive'."
        )
    if import_source == "local" and not operator:
        raise ProfileConfigError("Local import is operator-only.")
    metrics_id = _optional_folder_id(metrics_id, "drive_metrics_folder_id", name)
    workouts_id = _optional_folder_id(workouts_id, "drive_workouts_folder_id", name)
    if path.exists():
        existing = load_profiles(path)
        if name in existing:
            raise ProfileConfigError(f"Profile {name!r} already exists.")
        if telegram_id in {profile.telegram_id for profile in existing.values()}:
            raise ProfileConfigError(f"Telegram ID {telegram_id} is already assigned.")
        if operator:
            raise ProfileConfigError("The roster already has an operator profile.")
    elif not operator:
        raise ProfileConfigError("The first profile must use --operator.")

    profile = Profile(
        name=name,
        telegram_id=telegram_id,
        root=path.parent / "profiles" / name,
        operator=operator,
        import_source=import_source,
        drive_metrics_folder_id=metrics_id,
        drive_workouts_folder_id=workouts_id,
    )
    _create_profile_tree(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = "\n" if path.exists() and path.stat().st_size else ""
    with path.open("a", encoding="utf-8") as roster:
        roster.write(
            prefix
            + _toml_profile_section(
                name,
                telegram_id,
                operator=operator,
                import_source=import_source,
                metrics_id=metrics_id,
                workouts_id=workouts_id,
            )
        )
    return profile


def set_profile_source(
    name: str,
    import_source: str,
    *,
    path: Path = PROFILES_FILE,
) -> Profile:
    """Atomically change one profile's import source in the TOML roster."""
    if import_source not in {"http", "local", "google-drive"}:
        raise ProfileConfigError(
            "Import source must be 'http', 'local', or 'google-drive'."
        )
    profiles = load_profiles(path)
    profile = profiles.get(name)
    if profile is None:
        raise ProfileConfigError(f"Unknown profile {name!r}.")
    if import_source == "local" and not profile.operator:
        raise ProfileConfigError("Local import is operator-only.")
    text = path.read_text(encoding="utf-8")
    header = f"[profiles.{name}]"
    start = text.find(header)
    if start < 0:
        raise ProfileConfigError(f"Could not locate {header} in {path}.")
    next_section = text.find("\n[", start + len(header))
    end = len(text) if next_section < 0 else next_section + 1
    section = text[start:end]
    source_line = re.compile(
        r'^(?P<indent>[ \t]*)import_source\s*=\s*"[^"]*"[ \t]*$',
        re.MULTILINE,
    )
    replacement = f'import_source = "{import_source}"'
    match = source_line.search(section)
    if match:
        updated_section = source_line.sub(
            f"{match.group('indent')}{replacement}", section, count=1
        )
    else:
        first_newline = section.find("\n")
        updated_section = (
            section[: first_newline + 1]
            + replacement
            + "\n"
            + section[first_newline + 1 :]
        )
    updated = text[:start] + updated_section + text[end:]
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(updated, encoding="utf-8")
        os.chmod(temp, path.stat().st_mode & 0o777)
        load_profiles(temp)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    updated_profile = load_profiles(path)[name]
    updated_profile.http_cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    return updated_profile


def adopt_existing(
    name: str,
    *,
    telegram_id: int,
    dry_run: bool = False,
    path: Path = PROFILES_FILE,
) -> list[tuple[Path, Path]]:
    """Copy a legacy single-profile installation into an operator profile.

    The legacy files remain in place as a rollback copy. The roster is written
    only after the copied database and required context files validate.
    """
    name = validate_profile_name(name)
    if path.exists():
        raise ProfileConfigError(f"Profile roster already exists: {path}")
    root = path.parent
    destination_root = root / "profiles" / name
    if destination_root.exists():
        raise ProfileConfigError(
            f"Refusing to overwrite existing profile directory: {destination_root}"
        )
    legacy_db = root / "health.db"
    legacy_context = root / "ContextFiles"
    if not legacy_db.exists():
        raise ProfileConfigError(f"Legacy health database not found: {legacy_db}")
    for filename in ("me.md", "strategy.md"):
        source = legacy_context / filename
        if not source.exists():
            raise ProfileConfigError(
                f"Required legacy context file not found: {source}"
            )

    import_source = os.environ.get("ZDROWSKIT_IMPORT_SOURCE", "local").strip()
    if import_source not in {"http", "local", "google-drive"}:
        raise ProfileConfigError(
            "ZDROWSKIT_IMPORT_SOURCE must be 'http', 'local', or 'google-drive'."
        )
    metrics_id = _optional_folder_id(
        os.environ.get("ZDROWSKIT_GOOGLE_DRIVE_METRICS_FOLDER_ID"),
        "drive_metrics_folder_id",
        name,
    )
    workouts_id = _optional_folder_id(
        os.environ.get("ZDROWSKIT_GOOGLE_DRIVE_WORKOUTS_FOLDER_ID"),
        "drive_workouts_folder_id",
        name,
    )

    mappings: list[tuple[Path, Path]] = []
    candidates = [
        (legacy_db, destination_root / "health.db"),
        (legacy_context, destination_root / "ContextFiles"),
        (root / "Reports", destination_root / "Reports"),
        (root / "Nudges", destination_root / "Nudges"),
        (
            root / "notification_prefs.json",
            destination_root / "notification_prefs.json",
        ),
        (root / "model_prefs.json", destination_root / "model_prefs.json"),
        (root / ".daemon_state.json", destination_root / "daemon_state.json"),
    ]
    if import_source == "google-drive":
        from config import resolve_google_drive_data_dir

        candidates.append(
            (
                resolve_google_drive_data_dir(None),
                destination_root / "Imports" / "google-drive",
            )
        )
    mappings.extend(
        (source, destination) for source, destination in candidates if source.exists()
    )
    if dry_run:
        return mappings

    destination_root.mkdir(parents=True, mode=0o700)
    for source, destination in mappings:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source == legacy_db:
            _backup_sqlite_database(source, destination)
        elif source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
    for directory in (
        destination_root / "Reports",
        destination_root / "Nudges",
        destination_root / "Imports" / "google-drive",
        destination_root / "Imports" / "http",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    from store import open_db

    try:
        conn = open_db(destination_root / "health.db")
        conn.execute("SELECT 1").fetchone()
        conn.close()
        for filename in ("me.md", "strategy.md"):
            if not (destination_root / "ContextFiles" / filename).is_file():
                raise ProfileConfigError(
                    f"Copied profile is missing ContextFiles/{filename}."
                )
    except Exception:
        shutil.rmtree(destination_root)
        raise

    path.write_text(
        _toml_profile_section(
            name,
            telegram_id,
            operator=True,
            import_source=import_source,
            metrics_id=metrics_id,
            workouts_id=workouts_id,
        ),
        encoding="utf-8",
    )
    os.chmod(destination_root, 0o700)
    return mappings


def cmd_profile(args: Any) -> None:
    """Handle profile creation and legacy adoption CLI operations."""
    if args.profile_cmd == "add":
        profile = add_profile(
            args.name,
            args.telegram_id,
            operator=args.operator,
            import_source=args.source,
            metrics_id=args.google_drive_metrics_folder_id,
            workouts_id=args.google_drive_workouts_folder_id,
        )
        print(f"Created profile {profile.name}: {profile.root}")
        return
    if args.profile_cmd == "source":
        profile = set_profile_source(args.name, args.source)
        print(f"Profile {profile.name} now imports from {profile.import_source}.")
        print("Restart the daemon for this change to take effect.")
        return
    if args.profile_cmd == "adopt":
        telegram_id = args.telegram_id
        if telegram_id is None:
            raw = os.environ.get("TELEGRAM_CHAT_ID")
            if not raw:
                raise ProfileConfigError(
                    "Telegram ID is required. Pass --telegram-id or set "
                    "TELEGRAM_CHAT_ID for the legacy installation."
                )
            try:
                telegram_id = int(raw)
            except ValueError as exc:
                raise ProfileConfigError(
                    "TELEGRAM_CHAT_ID must be an integer."
                ) from exc
        mappings = adopt_existing(
            args.name,
            telegram_id=telegram_id,
            dry_run=args.dry_run,
        )
        verb = "Would copy" if args.dry_run else "Copied"
        for source, destination in mappings:
            print(f"{verb}: {source} -> {destination}")
        if args.dry_run:
            print("Dry run only; no files changed.")
        else:
            print(
                f"Adopted {args.name}. Legacy files remain in place for rollback; "
                f"remove them only after verifying {PROFILES_FILE}."
            )


def resolve_cli_profile(
    name: str | None,
    *,
    db: str | None,
    roster_path: Path = PROFILES_FILE,
    require_existing: bool = True,
) -> tuple[Profile | None, Path]:
    """Resolve a CLI database, with an explicit ``--db`` taking precedence."""
    if db:
        db_path = Path(db).expanduser().resolve()
        if require_existing and not db_path.exists():
            raise ProfileConfigError(
                f"Database does not exist: {db_path}. Use 'profile add' to create "
                "a profile database."
            )
        return None, db_path
    profiles = load_profiles(roster_path)
    if name:
        try:
            profile = profiles[name]
        except KeyError as exc:
            raise ProfileConfigError(
                f"Unknown profile {name!r} in {roster_path}."
            ) from exc
    else:
        profile = operator_profile(profiles)
    if require_existing and not profile.db.exists():
        raise ProfileConfigError(
            f"Database for profile {profile.name!r} does not exist: {profile.db}. "
            "Run 'profile add' or restore the profile directory."
        )
    return profile, profile.db
