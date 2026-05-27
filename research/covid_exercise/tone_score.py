"""
Tone Scoring with FinBERT
==========================

Computes per-headline financial-news sentiment using FinBERT-tone.
Stores scores so they can be aggregated for drift detection.

Model: yiyanghkust/finbert-tone
  - 110M parameters, ~400MB
  - 3-class output: positive / neutral / negative
  - We map to a single signed score in [-1, +1]:
      negative * (-1) + neutral * 0 + positive * (+1)

Usage:
    # First time only - downloads model (~400MB)
    python -m research.covid_exercise.tone_score --topic covid

    # Process all topics
    python -m research.covid_exercise.tone_score --all
"""

import os
import sys
import argparse
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from config.database import get_connection, get_cursor

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


MODEL_NAME = "yiyanghkust/finbert-tone"


def install_dependencies():
    """Install transformers + torch + sentencepiece on first run."""
    import subprocess
    
    needed = []
    try:
        import torch  # noqa
    except ImportError:
        needed.append("torch")
    try:
        import transformers  # noqa
    except ImportError:
        needed.append("transformers")
    try:
        import sentencepiece  # noqa - required by FinBERT tokenizer
    except ImportError:
        needed.append("sentencepiece")
    
    if not needed:
        return True
    
    log.info(f"Installing dependencies: {needed}")
    cmds = [
        [sys.executable, "-m", "pip", "install"] + needed + ["--break-system-packages", "--quiet"],
        [sys.executable, "-m", "pip", "install"] + needed + ["--quiet"],
    ]
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode == 0:
                return True
        except Exception:
            continue
    log.error(f"Could not install: pip install {' '.join(needed)}")
    return False


def add_tone_column():
    """Add tone columns to historical_headlines if not present."""
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("""
        ALTER TABLE historical_headlines
        ADD COLUMN IF NOT EXISTS tone_score DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS tone_label TEXT,
        ADD COLUMN IF NOT EXISTS tone_confidence DOUBLE PRECISION
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_hh_tone_topic_date
        ON historical_headlines(research_topic, published_at)
        WHERE tone_score IS NOT NULL
    """)
    conn.commit()
    conn.close()


class FinBertScorer:
    """
    Wraps FinBERT-tone for batched scoring.
    Loads model once, scores in batches of 32 for efficiency.
    """
    def __init__(self, model_name: str = MODEL_NAME):
        log.info(f"Loading model: {model_name}")
        from transformers import BertTokenizer, BertForSequenceClassification, BertConfig
        import torch
        
        # finbert-tone has incomplete metadata files:
        #   - missing tokenizer.json/tokenizer_config.json (use BertTokenizer with vocab.txt)
        #   - config.json missing 'model_type' field (use BertConfig+BertForSequenceClassification
        #     directly instead of AutoModel which can't infer the type)
        # Both fixes bypass the auto-discovery that newer transformers versions require.
        self.tokenizer = BertTokenizer.from_pretrained(model_name)
        
        config = BertConfig.from_pretrained(model_name)
        # Make sure num_labels is set (FinBERT-tone has 3 classes)
        if not hasattr(config, "num_labels") or config.num_labels != 3:
            config.num_labels = 3
        
        self.model = BertForSequenceClassification.from_pretrained(
            model_name, config=config
        )
        self.model.eval()
        
        # Detect device
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model.to(self.device)
        log.info(f"Model loaded on {self.device}")
        
        # finbert-tone label order: negative=0, neutral=1, positive=2
        self.label_map = {0: "negative", 1: "neutral", 2: "positive"}
        # Map labels to signed scores: negative=-1, neutral=0, positive=+1
        self.score_weights = {0: -1.0, 1: 0.0, 2: 1.0}
    
    def score_batch(self, texts: list[str]) -> list[dict]:
        """Score a batch of texts. Returns list of {score, label, confidence}."""
        import torch
        
        if not texts:
            return []
        
        # Tokenize batch
        inputs = self.tokenizer(
            texts, padding=True, truncation=True, max_length=128,
            return_tensors="pt"
        ).to(self.device)
        
        with torch.no_grad():
            outputs = self.model(**inputs)
            probs = torch.softmax(outputs.logits, dim=-1)
        
        results = []
        for prob_row in probs:
            prob_list = prob_row.cpu().numpy().tolist()
            # Predicted label is argmax
            pred_idx = int(prob_row.argmax().item())
            label = self.label_map[pred_idx]
            confidence = prob_list[pred_idx]
            
            # Signed score = expected value over distribution
            #   E[score] = -1 * P(neg) + 0 * P(neu) + 1 * P(pos)
            score = sum(p * self.score_weights[i] for i, p in enumerate(prob_list))
            
            results.append({
                "score": score,
                "label": label,
                "confidence": confidence,
            })
        
        return results


def score_topic(topic: str | None, batch_size: int = 32):
    """
    Score all unscored headlines for a given topic (or all topics if None).
    """
    if not install_dependencies():
        log.error("Cannot proceed without transformers/torch")
        return
    
    add_tone_column()
    
    conn = get_connection()
    cur = get_cursor(conn)
    
    # Find headlines that need scoring
    if topic:
        cur.execute("""
            SELECT id, title FROM historical_headlines
            WHERE research_topic = %s AND tone_score IS NULL
            ORDER BY id
        """, (topic,))
    else:
        cur.execute("""
            SELECT id, title FROM historical_headlines
            WHERE tone_score IS NULL
            ORDER BY id
        """)
    
    rows = cur.fetchall()
    
    if not rows:
        log.info("No headlines need scoring.")
        conn.close()
        return
    
    log.info(f"Scoring {len(rows):,} headlines (batch size {batch_size})")
    
    scorer = FinBertScorer()
    update_cur = conn.cursor()
    
    for batch_start in range(0, len(rows), batch_size):
        batch = rows[batch_start:batch_start + batch_size]
        titles = [r["title"] for r in batch]
        
        results = scorer.score_batch(titles)
        
        for row, result in zip(batch, results):
            update_cur.execute("""
                UPDATE historical_headlines
                SET tone_score = %s, tone_label = %s, tone_confidence = %s
                WHERE id = %s
            """, (
                float(result["score"]),
                result["label"],
                float(result["confidence"]),
                row["id"],
            ))
        
        conn.commit()
        
        progress = batch_start + len(batch)
        if progress % 256 == 0 or progress == len(rows):
            log.info(f"  ... {progress:,}/{len(rows):,}")
    
    conn.close()
    log.info("Done.")


def show_tone_stats():
    """Print quick distribution stats by topic."""
    conn = get_connection()
    cur = get_cursor(conn)
    cur.execute("""
        SELECT research_topic,
               COUNT(*) AS n,
               AVG(tone_score)::numeric(4,3) AS mean_tone,
               STDDEV(tone_score)::numeric(4,3) AS std_tone,
               COUNT(*) FILTER (WHERE tone_label = 'negative') AS n_negative,
               COUNT(*) FILTER (WHERE tone_label = 'neutral') AS n_neutral,
               COUNT(*) FILTER (WHERE tone_label = 'positive') AS n_positive
        FROM historical_headlines
        WHERE tone_score IS NOT NULL
        GROUP BY research_topic
        ORDER BY research_topic
    """)
    
    print(f"\n{'Topic':<15s} {'N':>6s} {'Mean':>8s} {'Std':>8s} "
          f"{'%neg':>6s} {'%neu':>6s} {'%pos':>6s}")
    print("-" * 70)
    for r in cur.fetchall():
        n = r["n"]
        print(f"{r['research_topic']:<15s} {n:>6,} "
              f"{r['mean_tone']:>8} {r['std_tone']:>8} "
              f"{r['n_negative']/n*100:>5.1f}% "
              f"{r['n_neutral']/n*100:>5.1f}% "
              f"{r['n_positive']/n*100:>5.1f}%")
    conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--topic", default=None,
                        help="research_topic value, or omit for all topics")
    parser.add_argument("--all", action="store_true",
                        help="Process all topics (same as omitting --topic)")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--stats-only", action="store_true",
                        help="Just show stats, don't score")
    args = parser.parse_args()
    
    if args.stats_only:
        show_tone_stats()
        return
    
    topic = None if args.all else args.topic
    score_topic(topic, batch_size=args.batch_size)
    show_tone_stats()


if __name__ == "__main__":
    main()
