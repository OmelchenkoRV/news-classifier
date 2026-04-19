"""
DistilBERT Fine-Tuning for Crypto News Classification
=======================================================

Trains a DistilBERT model to classify crypto headlines by:
  1. Impact level: high / medium / low / noise
  2. Category: geopolitical / regulatory / exchange / macro / protocol /
               market_structure / noise

Two separate classification heads on the same base model.
The model runs on CPU (~50ms per headline inference).

Usage:
    python -m training.train_classifier
    python -m training.train_classifier --epochs 5 --batch-size 16
    python -m training.train_classifier --evaluate-only
"""

import os
import sys
import argparse
import logging
import json
from datetime import datetime

import numpy as np
import pandas as pd
import torch
from torch import nn
from torch.utils.data import Dataset, DataLoader
from transformers import DistilBertTokenizer, DistilBertModel
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, confusion_matrix

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")
os.makedirs(MODEL_DIR, exist_ok=True)

IMPACT_LABELS = ["noise", "low", "medium", "high"]
CATEGORY_LABELS = ["noise", "geopolitical", "regulatory", "exchange",
                    "macro", "protocol", "market_structure"]

MAX_LENGTH = 128  # headline max tokens


class HeadlineDataset(Dataset):
    """PyTorch dataset for headline classification."""

    def __init__(self, texts, impact_labels, category_labels, tokenizer):
        self.encodings = tokenizer(
            texts, truncation=True, padding="max_length",
            max_length=MAX_LENGTH, return_tensors="pt"
        )
        self.impact_labels = torch.tensor(impact_labels, dtype=torch.long)
        self.category_labels = torch.tensor(category_labels, dtype=torch.long)

    def __len__(self):
        return len(self.impact_labels)

    def __getitem__(self, idx):
        return {
            "input_ids": self.encodings["input_ids"][idx],
            "attention_mask": self.encodings["attention_mask"][idx],
            "impact_label": self.impact_labels[idx],
            "category_label": self.category_labels[idx],
        }


class HeadlineClassifier(nn.Module):
    """
    DistilBERT with two classification heads:
      - Impact: 4-class (noise, low, medium, high)
      - Category: 7-class (noise, geopolitical, regulatory, ...)
    """

    def __init__(self, num_impact=4, num_category=7):
        super().__init__()
        self.distilbert = DistilBertModel.from_pretrained("distilbert-base-uncased")
        hidden_size = self.distilbert.config.hidden_size  # 768

        self.impact_head = nn.Sequential(
            nn.Dropout(0.3),
            nn.Linear(hidden_size, 128),
            nn.ReLU(),
            nn.Dropout(0.2),
            nn.Linear(128, num_impact),
        )

        self.category_head = nn.Sequential(
            nn.Dropout(0.3),
            nn.Linear(hidden_size, 128),
            nn.ReLU(),
            nn.Dropout(0.2),
            nn.Linear(128, num_category),
        )

    def forward(self, input_ids, attention_mask):
        outputs = self.distilbert(input_ids=input_ids, attention_mask=attention_mask)
        cls_hidden = outputs.last_hidden_state[:, 0, :]  # [CLS] token

        impact_logits = self.impact_head(cls_hidden)
        category_logits = self.category_head(cls_hidden)

        return impact_logits, category_logits


def load_training_data():
    """Load labelled headlines from the database."""
    conn = get_connection()
    cur = get_cursor(conn)

    cur.execute("""
        SELECT h.title,
               l.impact_level,
               COALESCE(l.category, 'noise') as category
        FROM headline_labels l
        JOIN headlines h ON h.id = l.headline_id
        WHERE l.impact_level IS NOT NULL
    """)
    rows = cur.fetchall()
    conn.close()

    if not rows:
        logger.error("No labelled data found. Run auto_label and review_tool first.")
        sys.exit(1)

    df = pd.DataFrame(rows)
    logger.info(f"Loaded {len(df)} labelled headlines")
    logger.info(f"Impact distribution:\n{df['impact_level'].value_counts().to_string()}")
    logger.info(f"Category distribution:\n{df['category'].value_counts().to_string()}")

    # Encode labels
    impact_map = {label: i for i, label in enumerate(IMPACT_LABELS)}
    category_map = {label: i for i, label in enumerate(CATEGORY_LABELS)}

    df["impact_idx"] = df["impact_level"].map(impact_map).fillna(0).astype(int)
    df["category_idx"] = df["category"].map(category_map).fillna(0).astype(int)

    return df


def train(epochs: int = 3, batch_size: int = 16, lr: float = 2e-5):
    """Train the headline classifier."""
    df = load_training_data()

    # Split: 80% train, 10% val, 10% test
    train_df, temp_df = train_test_split(df, test_size=0.2, random_state=42,
                                          stratify=df["impact_idx"])
    val_df, test_df = train_test_split(temp_df, test_size=0.5, random_state=42)

    logger.info(f"Split: {len(train_df)} train / {len(val_df)} val / {len(test_df)} test")

    # Tokenizer
    tokenizer = DistilBertTokenizer.from_pretrained("distilbert-base-uncased")

    train_dataset = HeadlineDataset(
        train_df["title"].tolist(),
        train_df["impact_idx"].tolist(),
        train_df["category_idx"].tolist(),
        tokenizer,
    )
    val_dataset = HeadlineDataset(
        val_df["title"].tolist(),
        val_df["impact_idx"].tolist(),
        val_df["category_idx"].tolist(),
        tokenizer,
    )
    test_dataset = HeadlineDataset(
        test_df["title"].tolist(),
        test_df["impact_idx"].tolist(),
        test_df["category_idx"].tolist(),
        tokenizer,
    )

    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=batch_size)
    test_loader = DataLoader(test_dataset, batch_size=batch_size)

    # Model
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logger.info(f"Training on: {device}")

    model = HeadlineClassifier().to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01)

    # Class weights for imbalanced data (impact: mostly noise/low)
    impact_counts = train_df["impact_idx"].value_counts().sort_index().values
    impact_weights = torch.FloatTensor(
        [1.0 / max(c, 1) for c in impact_counts]
    ).to(device)
    impact_weights = impact_weights / impact_weights.sum() * len(impact_counts)

    impact_criterion = nn.CrossEntropyLoss(weight=impact_weights)
    category_criterion = nn.CrossEntropyLoss()

    # Training loop
    best_val_loss = float("inf")
    for epoch in range(epochs):
        model.train()
        train_loss = 0
        for batch in train_loader:
            input_ids = batch["input_ids"].to(device)
            attention_mask = batch["attention_mask"].to(device)
            impact_labels = batch["impact_label"].to(device)
            category_labels = batch["category_label"].to(device)

            impact_logits, category_logits = model(input_ids, attention_mask)

            loss_impact = impact_criterion(impact_logits, impact_labels)
            loss_category = category_criterion(category_logits, category_labels)
            loss = loss_impact + 0.5 * loss_category  # impact is primary task

            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()

            train_loss += loss.item()

        avg_train = train_loss / len(train_loader)

        # Validation
        model.eval()
        val_loss = 0
        with torch.no_grad():
            for batch in val_loader:
                input_ids = batch["input_ids"].to(device)
                attention_mask = batch["attention_mask"].to(device)
                impact_labels = batch["impact_label"].to(device)
                category_labels = batch["category_label"].to(device)

                impact_logits, category_logits = model(input_ids, attention_mask)
                loss = (impact_criterion(impact_logits, impact_labels)
                        + 0.5 * category_criterion(category_logits, category_labels))
                val_loss += loss.item()

        avg_val = val_loss / len(val_loader)
        logger.info(f"Epoch {epoch+1}/{epochs}: train_loss={avg_train:.4f}, val_loss={avg_val:.4f}")

        # Save best model
        if avg_val < best_val_loss:
            best_val_loss = avg_val
            model_path = os.path.join(MODEL_DIR, "headline_classifier.pt")
            torch.save({
                "model_state_dict": model.state_dict(),
                "epoch": epoch,
                "val_loss": avg_val,
                "impact_labels": IMPACT_LABELS,
                "category_labels": CATEGORY_LABELS,
            }, model_path)
            logger.info(f"  Saved best model (val_loss={avg_val:.4f})")

    # Evaluate on test set
    logger.info("\nTest set evaluation:")
    evaluate_model(model, test_loader, device)

    # Save tokenizer config for inference
    tokenizer.save_pretrained(os.path.join(MODEL_DIR, "tokenizer"))
    logger.info(f"\nModel saved to: {MODEL_DIR}/")


def evaluate_model(model, test_loader, device):
    """Evaluate model on test set with detailed metrics."""
    model.eval()
    all_impact_preds = []
    all_impact_true = []
    all_cat_preds = []
    all_cat_true = []

    with torch.no_grad():
        for batch in test_loader:
            input_ids = batch["input_ids"].to(device)
            attention_mask = batch["attention_mask"].to(device)

            impact_logits, cat_logits = model(input_ids, attention_mask)

            all_impact_preds.extend(impact_logits.argmax(dim=1).cpu().numpy())
            all_impact_true.extend(batch["impact_label"].numpy())
            all_cat_preds.extend(cat_logits.argmax(dim=1).cpu().numpy())
            all_cat_true.extend(batch["category_label"].numpy())

    logger.info("\n--- Impact Level Classification ---")
    logger.info("\n" + classification_report(
        all_impact_true, all_impact_preds,
        target_names=IMPACT_LABELS, zero_division=0
    ))

    logger.info("\n--- Category Classification ---")
    logger.info("\n" + classification_report(
        all_cat_true, all_cat_preds,
        target_names=CATEGORY_LABELS, zero_division=0
    ))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train headline classifier")
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--lr", type=float, default=2e-5)
    parser.add_argument("--evaluate-only", action="store_true")
    args = parser.parse_args()

    train(epochs=args.epochs, batch_size=args.batch_size, lr=args.lr)
