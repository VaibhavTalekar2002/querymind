from sqlalchemy import create_engine, text
import json
import os
from urllib.parse import unquote


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(BASE_DIR, "databases")
DATASET_DB_DIR = os.path.join(BASE_DIR, "datasets")
CONNECTIONS_FILE = os.path.join(BASE_DIR, "connections.json")

os.makedirs(DB_DIR, exist_ok=True)
os.makedirs(DATASET_DB_DIR, exist_ok=True)

SQLITE_PREFIX = "sqlite:///"

# Runtime-only state. The app now starts with no active database so employees.db,
# chinook.db, and old uploaded files are not auto-mounted on backend startup.
_active_conn_id = None
_connections = {}
_engines = {}


def _is_sqlite_connection(conn):
    return conn.get("type") == "sqlite" and str(conn.get("url", "")).startswith(SQLITE_PREFIX)


def _sqlite_url_path(url):
    return unquote(str(url)[len(SQLITE_PREFIX):])


def _sqlite_url(path):
    return f"{SQLITE_PREFIX}{str(path).replace(os.sep, '/')}"


def _project_relative_sqlite_url(abs_path):
    rel_path = os.path.relpath(abs_path, BASE_DIR)
    return _sqlite_url(rel_path)


def _resolve_project_sqlite_path(path):
    normalized = path.replace("\\", os.sep).replace("/", os.sep)
    if os.path.isabs(normalized):
        return os.path.abspath(normalized)
    return os.path.abspath(os.path.join(BASE_DIR, normalized))


def _is_within_base(abs_path):
    try:
        return os.path.commonpath([BASE_DIR, abs_path]) == BASE_DIR
    except ValueError:
        return False


def _sanitize_sqlite_connection(conn):
    """
    Keep project-owned SQLite URLs portable while avoiding stale absolute paths.

    Connections are runtime-only now, but uploads still pass absolute paths. We
    normalize those into backend-relative URLs so any debugging output remains
    portable and stale Windows machine paths never become the active database.
    """
    if not _is_sqlite_connection(conn):
        return conn

    raw_path = _sqlite_url_path(conn["url"])
    resolved_path = _resolve_project_sqlite_path(raw_path)
    filename = conn.get("filename") or os.path.basename(raw_path.replace("\\", os.sep))

    candidates = []
    if _is_within_base(resolved_path):
        candidates.append(resolved_path)
    if filename:
        candidates.extend([
            os.path.join(DB_DIR, filename),
            os.path.join(DATASET_DB_DIR, filename),
        ])

    for candidate in candidates:
        abs_candidate = os.path.abspath(candidate)
        if _is_within_base(abs_candidate) and os.path.exists(abs_candidate):
            sanitized = dict(conn)
            sanitized["url"] = _project_relative_sqlite_url(abs_candidate)
            sanitized["filename"] = os.path.basename(abs_candidate)
            return sanitized

    if os.path.isabs(raw_path.replace("\\", os.sep)):
        return None

    return conn


def _runtime_url(conn):
    if not _is_sqlite_connection(conn):
        return conn["url"]
    abs_path = _resolve_project_sqlite_path(_sqlite_url_path(conn["url"]))
    return _sqlite_url(abs_path)


def _save_connections(_connections_snapshot=None):
    # Keep the legacy file empty so old employees/chinook/uploaded entries cannot
    # remount themselves on the next restart. Runtime state lives in memory only.
    with open(CONNECTIONS_FILE, "w") as f:
        json.dump({}, f, indent=2)


def get_engine(conn_id=None):
    global _active_conn_id
    if conn_id is None:
        conn_id = _active_conn_id
    if not conn_id:
        raise ValueError("No active database. Upload or connect a database first.")
    if conn_id not in _engines:
        conn = _connections.get(conn_id)
        if not conn:
            raise ValueError(f"Connection '{conn_id}' not found.")
        _engines[conn_id] = create_engine(_runtime_url(conn), echo=False)
    return _engines[conn_id]


def get_active_db():
    if not _active_conn_id:
        return ""
    conn = _connections.get(_active_conn_id, {})
    return conn.get("display", "")


def get_active_conn_id():
    return _active_conn_id or ""


def set_active_connection(conn_id):
    global _active_conn_id
    if conn_id not in _connections:
        raise ValueError(f"Connection '{conn_id}' not found.")
    _active_conn_id = conn_id
    return _connections[conn_id]["display"]


def list_connections():
    return [
        {
            "id": c["id"],
            "name": c["name"],
            "type": c["type"],
            "display": c["display"],
            "is_sqlite_file": c.get("is_sqlite_file", False),
        }
        for c in _connections.values()
    ]


def add_connection(conn_id, name, db_type, url, display, is_sqlite_file=False, filename=None):
    conn = {
        "id": conn_id,
        "name": name,
        "type": db_type,
        "url": url,
        "display": display,
        "is_sqlite_file": is_sqlite_file,
        "filename": filename,
    }
    conn = _sanitize_sqlite_connection(conn) or conn
    _connections[conn_id] = conn
    _save_connections(_connections)


def remove_connection(conn_id):
    global _active_conn_id
    if conn_id in _connections:
        del _connections[conn_id]
        if conn_id in _engines:
            _engines[conn_id].dispose()
            del _engines[conn_id]
        if _active_conn_id == conn_id:
            _active_conn_id = None
        _save_connections(_connections)


def test_connection(url):
    """Test a connection URL. Returns (success, message)."""
    try:
        conn = {"type": "sqlite" if str(url).startswith(SQLITE_PREFIX) else "", "url": url}
        runtime_url = _runtime_url(conn) if _is_sqlite_connection(conn) else url
        connect_args = {"connect_timeout": 5} if "postgresql" in url or "mysql" in url else {}
        engine = create_engine(runtime_url, echo=False, connect_args=connect_args)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True, "Connection successful!"
    except Exception as e:
        return False, str(e)


def save_uploaded_db(filename, data):
    """Save an uploaded .db file and register it for this runtime session."""
    path = os.path.join(DB_DIR, filename)
    with open(path, "wb") as f:
        f.write(data)

    conn_id = filename.replace(".", "_").replace("-", "_")
    url = _project_relative_sqlite_url(path)
    add_connection(
        conn_id=conn_id,
        name=filename,
        db_type="sqlite",
        url=url,
        display=filename,
        is_sqlite_file=True,
        filename=filename,
    )
    _engines[conn_id] = create_engine(_sqlite_url(path), echo=False)
    return conn_id


def list_sqlite_files():
    """List only SQLite DBs mounted in the current runtime session."""
    return [
        c.get("filename") or c.get("name")
        for c in _connections.values()
        if c.get("type") == "sqlite" and c.get("is_sqlite_file")
    ]


_save_connections(_connections)
