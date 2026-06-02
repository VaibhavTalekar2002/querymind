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
import uuid
from datetime import datetime
import re
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

                if table_name.startswith("sqlite_"):
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


# ── CSV Upload ────────────────────────────────────────────────────────────────

@app.post("/upload-csv")
async def upload_csv(file: UploadFile = File(...)):
    try:
        df = pd.read_csv(file.file)
        table_name = file.filename.replace(".csv", "").replace(" ", "_").replace("(", "").replace(")", "")
        df.to_sql(table_name, con=get_engine(), if_exists="replace", index=False)
        suggestions = [
            f"Show top 10 rows from {table_name}",
            f"Find summary statistics of {table_name}",
            f"Group data by important columns in {table_name}",
            f"Detect missing or null values in {table_name}",
            f"What are trends in {table_name}?"
]
        return {
            "message": "CSV uploaded successfully!",
            "table_name": table_name,
            "row_count": len(df),
            "columns": list(df.columns),
            "suggestions": suggestions,
            "active_db": get_active_db()
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