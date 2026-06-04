import os
import uuid
import json
import pandas as pd
from sqlalchemy import create_engine

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET_DIR = os.path.join(BASE_DIR, "datasets")
REGISTRY_FILE = os.path.join(BASE_DIR, "datasets.json")

os.makedirs(DATASET_DIR, exist_ok=True)


# ───────────────────────────────
# Load / Save registry
# ───────────────────────────────
def _load_registry():
    if os.path.exists(REGISTRY_FILE):
        with open(REGISTRY_FILE, "r") as f:
            return json.load(f)
    return {}


def _save_registry(data):
    with open(REGISTRY_FILE, "w") as f:
        json.dump(data, f, indent=2)


datasets = _load_registry()


# ───────────────────────────────
# Create dataset from file
# ───────────────────────────────
def create_dataset_from_file(filename: str, file_bytes: bytes):
    dataset_id = str(uuid.uuid4())[:8]

    db_path = os.path.join(DATASET_DIR, f"{dataset_id}.sqlite")
    engine = create_engine(f"sqlite:///{db_path}")

    # detect file type
    if filename.endswith(".csv"):
        import io
        df = pd.read_csv(io.BytesIO(file_bytes))
    else:
        import io
        df = pd.read_excel(io.BytesIO(file_bytes))

    table_name = "data"
    df.to_sql(table_name, con=engine, if_exists="replace", index=False)

    datasets[dataset_id] = {
        "id": dataset_id,
        "name": filename,
        "db_path": db_path,
        "table": table_name
    }

    _save_registry(datasets)

    return dataset_id


# ───────────────────────────────
# Get dataset engine
# ───────────────────────────────
def get_dataset_engine(dataset_id: str):
    if dataset_id not in datasets:
        raise ValueError("Dataset not found")

    db_path = datasets[dataset_id]["db_path"]
    return create_engine(f"sqlite:///{db_path}")


def list_datasets():
    return list(datasets.values())