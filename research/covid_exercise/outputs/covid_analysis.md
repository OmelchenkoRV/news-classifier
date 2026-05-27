# COVID BDBA Exercise � Analysis Results




## Summary across all research topics

| Topic | Days | Alerts | Alert rate | First alert | Last alert |
|-------|------|--------|-----------|-------------|------------|
| covid | 152 | 0 | 0.0% | - | - |
| ebola_2018 | 487 | 2 | 0.4% | 2019-05-22 | 2019-12-18 |

## covid

**Total days simulated:** 152
**Alerts fired:** 0 (0.0% of days)

**Lead time analysis:**

- First alert: `None`
- First SPY 5d drop > 2%: `2020-01-20`
- First SPY 5d drop > 5%: `2020-02-20`
- Peak VIX: 82.7 on 2020-03-14


## ebola_2018

**Total days simulated:** 487
**Alerts fired:** 2 (0.4% of days)

**By severity:**
- `watch`: 2

**Lead time analysis:**

- First alert: `2019-05-22`
- First SPY 5d drop > 2%: `2019-09-27`
- First SPY 5d drop > 5%: `None`
- **Lead time to first 2% drop: 128 days**
- Peak VIX: 20.6 on 2019-10-02

**First alerts in detail:**

| Date | 24h count | 7d count | z-score | tier1 | severity | reasons | SPY +5d | SPY +30d |
|------|-----------|----------|---------|-------|----------|---------|---------|----------|
| 2019-05-22 | 27 | 0 | 3.2 | 3 | watch | frequency,tier1_diversification,legacy_freq_tier1 | - | - |
| 2019-12-18 | 32 | 1 | 3.2 | 4 | watch | frequency,tier1_diversification,legacy_freq_tier1 | +0.5% | +3.9% |


## Specificity test (false positive analysis)

If the BDBA system fires alerts on COVID, it should NOT fire similarly for past disease outbreaks that didn't crash markets (Ebola 2014, MERS 2015).

| Topic | Alert rate | Peak VIX | Conclusion |
|-------|-----------|----------|------------|
| covid | 0.0% | 82.7 | low alerts (good null) |
| ebola_2018 | 0.4% | 20.6 | low alerts (good null) |

## Tone drift analysis

Tone is computed per-headline using FinBERT-tone (a transformer trained on financial news sentiment). Daily mean tone is weighted by headline count. Drift = recent 14d weighted mean minus prior 14d weighted mean.

Negative drift = coverage tone deteriorating over time.


| Topic | Days scored | Mean tone | Min daily mean | Max negative drift | Drift trend |
|-------|-------------|-----------|----------------|-------------------|-------------|
| covid | 35 | -0.481 | -1.000 | -0.168 | stable |
| ebola_2018 | 82 | -0.480 | -1.000 | -0.421 | intermittent stress |

## Methodology notes

- Keywords are PRE-COVID-defensible: generic outbreak language, no COVID-specific terms
- Baseline window: 30 days, recomputed each day (no future data leak)
- Z threshold: 3.0 for frequency anomaly
- Tier 1 sources = Reuters, Bloomberg, WSJ, NYT, FT, BBC, etc.
- Forward returns computed against snapshot day's SPY close
- Tone scoring: FinBERT-tone (yiyanghkust/finbert-tone), 110M params
- Tone drift: 14d weighted mean change over 28d total window
