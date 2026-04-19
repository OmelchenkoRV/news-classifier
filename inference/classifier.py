"""
Headline Classifier — Inference
=================================

Loads the trained DistilBERT model and classifies headlines.
Returns a news_impact_score for integration with the risk pipeline.

Usage:
    from inference.classifier import HeadlineClassifierInference

    clf = HeadlineClassifierInference()
    result = clf.classify("Trump announces ceasefire with Iran")
    # result = {
    #     "impact_level": "high",
    #     "impact_confidence": 0.87,
    #     "category": "geopolitical",
    #     "category_confidence": 0.92,
    #     "news_impact_score": 0.78,  # [-1, +1] for pipeline integration
    # }
"""

import os
import logging

import torch
from transformers import DistilBertTokenizer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")

# Impact scores for computing news_impact_score
IMPACT_WEIGHT = {"noise": 0.0, "low": 0.2, "medium": 0.5, "high": 1.0}

# Categories that should tighten trading gates (non-crypto-native catalysts)
TIGHTEN_CATEGORIES = {"geopolitical", "macro", "regulatory"}


class HeadlineClassifierInference:
    """Load trained model and classify headlines."""

    def __init__(self, model_dir: str = None):
        model_dir = model_dir or MODEL_DIR
        model_path = os.path.join(model_dir, "headline_classifier.pt")
        tokenizer_path = os.path.join(model_dir, "tokenizer")

        if not os.path.exists(model_path):
            raise FileNotFoundError(
                f"Model not found at {model_path}. "
                f"Run: python -m training.train_classifier"
            )

        # Load model
        from training.train_classifier import HeadlineClassifier

        checkpoint = torch.load(model_path, map_location="cpu", weights_only=False)
        self.impact_labels = checkpoint["impact_labels"]
        self.category_labels = checkpoint["category_labels"]

        self.model = HeadlineClassifier(
            num_impact=len(self.impact_labels),
            num_category=len(self.category_labels),
        )
        self.model.load_state_dict(checkpoint["model_state_dict"])
        self.model.eval()

        self.tokenizer = DistilBertTokenizer.from_pretrained(tokenizer_path)
        logger.info(f"Model loaded from {model_path}")

    def classify(self, headline: str) -> dict:
        """
        Classify a single headline.

        Returns dict with:
          impact_level, impact_confidence,
          category, category_confidence,
          news_impact_score ([-1, +1] for pipeline),
          should_tighten_gates (bool)
        """
        encoding = self.tokenizer(
            headline, truncation=True, padding="max_length",
            max_length=128, return_tensors="pt"
        )

        with torch.no_grad():
            impact_logits, cat_logits = self.model(
                encoding["input_ids"], encoding["attention_mask"]
            )

        impact_probs = torch.softmax(impact_logits, dim=1)[0]
        cat_probs = torch.softmax(cat_logits, dim=1)[0]

        impact_idx = impact_probs.argmax().item()
        cat_idx = cat_probs.argmax().item()

        impact_level = self.impact_labels[impact_idx]
        category = self.category_labels[cat_idx]
        impact_conf = float(impact_probs[impact_idx])
        cat_conf = float(cat_probs[cat_idx])

        # Compute news_impact_score for pipeline integration
        # Magnitude from impact level, sign from whether it's constructive
        magnitude = IMPACT_WEIGHT.get(impact_level, 0.0) * impact_conf
        # For now, we don't predict direction (bullish/bearish) —
        # we just flag magnitude. Direction comes from price action.
        news_impact_score = magnitude

        # Should the capture gate tighten?
        should_tighten = (
            category in TIGHTEN_CATEGORIES
            and impact_level in ("high", "medium")
            and impact_conf > 0.6
        )

        return {
            "impact_level": impact_level,
            "impact_confidence": round(impact_conf, 3),
            "category": category,
            "category_confidence": round(cat_conf, 3),
            "news_impact_score": round(news_impact_score, 3),
            "should_tighten_gates": should_tighten,
        }

    def classify_batch(self, headlines: list) -> list:
        """Classify multiple headlines at once (more efficient)."""
        return [self.classify(h) for h in headlines]


if __name__ == "__main__":
    # Quick test
    clf = HeadlineClassifierInference()

    test_headlines = [
        "Trump announces ceasefire with Iran over social media",
        "SEC approves spot Ethereum ETF applications",
        "Binance halts ETH withdrawals amid liquidity concerns",
        "Fed holds interest rates steady, signals possible cuts",
        "Ethereum Pectra upgrade successfully deployed on mainnet",
        "Bitcoin price could reach $200,000 by end of year, analyst says",
        "Major liquidation cascade wipes $500M from crypto markets",
        "New memecoin gains 5000% in 24 hours",
    ]

    print(f"\n{'Headline':65s} {'Impact':8s} {'Conf':6s} {'Category':18s} {'Gate':6s}")
    print("-" * 110)
    for h in test_headlines:
        r = clf.classify(h)
        gate = "TIGHT" if r["should_tighten_gates"] else ""
        print(
            f"{h[:64]:65s} {r['impact_level']:8s} {r['impact_confidence']:.2f}  "
            f"{r['category']:18s} {gate}"
        )
