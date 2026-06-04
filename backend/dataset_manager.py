import pandas as pd
import os
from sqlalchemy import create_engine

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET_DIR = os.path.join(BASE_DIR, "datasets")

os.makedirs(DATASET_DIR, exist_ok=True)

ACTIVE_DATASETS = {}

def create_dataset_from_file(filename, file_bytes):
    path = os.path.join(DATASET_DIR, filename.replace(" ", "_"))

    with open(path, "wb") as f:
        f.write(file_bytes)

    # Create sqlite DB for dataset
    db_path = path + ".db"
    engine = create_engine(f"sqlite:///{db_path}")

    # detect format
    if filename.endswith(".csv"):
        import pandas as pd
        df = pd.read_csv(path)
    else:
        df = pd.read_excel(path)

    table_name = "dataset_table"
    df.to_sql(table_name, engine, if_exists="replace", index=False)

    dataset_id = os.path.basename(db_path)

    ACTIVE_DATASETS[dataset_id] = engine

    return dataset_id


def get_dataset_engine(dataset_id):
    return ACTIVE_DATASETS.get(dataset_id)


def list_datasets():
    return list(ACTIVE_DATASETS.keys())