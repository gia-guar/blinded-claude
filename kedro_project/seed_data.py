"""Generate synthetic classification data for the dummy Kedro project.

Writes ``data/01_raw/dummy_data.csv`` with 10 numeric features and a binary
``target`` column. Delete this file when you switch to real data.
"""
from pathlib import Path

import pandas as pd
from sklearn.datasets import make_classification

OUTPUT = Path(__file__).parent / "data" / "01_raw" / "dummy_data.csv"
N_FEATURES = 10


def main() -> None:
    X, y = make_classification(
        n_samples=1000,
        n_features=N_FEATURES,
        n_informative=5,
        n_redundant=2,
        random_state=42,
    )
    df = pd.DataFrame(X, columns=[f"feature_{i}" for i in range(N_FEATURES)])
    df["target"] = y

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT, index=False)
    print(f"Wrote {len(df)} rows x {df.shape[1]} cols to {OUTPUT}")


if __name__ == "__main__":
    main()
