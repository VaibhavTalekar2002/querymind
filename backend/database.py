from sqlalchemy import create_engine, text
import os
import json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(BASE_DIR, "databases")
CONNECTIONS_FILE = os.path.join(BASE_DIR, "connections.json")

os.makedirs(DB_DIR, exist_ok=True)

# ── Default SQLite setup ──────────────────────────────────────────────────────
DEFAULT_DB = "employees.db"
_legacy = os.path.join(BASE_DIR, "employees.db")
_new = os.path.join(DB_DIR, "employees.db")
if os.path.exists(_legacy) and not os.path.exists(_new):
    import shutil
    shutil.copy(_legacy, _new)

# ── State ─────────────────────────────────────────────────────────────────────
_active_conn_id = "default"
_engines = {}

# ── Connection registry ───────────────────────────────────────────────────────
# Each connection: { id, name, type, url, is_sqlite_file }

def _load_connections():
    """Load saved connections from disk."""
    if os.path.exists(CONNECTIONS_FILE):
        try:
            with open(CONNECTIONS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass

    # Default SQLite connection
    default = {
        "default": {
            "id": "default",
            "name": "employees.db",
            "type": "sqlite",
            "url": f"sqlite:///{os.path.join(DB_DIR, DEFAULT_DB)}",
            "display": "employees.db",
            "is_sqlite_file": True,
            "filename": DEFAULT_DB
        }
    }
    _save_connections(default)
    return default


def _save_connections(connections):
    with open(CONNECTIONS_FILE, "w") as f:
        json.dump(connections, f, indent=2)


_connections = _load_connections()


# ── Public API ────────────────────────────────────────────────────────────────

def get_engine(conn_id=None):
    global _active_conn_id
    if conn_id is None:
        conn_id = _active_conn_id
    if conn_id not in _engines:
        conn = _connections.get(conn_id)
        if not conn:
            raise ValueError(f"Connection '{conn_id}' not found.")
        _engines[conn_id] = create_engine(conn["url"], echo=False)
    return _engines[conn_id]


def get_active_db():
    conn = _connections.get(_active_conn_id, {})
    return conn.get("display", _active_conn_id)


def get_active_conn_id():
    return _active_conn_id


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
            "is_sqlite_file": c.get("is_sqlite_file", False)
        }
        for c in _connections.values()
    ]


def add_connection(conn_id, name, db_type, url, display, is_sqlite_file=False, filename=None):
    _connections[conn_id] = {
        "id": conn_id,
        "name": name,
        "type": db_type,
        "url": url,
        "display": display,
        "is_sqlite_file": is_sqlite_file,
        "filename": filename
    }
    _save_connections(_connections)


def remove_connection(conn_id):
    if conn_id == "default":
        raise ValueError("Cannot remove the default connection.")
    if conn_id in _connections:
        del _connections[conn_id]
        if conn_id in _engines:
            del _engines[conn_id]
        _save_connections(_connections)


def test_connection(url):
    """Test a connection URL. Returns (success, message)."""
    try:
        engine = create_engine(url, echo=False, connect_args={"connect_timeout": 5} if "postgresql" in url or "mysql" in url else {})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True, "Connection successful!"
    except Exception as e:
        return False, str(e)


def save_uploaded_db(filename, data):
    """Save an uploaded .db file and register it as a connection."""
    path = os.path.join(DB_DIR, filename)
    with open(path, "wb") as f:
        f.write(data)

    conn_id = filename.replace(".", "_").replace("-", "_")
    url = f"sqlite:///{path}"
    add_connection(
        conn_id=conn_id,
        name=filename,
        db_type="sqlite",
        url=url,
        display=filename,
        is_sqlite_file=True,
        filename=filename
    )
    _engines[conn_id] = create_engine(url, echo=False)
    return conn_id


def list_sqlite_files():
    """List all .db files in the databases folder."""
    return [f for f in os.listdir(DB_DIR) if f.endswith(".db")]


# ── Default SQLite sample data ────────────────────────────────────────────────
engine = get_engine("default")


def create_sample_data():
    eng = get_engine("default")
    with eng.begin() as conn:
        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            department TEXT,
            salary INTEGER
        )
        """))
        result = conn.execute(text("SELECT COUNT(*) FROM employees"))
        count = result.fetchone()[0]
        if count == 0:
            conn.execute(text("""
            INSERT INTO employees (name, department, salary)
            VALUES
            ('Rahul', 'IT', 70000),
            ('Amit', 'HR', 50000),
            ('Sneha', 'Finance', 90000)
            """))
            print("Sample data inserted!")
        else:
            print("Employees table already has data. Skipping insert.")


create_sample_data()
