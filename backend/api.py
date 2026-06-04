from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy import text
from database import (
    get_engine, get_active_db, get_active_conn_id,
    set_active_connection, list_connections, add_connection,
    remove_connection, test_connection, save_uploaded_db,
    list_sqlite_files
)
from ai_service import generate_sql, fix_sql, explain_results, process_nl_to_sql
from schema import get_database_schema, get_erd_schema
from fastapi.responses import StreamingResponse
import pandas as pd
import json
import io
import os
import uuid
from datetime import datetime
import re
import hashlib
from fastapi import HTTPException

DANGEROUS_KEYWORDS = [
    "drop", "delete", "truncate", "alter", "update",
    "insert", "create", "replace", "grant", "revoke"
]

def validate_sql(sql: str):

    if not sql:
        raise HTTPException(
            status_code=400,
            detail="No valid SQL generated."
        )

    cleaned = sql.lower().strip()

    # must be SELECT only
    if not cleaned.startswith("select"):
        raise HTTPException(
            status_code=400,
            detail="Only SELECT queries are allowed"
        )

    # block dangerous keywords
    for kw in DANGEROUS_KEYWORDS:
        if re.search(rf"\b{kw}\b", cleaned):
            raise HTTPException(
                status_code=400,
                detail=f"Blocked unsafe keyword: {kw}"
            )

    # prevent multi statement injection
    if cleaned.count(";") > 1:
        raise HTTPException(
            status_code=400,
            detail="Multiple statements not allowed"
        )

    if " into " in cleaned:
        raise HTTPException(
            status_code=400,
            detail="INTO operations not allowed"
        )

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_ROWS = 500
query_history = []


class QueryRequest(BaseModel):
    question: str


class SwitchConnRequest(BaseModel):
    conn_id: str


class NewConnectionRequest(BaseModel):
    name: str
    type: str       # sqlite | postgresql | mysql | mssql
    host: str = ""
    port: int = 0
    database: str = ""
    username: str = ""
    password: str = ""
    url: str = ""   # optional raw URL override


class TestConnectionRequest(BaseModel):
    type: str
    host: str = ""
    port: int = 0
    database: str = ""
    username: str = ""
    password: str = ""
    url: str = ""


def build_url(req):
    """Build SQLAlchemy URL from connection params."""
    if req.url:
        return req.url
    t = req.type.lower()
    if t == "sqlite":
        return f"sqlite:///{req.database}"
    elif t == "postgresql":
        return f"postgresql+psycopg2://{req.username}:{req.password}@{req.host}:{req.port or 5432}/{req.database}"
    elif t == "mysql":
        return f"mysql+pymysql://{req.username}:{req.password}@{req.host}:{req.port or 3306}/{req.database}"
    elif t == "mssql":
        return f"mssql+pyodbc://{req.username}:{req.password}@{req.host}:{req.port or 1433}/{req.database}?driver=ODBC+Driver+17+for+SQL+Server"
    raise ValueError(f"Unsupported database type: {req.type}")


def inject_limit(sql_query):

    if not sql_query:
        return "SELECT 1 LIMIT 1;"

    sql_lower = sql_query.lower().strip()

    if sql_lower.startswith("select") and "limit" not in sql_lower:
        sql_query = sql_query.rstrip(";") + f" LIMIT {MAX_ROWS};"

    return sql_query


def format_result_rows(result):
    columns = result.keys()
    seen = {}
    unique_columns = []
    for col in columns:
        if col in seen:
            seen[col] += 1
            unique_columns.append(f"{col}_{seen[col]}")
        else:
            seen[col] = 1
            unique_columns.append(col)
    rows = []
    for row in result.fetchall():
        rows.append({unique_columns[i]: value for i, value in enumerate(row)})
    return rows


# ── Connection Management ─────────────────────────────────────────────────────

@app.get("/connections")
def get_connections():
    return {
        "connections": list_connections(),
        "active": get_active_conn_id(),
        "active_display": get_active_db()
    }


@app.post("/connections/test")
def test_conn(req: TestConnectionRequest):
    try:
        url = build_url(req)
        success, message = test_connection(url)
        return {"success": success, "message": message}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.post("/connections/add")
def add_conn(req: NewConnectionRequest):
    try:
        url = build_url(req)
        success, message = test_connection(url)
        if not success:
            return {"success": False, "message": f"Connection failed: {message}"}

        conn_id = str(uuid.uuid4())[:8]
        display = f"{req.name} ({req.type})"
        if req.host:
            display = f"{req.name} @ {req.host}"

        add_connection(
            conn_id=conn_id,
            name=req.name,
            db_type=req.type,
            url=url,
            display=display
        )
        return {
            "success": True,
            "message": f"Connected to {display}",
            "conn_id": conn_id,
            "connections": list_connections()
        }
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.post("/connections/switch")
def switch_conn(req: SwitchConnRequest):
    try:
        display = set_active_connection(req.conn_id)
        return {"success": True, "active": req.conn_id, "display": display}
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.delete("/connections/{conn_id}")
def delete_conn(conn_id: str):
    try:
        remove_connection(conn_id)
        return {"success": True, "connections": list_connections()}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── SQLite file upload ────────────────────────────────────────────────────────

@app.post("/connections/upload-db")
async def upload_db(file: UploadFile = File(...)):
    try:
        if not file.filename.endswith(".db"):
            raise HTTPException(status_code=400, detail="Only .db files allowed.")
        data = await file.read()
        conn_id = save_uploaded_db(file.filename, data)
        set_active_connection(conn_id)
        return {
            "success": True,
            "message": f"'{file.filename}' uploaded and connected!",
            "conn_id": conn_id,
            "active": conn_id,
            "display": file.filename,
            "connections": list_connections()
        }
    except Exception as e:
        return {"success": False, "message": str(e)}
    
def safe_execute(conn, sql_query: str):
    """
    Single safety gate for ALL DB execution.
    """
    validate_sql(sql_query)
    return conn.execute(text(sql_query))

# ── Keep old /databases endpoints for backward compat ─────────────────────────

@app.get("/databases")
def get_databases():
    return {
        "databases": list_sqlite_files(),
        "active": get_active_db()
    }


@app.post("/databases/switch")
def switch_database_legacy(data: dict):
    try:
        db_name = data.get("db_name", "")
        # Find connection by filename
        for c in list_connections():
            if c.get("display") == db_name or c.get("name") == db_name:
                display = set_active_connection(c["id"])
                return {"message": f"Switched to '{display}'", "active": display}
        raise HTTPException(status_code=404, detail=f"Database '{db_name}' not found.")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/databases/upload-db")
async def upload_database_legacy(file: UploadFile = File(...)):
    return await upload_db(file)


# ── Schema ────────────────────────────────────────────────────────────────────

@app.get("/schema")
def get_schema():
    try:
        if not get_active_conn_id():
            return {"schema": [], "active_db": ""}

        engine = get_engine()

        db_type = ""
        for c in list_connections():
            if c["id"] == get_active_conn_id():
                db_type = c["type"]
                break

        with engine.connect() as conn:

            # ── Get tables ─────────────────────────────────────

            if db_type == "postgresql":
                tables_result = conn.execute(text("""
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema='public'
                    ORDER BY table_name
                """)).fetchall()

            elif db_type == "mysql":
                tables_result = conn.execute(
                    text("SHOW TABLES")
                ).fetchall()

            elif db_type == "mssql":
                tables_result = conn.execute(text("""
                    SELECT TABLE_NAME
                    FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_TYPE='BASE TABLE'
                """)).fetchall()

            else:
                tables_result = conn.execute(text("""
                    SELECT name
                    FROM sqlite_master
                    WHERE type='table'
                    ORDER BY name
                """)).fetchall()

            schema = []

            # ── Process each table ─────────────────────────────

            for table_row in tables_result:

                table_name = table_row[0]

                # Hide SQLite/system metadata tables from the user-facing schema.
                if table_name.startswith("sqlite_") or table_name.startswith("_"):
                    continue

                # =========================================================
                # SQLITE
                # =========================================================

                if db_type == "sqlite":

                    columns_raw = conn.execute(
                        text(f'PRAGMA table_info("{table_name}")')
                    ).fetchall()

                    fk_raw = conn.execute(
                        text(f'PRAGMA foreign_key_list("{table_name}")')
                    ).fetchall()

                    fk_map = {
                        fk[3]: {
                            "ref_table": fk[2],
                            "ref_col": fk[4]
                        }
                        for fk in fk_raw
                    }

                    indexes_raw = conn.execute(
                        text(f'PRAGMA index_list("{table_name}")')
                    ).fetchall()

                    unique_cols = set()

                    for idx in indexes_raw:
                        if idx[2]:
                            idx_info = conn.execute(
                                text(f'PRAGMA index_info("{idx[1]}")')
                            ).fetchall()

                            for ii in idx_info:
                                unique_cols.add(ii[2])

                    columns = []

                    for col in columns_raw:

                        col_name = col[1]
                        col_type = col[2] or "TEXT"

                        is_pk = bool(col[5])
                        is_fk = col_name in fk_map

                        col_info = {
                            "name": col_name,
                            "type": col_type,
                            "is_pk": is_pk,
                            "is_fk": is_fk,
                            "not_null": bool(col[3]),
                            "unique": col_name in unique_cols,
                            "default": col[4]
                        }

                        if is_fk:
                            col_info["fk_ref_table"] = fk_map[col_name]["ref_table"]
                            col_info["fk_ref_col"] = fk_map[col_name]["ref_col"]

                        columns.append(col_info)

                # =========================================================
                # POSTGRESQL / MYSQL / MSSQL
                # =========================================================

                else:

                    # ── Columns ─────────────────────────────

                    if db_type == "postgresql":

                        cols = conn.execute(text(f"""
                            SELECT
                                column_name,
                                data_type,
                                CASE WHEN is_nullable='NO'
                                    THEN 1 ELSE 0 END as not_null,
                                column_default
                            FROM information_schema.columns
                            WHERE table_name='{table_name}'
                              AND table_schema='public'
                            ORDER BY ordinal_position
                        """)).fetchall()

                    elif db_type == "mysql":

                        cols = conn.execute(text(f"""
                            SELECT
                                COLUMN_NAME,
                                DATA_TYPE,
                                CASE WHEN IS_NULLABLE='NO'
                                    THEN 1 ELSE 0 END,
                                COLUMN_DEFAULT
                            FROM INFORMATION_SCHEMA.COLUMNS
                            WHERE TABLE_NAME='{table_name}'
                            ORDER BY ORDINAL_POSITION
                        """)).fetchall()

                    else:  # MSSQL

                        cols = conn.execute(text(f"""
                            SELECT
                                COLUMN_NAME,
                                DATA_TYPE,
                                0,
                                NULL
                            FROM INFORMATION_SCHEMA.COLUMNS
                            WHERE TABLE_NAME='{table_name}'
                        """)).fetchall()

                    # ── Primary Keys ───────────────────────

                    pk_result = conn.execute(text(f"""
                        SELECT kcu.column_name
                        FROM information_schema.table_constraints tc
                        JOIN information_schema.key_column_usage kcu
                          ON tc.constraint_name = kcu.constraint_name
                        WHERE tc.table_name = '{table_name}'
                          AND tc.constraint_type = 'PRIMARY KEY'
                    """)).fetchall()

                    pk_cols = {row[0] for row in pk_result}

                    # ── Foreign Keys ───────────────────────

                    fk_result = conn.execute(text(f"""
                        SELECT
                            kcu.column_name,
                            ccu.table_name AS foreign_table_name,
                            ccu.column_name AS foreign_column_name
                        FROM information_schema.table_constraints AS tc
                        JOIN information_schema.key_column_usage AS kcu
                          ON tc.constraint_name = kcu.constraint_name
                        JOIN information_schema.constraint_column_usage AS ccu
                          ON ccu.constraint_name = tc.constraint_name
                        WHERE tc.constraint_type = 'FOREIGN KEY'
                          AND tc.table_name = '{table_name}'
                    """)).fetchall()

                    fk_map = {
                        row[0]: {
                            "ref_table": row[1],
                            "ref_col": row[2]
                        }
                        for row in fk_result
                    }

                    # ── Build columns ──────────────────────

                    columns = []

                    for c in cols:

                        col_name = c[0]

                        is_pk = col_name in pk_cols
                        is_fk = col_name in fk_map

                        col_info = {
                            "name": col_name,
                            "type": c[1],
                            "is_pk": is_pk,
                            "is_fk": is_fk,
                            "not_null": bool(c[2]),
                            "unique": False,
                            "default": c[3]
                        }

                        if is_fk:
                            col_info["fk_ref_table"] = fk_map[col_name]["ref_table"]
                            col_info["fk_ref_col"] = fk_map[col_name]["ref_col"]

                        columns.append(col_info)

                # ── Row count ─────────────────────────────

                try:
                    row_count = conn.execute(
                        text(f'SELECT COUNT(*) FROM "{table_name}"')
                    ).fetchone()[0]

                except Exception:
                    row_count = 0

                schema.append({
                    "table": table_name,
                    "columns": columns,
                    "row_count": row_count
                })

        return {
            "schema": schema,
            "active_db": get_active_db()
        }

    except Exception as e:
        return {"error": str(e)}
# ── Query ─────────────────────────────────────────────────────────────────────

@app.post("/generate-sql")
def generate_query(data: QueryRequest):
    try:
        schema = get_database_schema()
        sql_query = generate_sql(data.question)
        sql_query = inject_limit(sql_query)

        auto_fixed = False
        try:
            with get_engine().connect() as conn:
                validate_sql(sql_query)
                result = safe_execute(conn, sql_query)
                rows = format_result_rows(result)
        except Exception as exec_error:
            sql_query = fix_sql(data.question, sql_query, str(exec_error), schema)
            sql_query = inject_limit(sql_query)
            auto_fixed = True
            with get_engine().connect() as conn:
                validate_sql(sql_query)
                result = safe_execute(conn, sql_query)
                rows = format_result_rows(result)

        explanation = ""
        if rows:
            try:
                sample = json.dumps(rows[:10], indent=2)
                explanation = explain_results(data.question, sql_query, sample)
            except Exception:
                explanation = "Could not generate explanation."

        query_history.append({
            "id": len(query_history) + 1,
            "question": data.question,
            "sql": sql_query,
            "row_count": len(rows),
            "auto_fixed": auto_fixed,
            "database": get_active_db(),
            "timestamp": datetime.now().isoformat()
        })

        return {
            "question": data.question,
            "generated_sql": sql_query,
            "auto_fixed": auto_fixed,
            "explanation": explanation,
            "total_rows": len(rows),
            "active_db": get_active_db(),
            "results": rows
        }

    except HTTPException:
        raise
    except ValueError as ve:
     raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        return {"error": str(e)}


# ── History ───────────────────────────────────────────────────────────────────

@app.get("/history")
def get_history():
    return {"history": list(reversed(query_history))}


@app.delete("/history")
def clear_history():
    query_history.clear()
    return {"message": "History cleared"}


# ── CSV / Excel Upload  (isolated DB + incremental layer) ────────────────────

# How the layer system works
# ─────────────────────────────────────────────────────────────────────────────
# Every uploaded file gets its OWN SQLite database under datasets/
# so it never pollutes the active connection (employees.db etc.)
#
# Inside that SQLite file two things are created:
#   1.  <table_name>          – the actual data table
#   2.  _ingest_log           – tracks every upload: batch_id, timestamp,
#                               rows_added, source_file, mode
#
# Upload modes
#   replace   – drop & recreate table (default first upload)
#   append    – add new rows, skip exact duplicates (incremental)
#   overwrite – always replace even if DB already exists
#
# The frontend can pass ?mode=append to do incremental loads.
# ─────────────────────────────────────────────────────────────────────────────

from sqlalchemy import create_engine as _ce

DATASET_DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "datasets")
os.makedirs(DATASET_DB_DIR, exist_ok=True)

# registry: filename_stem -> { db_path, table_name, conn_id }
_csv_registry: dict = {}


def _sanitize_name(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0]
    return re.sub(r"[^\w]", "_", stem).strip("_").lower() or "uploaded_data"


def _get_or_create_csv_engine(table_name: str):
    """Return (engine, db_path) for an isolated per-file SQLite DB."""
    db_path = os.path.join(DATASET_DB_DIR, f"{table_name}.db")
    engine = _ce(f"sqlite:///{db_path}", echo=False)
    return engine, db_path


def _ensure_ingest_log(engine):
    """Create _ingest_log table if it doesn't exist."""
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS _ingest_log (
                batch_id    TEXT PRIMARY KEY,
                source_file TEXT,
                mode        TEXT,
                rows_added  INTEGER,
                total_rows  INTEGER,
                ingested_at TEXT
            )
        """))


def _incremental_append(df: pd.DataFrame, table_name: str, engine) -> int:
    """
    Append only rows that don't already exist.
    Dedup is based on a SHA-256 hash of the entire row (all columns).
    Returns number of NEW rows inserted.
    """
    from sqlalchemy import inspect as _inspect

    # add row hash column for dedup
    df = df.copy()
    df["_row_hash"] = df.apply(
        lambda r: hashlib.sha256(str(tuple(r)).encode()).hexdigest(), axis=1
    )

    inspector = _inspect(engine)
    if table_name not in inspector.get_table_names():
        # first time — just write everything
        df.to_sql(table_name, engine, if_exists="replace", index=False)
        return len(df)

    # load existing hashes only (cheap)
    with engine.connect() as conn:
        try:
            existing = set(
                row[0] for row in
                conn.execute(text(f'SELECT _row_hash FROM "{table_name}"')).fetchall()
            )
        except Exception:
            existing = set()

    new_rows = df[~df["_row_hash"].isin(existing)]

    if len(new_rows) > 0:
        new_rows.to_sql(table_name, engine, if_exists="append", index=False)

    return len(new_rows)


@app.post("/upload-csv")
async def upload_csv(
    file: UploadFile = File(...),
    mode: str = "replace"   # replace | append | overwrite
):
    """
    Upload a CSV or Excel file.

    Each file gets its own isolated SQLite database — it does NOT
    touch the active connection (employees.db etc.).

    Query parameters
    ────────────────
    mode=replace    Drop & recreate table (default for first upload)
    mode=append     Incremental — only insert rows not already present
    mode=overwrite  Always drop & recreate, even if DB exists
    """
    engine = None
    try:
        # ── 1. Read file into memory (fixes timeout on large files) ──────────
        raw_bytes = await file.read()          # fully buffer async → sync safe
        buf = io.BytesIO(raw_bytes)

        filename = file.filename or "upload.csv"
        ext = filename.rsplit(".", 1)[-1].lower()

        if ext == "csv":
            df = pd.read_csv(buf)
        elif ext in ("xlsx", "xls"):
            df = pd.read_excel(buf)
        else:
            return {"error": f"Unsupported file type: .{ext}. Use .csv or .xlsx"}

        if df.empty:
            return {"error": "Uploaded file is empty."}

        # ── 2. Isolated DB per file ──────────────────────────────────────────
        table_name = _sanitize_name(filename)
        engine, db_path = _get_or_create_csv_engine(table_name)
        _ensure_ingest_log(engine)

        # ── 3. Incremental / replace layer ──────────────────────────────────
        batch_id = str(uuid.uuid4())[:12]
        now_iso  = datetime.now().isoformat(timespec="seconds")

        if mode == "append":
            rows_added = _incremental_append(df, table_name, engine)
            write_mode = "append (incremental)"
        else:
            # replace or overwrite — drop & recreate
            df_write = df.copy()
            df_write["_row_hash"] = df_write.apply(
                lambda r: hashlib.sha256(str(tuple(r)).encode()).hexdigest(), axis=1
            )
            df_write.to_sql(table_name, engine, if_exists="replace", index=False)
            rows_added = len(df_write)
            write_mode = mode

        # total rows now in table
        with engine.connect() as conn:
            total_rows = conn.execute(
                text(f'SELECT COUNT(*) FROM "{table_name}"')
            ).fetchone()[0]

        # write ingest log entry
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT OR REPLACE INTO _ingest_log
                    (batch_id, source_file, mode, rows_added, total_rows, ingested_at)
                VALUES
                    (:bid, :src, :mode, :added, :total, :ts)
            """), {
                "bid":   batch_id,
                "src":   filename,
                "mode":  write_mode,
                "added": rows_added,
                "total": total_rows,
                "ts":    now_iso
            })

        # ── 4. Register as a queryable connection ────────────────────────────
        conn_id = f"csv_{table_name}"
        if conn_id not in [c["id"] for c in list_connections()]:
            add_connection(
                conn_id=conn_id,
                name=filename,
                db_type="sqlite",
                url=f"sqlite:///{db_path}",
                display=f"{filename} (uploaded)",
                is_sqlite_file=True,
                filename=f"{table_name}.db"
            )

        # auto-switch to the new DB so queries hit it immediately
        set_active_connection(conn_id)

        # ── 5. Response ──────────────────────────────────────────────────────
        suggestions = [
            f"Show top 10 rows from {table_name}",
            f"Find summary statistics of {table_name}",
            f"Group data by important columns in {table_name}",
            f"Detect missing or null values in {table_name}",
            f"What are the trends in {table_name}?"
        ]

        return {
            "message": f"File '{filename}' uploaded successfully!",
            "table_name":   table_name,
            "db_file":      f"{table_name}.db",
            "conn_id":      conn_id,
            "mode":         write_mode,
            "rows_added":   rows_added,
            "total_rows":   total_rows,
            "batch_id":     batch_id,
            "columns":      [c for c in df.columns],
            "suggestions":  suggestions,
            "active_db":    get_active_db()
        }

    except Exception as e:
        import traceback
        return {"error": str(e), "detail": traceback.format_exc()}
    finally:
        if engine is not None:
            engine.dispose()


@app.get("/upload-csv/log/{table_name}")
def get_ingest_log(table_name: str):
    """Return the full ingest history for an uploaded dataset."""
    try:
        _, db_path = _get_or_create_csv_engine(table_name)
        engine = _ce(f"sqlite:///{db_path}", echo=False)
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT * FROM _ingest_log ORDER BY ingested_at DESC")
            ).fetchall()
            cols = ["batch_id", "source_file", "mode", "rows_added", "total_rows", "ingested_at"]
            return {
                "table_name": table_name,
                "log": [dict(zip(cols, r)) for r in rows]
            }
    except Exception as e:
        return {"error": str(e)}


# ── Export ────────────────────────────────────────────────────────────────────

@app.post("/export-csv")
def export_csv(data: QueryRequest):
    try:
        sql_query = generate_sql(data.question)
        sql_query = inject_limit(sql_query)
        with get_engine().connect() as conn:
            validate_sql(sql_query)
            result = safe_execute(conn, sql_query)
            rows = format_result_rows(result)
        if not rows:
            raise HTTPException(status_code=404, detail="No results to export")
        df = pd.DataFrame(rows)
        output = io.StringIO()
        df.to_csv(output, index=False)
        output.seek(0)
        return StreamingResponse(
            io.BytesIO(output.getvalue().encode()),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=results.csv"}
        )
    except Exception as e:
        return {"error": str(e)}
    
@app.get("/schema/erd")
def get_schema_erd(compact: bool = True):
    try:
        erd = get_erd_schema()

        # ─────────────────────────────────────────────
        # LIGHTWEIGHT MODE (DEFAULT)
        # ─────────────────────────────────────────────
        if compact:
            optimized_nodes = []
            optimized_edges = []

            # Reduce node payload for React rendering
            for node in erd["nodes"]:
                optimized_nodes.append({
                    "id": node["id"],
                    "label": node["data"]["label"],
                    "columns": len(node["data"]["columns"]),
                    "pk_count": len([
                        c for c in node["data"]["columns"] if c.get("is_pk")
                    ]),
                    # layout hints (important for graph engines)
                    "width": 180,
                    "height": max(60, 20 + len(node["data"]["columns"]) * 18)
                })

            # compress edges
            for edge in erd["edges"]:
                optimized_edges.append({
                    "id": edge["id"],
                    "source": edge["source"],
                    "target": edge["target"],
                    "label": edge["label"]
                })

            return {
                "nodes": optimized_nodes,
                "edges": optimized_edges,
                "active_db": get_active_db(),
                "mode": "compact"
            }

        # ─────────────────────────────────────────────
        # FULL MODE (DEBUG / INSPECT)
        # ─────────────────────────────────────────────
        return {
            "nodes": erd["nodes"],
            "edges": erd["edges"],
            "active_db": get_active_db(),
            "mode": "full"
        }

    except Exception as e:
        return {"error": str(e)}
