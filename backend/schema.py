from database import get_engine
from sqlalchemy import inspect


# ─────────────────────────────────────────────────────────────
# TEXT SCHEMA (for AI / SQL generation)
# ─────────────────────────────────────────────────────────────

def get_database_schema():
    schema_text = ""

    engine = get_engine()
    inspector = inspect(engine)

    tables = inspector.get_table_names()

    for table_name in tables:

        schema_text += f"\nTABLE: {table_name}\n"

        columns = inspector.get_columns(table_name)

        pk_columns = inspector.get_pk_constraint(table_name).get(
            "constrained_columns", []
        )

        foreign_keys = inspector.get_foreign_keys(table_name)

        fk_columns = {
            fk["constrained_columns"][0]: fk
            for fk in foreign_keys
            if fk.get("constrained_columns")
        }

        schema_text += "Columns:\n"

        for col in columns:

            col_name = col["name"]
            col_type = str(col["type"])

            nullable = col.get("nullable", True)

            is_pk = col_name in pk_columns
            is_fk = col_name in fk_columns

            pk_text = " PRIMARY KEY" if is_pk else ""
            fk_text = " FOREIGN KEY" if is_fk else ""
            nn_text = " NOT NULL" if not nullable else ""

            schema_text += (
                f" - {col_name} ({col_type})"
                f"{pk_text}{fk_text}{nn_text}\n"
            )

        if foreign_keys:
            schema_text += "Foreign Keys:\n"

            for fk in foreign_keys:
                constrained_cols = fk.get("constrained_columns", [])
                referred_cols = fk.get("referred_columns", [])
                referred_table = fk.get("referred_table")

                if constrained_cols and referred_cols:
                    schema_text += (
                        f" - {constrained_cols[0]} "
                        f"→ {referred_table}.{referred_cols[0]}\n"
                    )

    return schema_text


# ─────────────────────────────────────────────────────────────
# ERD SCHEMA (FRONTEND READY)
# ─────────────────────────────────────────────────────────────

def get_erd_schema():
    engine = get_engine()
    inspector = inspect(engine)

    tables = inspector.get_table_names()

    nodes = []
    edges = []

    for table in tables:

        # ── Columns ───────────────────────────────
        columns = inspector.get_columns(table)

        pk_cols = inspector.get_pk_constraint(table).get(
            "constrained_columns", []
        )

        fk_list = inspector.get_foreign_keys(table)

        # ── Build FK map (supports multi-column FKs) ─────────────
        fk_map = []
        for fk in fk_list:
            constrained = fk.get("constrained_columns", [])
            referred = fk.get("referred_columns", [])
            referred_table = fk.get("referred_table")

            if constrained and referred and referred_table:
                fk_map.append({
                    "source_cols": constrained,
                    "target_cols": referred,
                    "target_table": referred_table
                })

        # ── NODE ───────────────────────────────
        nodes.append({
            "id": table,
            "data": {
                "label": table,
                "columns": [
                    {
                        "name": col["name"],
                        "type": str(col["type"]),
                        "is_pk": col["name"] in pk_cols,
                        "is_fk": any(
                            col["name"] in fk["source_cols"]
                            for fk in fk_map
                        )
                    }
                    for col in columns
                ]
            }
        })

        # ── EDGES ───────────────────────────────
        for fk in fk_map:
            source_cols = fk["source_cols"]
            target_cols = fk["target_cols"]
            target_table = fk["target_table"]

            for i in range(min(len(source_cols), len(target_cols))):
                source_col = source_cols[i]
                target_col = target_cols[i]

                edges.append({
                    "id": f"{table}-{source_col}-{target_table}-{target_col}",
                    "source": table,
                    "target": target_table,
                    "source_column": source_col,
                    "target_column": target_col,
                    "label": f"{source_col} → {target_col}"
                })

    return {
        "nodes": nodes,
        "edges": edges
    }