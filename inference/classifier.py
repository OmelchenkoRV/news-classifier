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
IMPACT_WEIGHT = {"noise": 0.0, "low": 0.2, "medium": 0.5, "high": 1.0, "during_event": 0.3}

# Categories that should tighten trading gates (non-crypto-native catalysts)
TIGHTEN_CATEGORIES = {"geopolitical", "macro", "regulatory"}

# Categories that are reactive — headline describes an existing move
REACTIVE_CATEGORIES = {"technical_analysis", "price_commentary", "opinion", "promotion"}


class HeadlineClassifierInference:
    """Load trained model and classify headlines."""

    def __init__(self, model_dir: str = None, quantize: bool = False):
        model_dir = model_dir or MODEL_DIR
        model_path = os.path.join(model_dir, "headline_classifier.pt")
        tokenizer_path = os.path.join(model_dir, "tokenizer")

        if not os.path.exists(model_path):
            raise FileNotFoundError(
                f"Model not found at {model_path}. "
                f"Run: python -m training.train_classifier"
            )

        # Load model
        from training.train_classifier_v2 import HeadlineClassifier

        checkpoint = torch.load(model_path, map_location="cpu", weights_only=False)
        self.impact_labels = checkpoint["impact_labels"]
        self.category_labels = checkpoint["category_labels"]

        self.model = HeadlineClassifier(
            num_impact=len(self.impact_labels),
            num_category=len(self.category_labels),
        )
        self.model.load_state_dict(checkpoint["model_state_dict"])
        self.model.eval()

        # Optional int8 dynamic quantization. Quantizes Linear layers to
        # int8 at inference time (no retraining). The int8 matmuls use the
        # CPU's AVX-VNNI / SIMD instructions via PyTorch's optimized
        # kernels — the SIMD speedup without hand-written C. Typically
        # 2-4x faster on CPU for a transformer, with sub-1% accuracy loss
        # on a classification head. Off by default (keeps live-pipeline
        # behaviour bit-identical); turn on for large batch backfills via
        # classify_backfill --quantize. NOTE: a quantized model produces
        # very slightly different probabilities than float32 — fine for a
        # backfill, but it means batch-classified rows aren't bit-identical
        # to live float32 rows. For this experiment that's an acceptable
        # trade for the speed; we note it as a (tiny) source of variance.
        if quantize:
            self.model = torch.quantization.quantize_dynamic(
                self.model, {torch.nn.Linear}, dtype=torch.qint8
            )
            logger.info("Applied int8 dynamic quantization to Linear layers.")

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

        # Shared assembly so classify() and classify_batch() are identical.
        return self._assemble_result(impact_probs, cat_probs)

    def classify_batch(self, headlines: list, batch_size: int = 64) -> list:
        """Classify many headlines with REAL tensor batching.

        The previous implementation looped classify() one headline at a
        time — correct but slow, because each call paid full tokenization
        plus a single-item forward pass. For large backfills (hundreds of
        thousands of headlines on CPU) that is the difference between a
        few hours and most of a day. Here we tokenize and forward-pass in
        batches of `batch_size`, which amortises per-call overhead and
        lets the matrix ops run at width — typically several times faster
        on CPU.

        Returns a list of result dicts in the same order as the input,
        identical in shape to classify()'s output.
        """
        if not headlines:
            return []

        results = []
        for start in range(0, len(headlines), batch_size):
            chunk = headlines[start:start + batch_size]
            # Dynamic padding (padding=True) pads only to the longest
            # headline IN THIS BATCH, not a fixed 128. Headlines are
            # ~15-25 tokens, so fixed max_length=128 padding made the
            # model process ~5-8x mostly-padding tokens — the dominant
            # cost on CPU. Dynamic padding cuts that without changing
            # results (attention_mask zeroes padding either way).
            # We still cap at 128 via truncation for the rare long title.
            encoding = self.tokenizer(
                chunk, truncation=True, padding=True,
                max_length=128, return_tensors="pt",
            )
            with torch.no_grad():
                impact_logits, cat_logits = self.model(
                    encoding["input_ids"], encoding["attention_mask"]
                )
            impact_probs = torch.softmax(impact_logits, dim=1)
            cat_probs = torch.softmax(cat_logits, dim=1)

            for i in range(len(chunk)):
                results.append(self._assemble_result(
                    impact_probs[i], cat_probs[i]
                ))
        return results

    def _assemble_result(self, impact_probs, cat_probs) -> dict:
        """Build a result dict from per-item probability tensors. Shared
        by classify() and classify_batch() so the two can never drift."""
        impact_idx = impact_probs.argmax().item()
        cat_idx = cat_probs.argmax().item()

        impact_level = self.impact_labels[impact_idx]
        category = self.category_labels[cat_idx]
        impact_conf = float(impact_probs[impact_idx])
        cat_conf = float(cat_probs[cat_idx])

        magnitude = IMPACT_WEIGHT.get(impact_level, 0.0) * impact_conf
        is_reactive = category in REACTIVE_CATEGORIES
        if is_reactive:
            magnitude = 0.0
        news_impact_score = magnitude

        should_tighten = (
            category in TIGHTEN_CATEGORIES
            and cat_conf > 0.6
            and not is_reactive
        )

        return {
            "impact_level": impact_level,
            "impact_confidence": round(impact_conf, 3),
            "category": category,
            "category_confidence": round(cat_conf, 3),
            "is_causal": not is_reactive,
            "news_impact_score": round(news_impact_score, 3),
            "should_tighten_gates": should_tighten,
        }


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
