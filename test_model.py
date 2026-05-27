from inference.classifier import HeadlineClassifierInference

clf = HeadlineClassifierInference()

# Test with real headlines from the Feb crash
headlines = [
    "Trump announces 15% global tariff hike on all imports",
    "Bitcoin RSI shows oversold conditions at key support level",
    "Fed holds interest rates steady, signals no cuts until Q3",
    "Binance halts all ETH withdrawals citing technical issues",
    "Why Bitcoin could reach $200,000 by end of 2026",
    "Iran-US ceasefire announced after weeks of military buildup",
    "Ethereum Pectra upgrade successfully deployed on mainnet",
    "Top 5 altcoins to buy this week for 100x gains",
]

for h in headlines:
    r = clf.classify(h)
    gate = "TIGHT" if r["should_tighten_gates"] else "-"
    is_causal = r["category"] not in {"technical_analysis", "price_commentary", "opinion", "promotion"}
    print(f"{h[:60]:62s} {r['category']:20s} causal={is_causal}  gate={gate}")