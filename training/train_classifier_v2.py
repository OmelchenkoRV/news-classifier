"""
DistilBERT Fine-Tuning v2 — Optimized for Intel Core Ultra 5 125H
===================================================================

Optimizations kept (Windows-compatible):
  1. MAX_LENGTH 64 (headlines are short, saves ~50% compute)
  2. Layer freezing (epoch 1 = heads only, epoch 2+ = top 2 layers)
  3. BF16 mixed precision (Intel AMX support)
  4. Gradient accumulation (effective batch 32)

Removed (Windows issues):
  - torch.compile (needs MSVC cl.exe)
  - pin_memory (CUDA only)

Usage:
    python -m training.train_classifier_v2
    python -m training.train_classifier_v2 --epochs 5 --batch-size 8
"""

import os
import sys
import time
import argparse
import logging

import numpy as np
import pandas as pd
import torch
from torch import nn
from torch.utils.data import Dataset, DataLoader
from transformers import DistilBertTokenizer, DistilBertModel
from sklearn.metrics import classification_report

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")
os.makedirs(MODEL_DIR, exist_ok=True)

IMPACT_LABELS = ["noise", "low", "medium", "high", "during_event"]

CATEGORY_LABELS = [
    "geopolitical", "regulatory", "macro", "exchange_event",
    "whale_flow", "protocol",
    "technical_analysis", "price_commentary", "opinion", "promotion",
    "uncategorized",
]

MAX_LENGTH = 64


class HeadlineDataset(Dataset):
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
    def __init__(self, num_impact=5, num_category=11):
        super().__init__()
        self.distilbert = DistilBertModel.from_pretrained("distilbert-base-uncased")
        hidden_size = self.distilbert.config.hidden_size

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

    def freeze_base(self):
        for param in self.distilbert.parameters():
            param.requires_grad = False
        trainable = sum(p.numel() for p in self.parameters() if p.requires_grad)
        logger.info(f"  Base frozen. Trainable params: {trainable:,} (heads only)")

    def unfreeze_top_layers(self, n_layers=2):
        total_layers = len(self.distilbert.transformer.layer)
        for i in range(total_layers - n_layers, total_layers):
            for param in self.distilbert.transformer.layer[i].parameters():
                param.requires_grad = True
        trainable = sum(p.numel() for p in self.parameters() if p.requires_grad)
        logger.info(f"  Unfroze top {n_layers} layers. Trainable params: {trainable:,}")

    def forward(self, input_ids, attention_mask):
        outputs = self.distilbert(input_ids=input_ids, attention_mask=attention_mask)
        cls_hidden = outputs.last_hidden_state[:, 0, :]
        return self.impact_head(cls_hidden), self.category_head(cls_hidden)


def load_training_data():
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("""
        SELECT h.title, h.published_at, l.impact_level,
               COALESCE(l.category, 'uncategorized') as category,
               COALESCE(l.is_causal_capable, TRUE) as is_causal_capable
        FROM headline_labels l
        JOIN headlines h ON h.id = l.headline_id
        WHERE l.impact_level IS NOT NULL
        ORDER BY h.published_at
    """)
    rows = cur.fetchall()
    conn.close()

    if not rows:
        logger.error("No labelled data found. Run auto_label_v2 first.")
        sys.exit(1)

    df = pd.DataFrame(rows)
    df["published_at"] = pd.to_datetime(df["published_at"], utc=True)
    logger.info(f"Loaded {len(df)} labelled headlines")
    logger.info(f"Date range: {df['published_at'].min().date()} to {df['published_at'].max().date()}")
    logger.info(f"\nImpact distribution:\n{df['impact_level'].value_counts().to_string()}")
    logger.info(f"\nCategory distribution:\n{df['category'].value_counts().to_string()}")
    logger.info(f"\nCausal capable: {df['is_causal_capable'].sum()} / {len(df)}")

    impact_map = {label: i for i, label in enumerate(IMPACT_LABELS)}
    category_map = {label: i for i, label in enumerate(CATEGORY_LABELS)}

    df["impact_idx"] = df["impact_level"].map(impact_map).fillna(0).astype(int)
    df["category_idx"] = df["category"].map(category_map).fillna(
        len(CATEGORY_LABELS) - 1
    ).astype(int)

    return df


def walk_forward_split(df, train_ratio=0.7, val_ratio=0.15):
    df = df.sort_values("published_at").reset_index(drop=True)
    n = len(df)
    train_end = int(n * train_ratio)
    val_end = int(n * (train_ratio + val_ratio))

    train_df = df.iloc[:train_end]
    val_df = df.iloc[train_end:val_end]
    test_df = df.iloc[val_end:]

    logger.info(f"\nWalk-forward split:")
    logger.info(f"  Train: {len(train_df):>6d} ({train_df['published_at'].min().date()} to {train_df['published_at'].max().date()})")
    logger.info(f"  Val:   {len(val_df):>6d} ({val_df['published_at'].min().date()} to {val_df['published_at'].max().date()})")
    logger.info(f"  Test:  {len(test_df):>6d} ({test_df['published_at'].min().date()} to {test_df['published_at'].max().date()})")
    return train_df, val_df, test_df


def compute_class_weights(labels, num_classes, device):
    counts = np.bincount(labels, minlength=num_classes).astype(float)
    counts = np.maximum(counts, 1.0)
    weights = 1.0 / counts
    weights = weights / weights.sum() * num_classes
    return torch.FloatTensor(weights).to(device)


def train(epochs=3, batch_size=8, lr=2e-5, accumulation_steps=4):
    df = load_training_data()
    train_df, val_df, test_df = walk_forward_split(df)

    tokenizer = DistilBertTokenizer.from_pretrained("distilbert-base-uncased")

    train_dataset = HeadlineDataset(
        train_df["title"].tolist(), train_df["impact_idx"].tolist(),
        train_df["category_idx"].tolist(), tokenizer,
    )
    val_dataset = HeadlineDataset(
        val_df["title"].tolist(), val_df["impact_idx"].tolist(),
        val_df["category_idx"].tolist(), tokenizer,
    )
    test_dataset = HeadlineDataset(
        test_df["title"].tolist(), test_df["impact_idx"].tolist(),
        test_df["category_idx"].tolist(), tokenizer,
    )

    num_workers = min(4, os.cpu_count() or 1)
    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, num_workers=num_workers)
    val_loader = DataLoader(val_dataset, batch_size=batch_size * 2, num_workers=num_workers)
    test_loader = DataLoader(test_dataset, batch_size=batch_size * 2, num_workers=num_workers)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    effective_batch = batch_size * accumulation_steps

    # BF16 detection
    use_bf16 = False
    try:
        t = torch.randn(2, 2, dtype=torch.bfloat16)
        _ = t @ t
        use_bf16 = True
    except Exception:
        pass

    logger.info(f"\nTraining config:")
    logger.info(f"  Device:           {device}")
    logger.info(f"  Precision:        {'BF16' if use_bf16 else 'FP32'}")
    logger.info(f"  Micro-batch:      {batch_size}")
    logger.info(f"  Accumulation:     {accumulation_steps}")
    logger.info(f"  Effective batch:  {effective_batch}")
    logger.info(f"  Max length:       {MAX_LENGTH} tokens")
    logger.info(f"  Data workers:     {num_workers}")

    model = HeadlineClassifier(
        num_impact=len(IMPACT_LABELS),
        num_category=len(CATEGORY_LABELS),
    ).to(device)

    # Loss functions (defined ONCE, used every epoch)
    impact_weights = compute_class_weights(train_df["impact_idx"].values, len(IMPACT_LABELS), device)
    category_weights = compute_class_weights(train_df["category_idx"].values, len(CATEGORY_LABELS), device)
    impact_criterion = nn.CrossEntropyLoss(weight=impact_weights)
    category_criterion = nn.CrossEntropyLoss(weight=category_weights)

    best_val_loss = float("inf")
    total_start = time.time()

    for epoch in range(epochs):
        epoch_start = time.time()

        # Layer freezing schedule
        if epoch == 0:
            logger.info(f"\nEpoch {epoch+1}/{epochs}: HEADS ONLY (base frozen)")
            model.freeze_base()
            optimizer = torch.optim.AdamW(
                [p for p in model.parameters() if p.requires_grad],
                lr=lr * 5, weight_decay=0.01,
            )
        elif epoch == 1:
            logger.info(f"\nEpoch {epoch+1}/{epochs}: FINE-TUNING top 2 layers")
            model.unfreeze_top_layers(2)
            optimizer = torch.optim.AdamW(
                [p for p in model.parameters() if p.requires_grad],
                lr=lr, weight_decay=0.01,
            )
        else:
            logger.info(f"\nEpoch {epoch+1}/{epochs}: FINE-TUNING (LR decay)")
            for pg in optimizer.param_groups:
                pg["lr"] = lr * 0.5

        # ── Training ──────────────────────────────────────────────
        model.train()
        train_loss = 0
        optimizer.zero_grad()

        for step, batch in enumerate(train_loader):
            input_ids = batch["input_ids"].to(device)
            attention_mask = batch["attention_mask"].to(device)
            impact_lab = batch["impact_label"].to(device)
            category_lab = batch["category_label"].to(device)

            if use_bf16:
                with torch.autocast(device_type="cpu", dtype=torch.bfloat16):
                    imp_logits, cat_logits = model(input_ids, attention_mask)
                    loss = (0.5 * impact_criterion(imp_logits, impact_lab)
                            + category_criterion(cat_logits, category_lab)) / accumulation_steps
            else:
                imp_logits, cat_logits = model(input_ids, attention_mask)
                loss = (0.5 * impact_criterion(imp_logits, impact_lab)
                        + category_criterion(cat_logits, category_lab)) / accumulation_steps

            loss.backward()

            if (step + 1) % accumulation_steps == 0 or (step + 1) == len(train_loader):
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
                optimizer.zero_grad()

            train_loss += loss.item() * accumulation_steps

        avg_train = train_loss / len(train_loader)

        # ── Validation ────────────────────────────────────────────
        model.eval()
        val_loss = 0
        with torch.no_grad():
            for batch in val_loader:
                input_ids = batch["input_ids"].to(device)
                attention_mask = batch["attention_mask"].to(device)
                impact_lab = batch["impact_label"].to(device)
                category_lab = batch["category_label"].to(device)

                if use_bf16:
                    with torch.autocast(device_type="cpu", dtype=torch.bfloat16):
                        imp_logits, cat_logits = model(input_ids, attention_mask)
                        loss = (0.5 * impact_criterion(imp_logits, impact_lab)
                                + category_criterion(cat_logits, category_lab))
                else:
                    imp_logits, cat_logits = model(input_ids, attention_mask)
                    loss = (0.5 * impact_criterion(imp_logits, impact_lab)
                            + category_criterion(cat_logits, category_lab))
                val_loss += loss.item()

        avg_val = val_loss / len(val_loader)
        epoch_time = time.time() - epoch_start

        logger.info(
            f"  train_loss={avg_train:.4f}, val_loss={avg_val:.4f}, "
            f"time={epoch_time:.0f}s ({epoch_time/60:.1f}min)"
        )

        if avg_val < best_val_loss:
            best_val_loss = avg_val
            torch.save({
                "model_state_dict": model.state_dict(),
                "epoch": epoch,
                "val_loss": avg_val,
                "impact_labels": IMPACT_LABELS,
                "category_labels": CATEGORY_LABELS,
            }, os.path.join(MODEL_DIR, "headline_classifier.pt"))
            logger.info(f"  Saved best model (val_loss={avg_val:.4f})")

    total_time = time.time() - total_start
    logger.info(f"\nTotal training time: {total_time:.0f}s ({total_time/60:.1f}min)")

    # ── Evaluate ──────────────────────────────────────────────────
    logger.info(f"\n{'='*70}")
    logger.info("TEST SET EVALUATION (walk-forward)")
    logger.info(f"{'='*70}")
    evaluate_model(model, test_loader, device, use_bf16)

    tokenizer.save_pretrained(os.path.join(MODEL_DIR, "tokenizer"))
    logger.info(f"\nModel saved to: {MODEL_DIR}/")


def evaluate_model(model, test_loader, device, use_bf16=False):
    model.eval()
    all_imp_pred, all_imp_true = [], []
    all_cat_pred, all_cat_true = [], []

    with torch.no_grad():
        for batch in test_loader:
            input_ids = batch["input_ids"].to(device)
            attention_mask = batch["attention_mask"].to(device)

            if use_bf16:
                with torch.autocast(device_type="cpu", dtype=torch.bfloat16):
                    imp_logits, cat_logits = model(input_ids, attention_mask)
            else:
                imp_logits, cat_logits = model(input_ids, attention_mask)

            all_imp_pred.extend(imp_logits.argmax(dim=1).cpu().numpy())
            all_imp_true.extend(batch["impact_label"].numpy())
            all_cat_pred.extend(cat_logits.argmax(dim=1).cpu().numpy())
            all_cat_true.extend(batch["category_label"].numpy())

    imp_present = sorted(set(all_imp_true) | set(all_imp_pred))
    cat_present = sorted(set(all_cat_true) | set(all_cat_pred))

    logger.info("\n--- Impact Level Classification ---")
    logger.info("\n" + classification_report(
        all_imp_true, all_imp_pred,
        labels=imp_present,
        target_names=[IMPACT_LABELS[i] for i in imp_present],
        zero_division=0,
    ))

    logger.info("\n--- Category Classification ---")
    logger.info("\n" + classification_report(
        all_cat_true, all_cat_pred,
        labels=cat_present,
        target_names=[CATEGORY_LABELS[i] for i in cat_present],
        zero_division=0,
    ))

    # Key metric: causal vs reactive binary
    causal_cats = {"geopolitical", "regulatory", "macro", "exchange_event", "whale_flow", "protocol"}
    causal_idx = {i for i, l in enumerate(CATEGORY_LABELS) if l in causal_cats}
    true_c = [1 if t in causal_idx else 0 for t in all_cat_true]
    pred_c = [1 if p in causal_idx else 0 for p in all_cat_pred]

    logger.info("\n--- Causal vs Reactive (binary) — KEY METRIC ---")
    logger.info("\n" + classification_report(
        true_c, pred_c, target_names=["reactive", "causal"], zero_division=0,
    ))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train headline classifier v2")
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--lr", type=float, default=2e-5)
    parser.add_argument("--accumulation", type=int, default=4)
    args = parser.parse_args()
    train(epochs=args.epochs, batch_size=args.batch_size, lr=args.lr,
          accumulation_steps=args.accumulation)
