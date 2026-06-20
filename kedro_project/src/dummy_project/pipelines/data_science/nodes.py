"""Nodes for the data_science pipeline."""
from __future__ import annotations

import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score
from sklearn.model_selection import train_test_split


def split_data(data: pd.DataFrame, parameters: dict):
    """Split the dataframe into train/test feature and target sets."""
    target = parameters["target_column"]
    data = data.dropna()
    X = data.drop(columns=[target])
    y = data[target].astype(int)
    X_train, X_test, y_train, y_test = train_test_split(
        X,
        y,
        test_size=parameters["test_size"],
        random_state=parameters["random_state"],
    )
    return X_train, X_test, y_train, y_test


def train_model(X_train: pd.DataFrame, y_train: pd.Series, parameters: dict):
    """Fit a logistic regression classifier on the training data."""
    model = LogisticRegression(max_iter=parameters["max_iter"])
    model.fit(X_train, y_train)
    return model


def evaluate_model(model, X_test: pd.DataFrame, y_test: pd.Series) -> dict:
    """Evaluate the model and return scalar metrics.

    The returned dict is saved as a versioned ``json.JSONDataset`` so only these
    aggregated scalars are ever exposed through the MCP bridge.
    """
    predictions = model.predict(X_test)
    return {
        "accuracy": float(accuracy_score(y_test, predictions)),
        "f1": float(f1_score(y_test, predictions)),
    }
