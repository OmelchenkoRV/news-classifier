"""
COVID Exercise Analysis
========================

Reads the bdba_retrospective table and produces:
  1. Timeline chart: news volume + alerts + market levels overlaid
  2. Lead time analysis: when first alert vs when first market move
  3. False positive analysis: alerts during 2014 Ebola, 2015 MERS controls
  4. Summary report

Outputs:
  - covid_timeline.png  (matplotlib chart)
  - covid_analysis.md   (markdown report with tables and findings)

Usage:
    python -m research.covid_exercise.analyze
"""

import os
import sys
import argparse
import logging
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from config.database import get_connection, get_cursor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# Output directory — relative to project root, works on Windows and Linux
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "research", "covid_exercise", "outputs")


def install_matplotlib():
    try:
        import matplotlib  # noqa
        return True
    except ImportError:
        log.info("Installing matplotlib...")
        import subprocess
        # Try with --break-system-packages first (Linux/Mac), fall back to plain
        cmds = [
            [sys.executable, "-m", "pip", "install", "matplotlib", "--break-system-packages", "--quiet"],
            [sys.executable, "-m", "pip", "install", "matplotlib", "--quiet"],
        ]
        for cmd in cmds:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode == 0:
                    return True
            except Exception:
                continue
        log.error("Could not install matplotlib. Run manually: pip install matplotlib")
        return False


def load_timeline(cur, research_topic: str, bdba_topic: str = "pandemic_signal"):
    cur.execute("""
        SELECT snapshot_date, headlines_24h, headlines_7d, z_score,
               unique_domains_24h, tier1_count_24h,
               would_alert, alert_severity, alert_reasons,
               spy_close, spy_return_5d_fwd, spy_return_30d_fwd,
               vix_close, btc_close
        FROM bdba_retrospective
        WHERE research_topic = %s AND bdba_topic = %s
        ORDER BY snapshot_date
    """, (research_topic, bdba_topic))
    return cur.fetchall()


def load_tone_data(cur, research_topic: str):
    """Load daily tone aggregates and drift signals."""
    cur.execute("""
        SELECT tone_date, n_headlines, mean_tone, pct_negative, pct_positive,
               cascade_score, cascade_word_count
        FROM bdba_daily_tone
        WHERE research_topic = %s
        ORDER BY tone_date
    """, (research_topic,))
    return cur.fetchall()


def detect_drift_for_report(cur, research_topic: str, window_days: int = 14):
    """Compute drift series for the report."""
    rows = load_tone_data(cur, research_topic)
    if len(rows) < window_days * 2:
        return []
    
    drifts = []
    for i in range(len(rows)):
        if i < window_days * 2 - 1:
            drifts.append(None)
            continue
        recent = rows[i - window_days + 1 : i + 1]
        prior = rows[i - 2*window_days + 1 : i - window_days + 1]
        recent_w = sum(r["n_headlines"] for r in recent)
        prior_w = sum(r["n_headlines"] for r in prior)
        if recent_w == 0 or prior_w == 0:
            drifts.append(None)
            continue
        recent_mean = sum(r["mean_tone"] * r["n_headlines"] for r in recent) / recent_w
        prior_mean = sum(r["mean_tone"] * r["n_headlines"] for r in prior) / prior_w
        drifts.append(recent_mean - prior_mean)
    return drifts


def compute_drift_series(tone_rows, window_days: int = 14):
    """Compute rolling drift for a series of tone rows."""
    if len(tone_rows) < window_days * 2:
        return [None] * len(tone_rows), [None] * len(tone_rows)
    
    drifts = [None] * len(tone_rows)
    rolling_means = [None] * len(tone_rows)
    
    for i in range(len(tone_rows)):
        if i >= window_days - 1:
            # Recent window weighted mean
            recent = tone_rows[i - window_days + 1 : i + 1]
            total_w = sum(r["n_headlines"] for r in recent)
            if total_w > 0:
                rolling_means[i] = sum(
                    r["mean_tone"] * r["n_headlines"] for r in recent
                ) / total_w
            
        if i >= window_days * 2 - 1:
            prior = tone_rows[i - 2*window_days + 1 : i - window_days + 1]
            prior_w = sum(r["n_headlines"] for r in prior)
            if prior_w > 0 and rolling_means[i] is not None:
                prior_mean = sum(
                    r["mean_tone"] * r["n_headlines"] for r in prior
                ) / prior_w
                drifts[i] = rolling_means[i] - prior_mean
    
    return rolling_means, drifts


def plot_timeline(rows, research_topic: str, output_path: str):
    if not install_matplotlib():
        log.warning("Skipping plot — matplotlib unavailable")
        return
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    
    if not rows:
        log.warning(f"No data for {research_topic}, skipping plot")
        return
    
    dates = [r["snapshot_date"] for r in rows]
    headlines = [r["headlines_24h"] or 0 for r in rows]
    tier1 = [r["tier1_count_24h"] or 0 for r in rows]
    spy = [r["spy_close"] for r in rows]
    vix = [r["vix_close"] for r in rows]
    
    alert_dates = [r["snapshot_date"] for r in rows if r["would_alert"]]
    
    # Load tone data if available
    conn_tone = get_connection()
    cur_tone = get_cursor(conn_tone)
    tone_rows = load_tone_data(cur_tone, research_topic)
    conn_tone.close()
    
    has_tone = len(tone_rows) > 0
    
    if has_tone:
        rolling_tones, drifts = compute_drift_series(tone_rows, window_days=14)
        
        fig, axes = plt.subplots(4, 1, figsize=(14, 12), sharex=True,
                                  gridspec_kw={"height_ratios": [2, 1.5, 1, 2]})
    else:
        fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True,
                                  gridspec_kw={"height_ratios": [2, 1, 2]})
    
    # ── Top: news volume ───────────────────────────────────────────
    ax = axes[0]
    ax.bar(dates, headlines, color="#888", alpha=0.5, label="All sources")
    ax.bar(dates, tier1, color="#185FA5", alpha=0.9, label="Tier 1 (mainstream)")
    
    # Alert markers
    for ad in alert_dates:
        ax.axvline(ad, color="#A2154F", linewidth=0.5, alpha=0.4)
    
    ax.set_ylabel("Headlines per day")
    ax.set_title(f"BDBA pandemic_signal surveillance — {research_topic}")
    ax.legend(loc="upper left")
    ax.grid(True, alpha=0.3)
    
    # ── Tone panel (if available) ──────────────────────────────────
    if has_tone:
        ax_tone = axes[1]
        tone_dates = [r["tone_date"] for r in tone_rows]
        daily_means = [r["mean_tone"] for r in tone_rows]
        
        # Daily mean as faint scatter
        ax_tone.scatter(tone_dates, daily_means, s=8, color="#888", alpha=0.4,
                        label="Daily mean tone")
        
        # 14d rolling mean as bold line
        rolling_dates = [d for d, m in zip(tone_dates, rolling_tones) if m is not None]
        rolling_vals = [m for m in rolling_tones if m is not None]
        if rolling_vals:
            ax_tone.plot(rolling_dates, rolling_vals,
                         color="#185FA5", linewidth=2,
                         label="14d weighted mean")
        
        # Zero reference line
        ax_tone.axhline(0, color="black", linewidth=0.5, alpha=0.3)
        
        # Alert markers
        for ad in alert_dates:
            ax_tone.axvline(ad, color="#A2154F", linewidth=0.5, alpha=0.4)
        
        ax_tone.set_ylabel("Tone (-1 to +1)")
        ax_tone.legend(loc="upper left", fontsize=8)
        ax_tone.grid(True, alpha=0.3)
        
        # Cascade score as separate small panel
        ax_cascade = axes[2]
        cascade_scores = [r["cascade_score"] or 0 for r in tone_rows]
        ax_cascade.bar(tone_dates, cascade_scores, color="#D04A0C", alpha=0.6)
        ax_cascade.set_ylabel("Cascade\nscore", fontsize=9)
        ax_cascade.grid(True, alpha=0.3)
        
        vix_idx = 3
        spy_idx = 3  # combined VIX+SPY into bottom panel
    else:
        vix_idx = 1
        spy_idx = 2
    
    # ── VIX ────────────────────────────────────────────────────────
    if has_tone:
        # Skip separate VIX panel when tone is shown - too crowded
        # Show SPY only
        pass
    else:
        ax = axes[vix_idx]
        valid_vix = [(d, v) for d, v in zip(dates, vix) if v is not None]
        if valid_vix:
            vd, vv = zip(*valid_vix)
            ax.plot(vd, vv, color="#D04A0C", linewidth=1.5)
        ax.set_ylabel("VIX")
        ax.grid(True, alpha=0.3)
    
    # ── Bottom: SPY (with VIX overlay if tone shown) ───────────────
    ax = axes[spy_idx]
    valid_spy = [(d, s) for d, s in zip(dates, spy) if s is not None]
    if valid_spy:
        sd, ss = zip(*valid_spy)
        ax.plot(sd, ss, color="#1D9E75", linewidth=1.5, label="SPY ($)")
    
    if has_tone:
        # Overlay VIX on twin axis when tone panel exists
        ax_vix = ax.twinx()
        valid_vix = [(d, v) for d, v in zip(dates, vix) if v is not None]
        if valid_vix:
            vd, vv = zip(*valid_vix)
            ax_vix.plot(vd, vv, color="#D04A0C", linewidth=1.2, alpha=0.7,
                        label="VIX")
            ax_vix.set_ylabel("VIX", color="#D04A0C")
            ax_vix.tick_params(axis='y', labelcolor="#D04A0C")
    
    # Alert markers on SPY too for visual correlation
    for ad in alert_dates:
        ax.axvline(ad, color="#A2154F", linewidth=0.5, alpha=0.4)
    
    ax.set_ylabel("SPY close ($)")
    ax.set_xlabel("Date")
    ax.grid(True, alpha=0.3)
    
    # Format x-axis dates - tick every 2 weeks on Mondays
    for ax in axes:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
        ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO, interval=2))
    
    plt.setp(axes[-1].xaxis.get_majorticklabels(), rotation=45, ha="right")
    plt.tight_layout()
    plt.savefig(output_path, dpi=130, bbox_inches="tight")
    plt.close()
    log.info(f"Saved: {output_path}")


def compute_lead_time(rows):
    """When did first alert fire vs first major market drop."""
    first_alert = None
    first_drop_5d = None
    first_drop_2pct = None
    first_drop_5pct = None
    peak_vix_date = None
    peak_vix = 0
    
    for r in rows:
        if r["would_alert"] and not first_alert:
            first_alert = r["snapshot_date"]
        if r["spy_return_5d_fwd"] and r["spy_return_5d_fwd"] < -0.02 and not first_drop_2pct:
            first_drop_2pct = r["snapshot_date"]
        if r["spy_return_5d_fwd"] and r["spy_return_5d_fwd"] < -0.05 and not first_drop_5pct:
            first_drop_5pct = r["snapshot_date"]
        if r["vix_close"] and r["vix_close"] > peak_vix:
            peak_vix = r["vix_close"]
            peak_vix_date = r["snapshot_date"]
    
    return {
        "first_alert": first_alert,
        "first_drop_2pct": first_drop_2pct,
        "first_drop_5pct": first_drop_5pct,
        "peak_vix": peak_vix,
        "peak_vix_date": peak_vix_date,
        "lead_2pct_days": (first_drop_2pct - first_alert).days
            if first_alert and first_drop_2pct else None,
        "lead_5pct_days": (first_drop_5pct - first_alert).days
            if first_alert and first_drop_5pct else None,
    }


def alert_summary(rows):
    """Summary stats on alert rate and false positives."""
    if not rows:
        return None
    
    total_days = len(rows)
    alerts = [r for r in rows if r["would_alert"]]
    
    by_severity = {}
    for a in alerts:
        sev = a["alert_severity"] or "unknown"
        by_severity[sev] = by_severity.get(sev, 0) + 1
    
    return {
        "total_days": total_days,
        "alert_days": len(alerts),
        "alert_rate_pct": len(alerts) / total_days * 100,
        "by_severity": by_severity,
        "first_alert": alerts[0]["snapshot_date"] if alerts else None,
        "last_alert": alerts[-1]["snapshot_date"] if alerts else None,
    }


def write_report(output_path: str):
    conn = get_connection()
    cur = get_cursor(conn)
    
    # Pull all research topics
    cur.execute("""
        SELECT DISTINCT research_topic FROM bdba_retrospective
        ORDER BY research_topic
    """)
    topics = [r["research_topic"] for r in cur.fetchall()]
    
    sections = []
    sections.append("# COVID BDBA Exercise — Analysis Results\n")
    sections.append(f"_Generated: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}_\n")
    sections.append("\n## Summary across all research topics\n")
    sections.append("| Topic | Days | Alerts | Alert rate | First alert | Last alert |")
    sections.append("|-------|------|--------|-----------|-------------|------------|")
    
    summaries = {}
    for topic in topics:
        rows = load_timeline(cur, topic)
        s = alert_summary(rows)
        if s:
            summaries[topic] = (s, rows)
            sections.append(
                f"| {topic} | {s['total_days']} | {s['alert_days']} | "
                f"{s['alert_rate_pct']:.1f}% | "
                f"{s['first_alert'] or '-'} | {s['last_alert'] or '-'} |"
            )
    
    # Detail per topic
    for topic, (summary, rows) in summaries.items():
        lead = compute_lead_time(rows)
        sections.append(f"\n## {topic}\n")
        sections.append(f"**Total days simulated:** {summary['total_days']}")
        sections.append(f"**Alerts fired:** {summary['alert_days']} "
                        f"({summary['alert_rate_pct']:.1f}% of days)")
        sections.append("")
        
        if summary["by_severity"]:
            sections.append("**By severity:**")
            for sev, count in summary["by_severity"].items():
                sections.append(f"- `{sev}`: {count}")
            sections.append("")
        
        sections.append("**Lead time analysis:**\n")
        sections.append(f"- First alert: `{lead['first_alert']}`")
        sections.append(f"- First SPY 5d drop > 2%: `{lead['first_drop_2pct']}`")
        sections.append(f"- First SPY 5d drop > 5%: `{lead['first_drop_5pct']}`")
        if lead["lead_2pct_days"] is not None:
            sections.append(f"- **Lead time to first 2% drop: {lead['lead_2pct_days']} days**")
        if lead["lead_5pct_days"] is not None:
            sections.append(f"- **Lead time to first 5% drop: {lead['lead_5pct_days']} days**")
        if lead["peak_vix"]:
            sections.append(f"- Peak VIX: {lead['peak_vix']:.1f} on {lead['peak_vix_date']}")
        sections.append("")
        
        # Show first 10 alerts in detail
        alerts = [r for r in rows if r["would_alert"]][:10]
        if alerts:
            sections.append("**First alerts in detail:**\n")
            sections.append("| Date | 24h count | 7d count | z-score | tier1 | severity | reasons | SPY +5d | SPY +30d |")
            sections.append("|------|-----------|----------|---------|-------|----------|---------|---------|----------|")
            for a in alerts:
                spy5 = f"{a['spy_return_5d_fwd']*100:+.1f}%" if a["spy_return_5d_fwd"] is not None else "-"
                spy30 = f"{a['spy_return_30d_fwd']*100:+.1f}%" if a["spy_return_30d_fwd"] is not None else "-"
                reasons = ",".join(a["alert_reasons"] or [])
                sections.append(
                    f"| {a['snapshot_date']} | {a['headlines_24h']} | "
                    f"{a['headlines_7d']} | {a['z_score']:.1f} | "
                    f"{a['tier1_count_24h']} | {a['alert_severity']} | "
                    f"{reasons} | {spy5} | {spy30} |"
                )
            sections.append("")
    
    conn.close()
    
    # Compare topics for false positive analysis
    if len(summaries) > 1:
        sections.append("\n## Specificity test (false positive analysis)\n")
        sections.append(
            "If the BDBA system fires alerts on COVID, it should NOT fire similarly "
            "for past disease outbreaks that didn't crash markets (Ebola 2014, MERS 2015).\n"
        )
        sections.append("| Topic | Alert rate | Peak VIX | Conclusion |")
        sections.append("|-------|-----------|----------|------------|")
        for topic, (summary, rows) in summaries.items():
            lead = compute_lead_time(rows)
            conclusion = "high alert + market reaction" if summary["alert_rate_pct"] > 5 and lead["peak_vix"] and lead["peak_vix"] > 30 else \
                "alerts but no crash" if summary["alert_rate_pct"] > 5 else \
                "low alerts (good null)"
            sections.append(
                f"| {topic} | {summary['alert_rate_pct']:.1f}% | "
                f"{lead['peak_vix']:.1f} | {conclusion} |"
            )
    
    # Tone drift analysis section
    conn_tone = get_connection()
    cur_tone = get_cursor(conn_tone)
    cur_tone.execute("""
        SELECT DISTINCT research_topic FROM bdba_daily_tone ORDER BY research_topic
    """)
    tone_topics = [r["research_topic"] for r in cur_tone.fetchall()]
    
    if tone_topics:
        sections.append("\n## Tone drift analysis\n")
        sections.append(
            "Tone is computed per-headline using FinBERT-tone (a transformer "
            "trained on financial news sentiment). Daily mean tone is "
            "weighted by headline count. Drift = recent 14d weighted mean "
            "minus prior 14d weighted mean.\n"
        )
        sections.append("Negative drift = coverage tone deteriorating over time.\n")
        
        sections.append("\n| Topic | Days scored | Mean tone | Min daily mean | Max negative drift | Drift trend |")
        sections.append("|-------|-------------|-----------|----------------|-------------------|-------------|")
        
        for topic in tone_topics:
            cur_tone.execute("""
                SELECT COUNT(*) AS n,
                       AVG(mean_tone)::numeric(4,3) AS overall_mean,
                       MIN(mean_tone)::numeric(4,3) AS min_daily
                FROM bdba_daily_tone
                WHERE research_topic = %s
            """, (topic,))
            stats = cur_tone.fetchone()
            
            drifts = detect_drift_for_report(cur_tone, topic)
            min_drift = min((d for d in drifts if d is not None), default=None)
            
            # Detect trend - is drift getting more negative over time?
            negative_drift_days = sum(1 for d in drifts if d is not None and d < -0.1)
            trend = "monotonic worsening" if negative_drift_days > len(drifts) * 0.3 else \
                    "stable" if negative_drift_days < len(drifts) * 0.1 else \
                    "intermittent stress"
            
            min_drift_str = f"{min_drift:.3f}" if min_drift is not None else "-"
            sections.append(
                f"| {topic} | {stats['n']} | {stats['overall_mean']} | "
                f"{stats['min_daily']} | {min_drift_str} | {trend} |"
            )
    conn_tone.close()
    
    sections.append("\n## Methodology notes\n")
    sections.append(
        "- Keywords are PRE-COVID-defensible: generic outbreak language, "
        "no COVID-specific terms\n"
        "- Baseline window: 30 days, recomputed each day (no future data leak)\n"
        "- Z threshold: 3.0 for frequency anomaly\n"
        "- Tier 1 sources = Reuters, Bloomberg, WSJ, NYT, FT, BBC, etc.\n"
        "- Forward returns computed against snapshot day's SPY close\n"
        "- Tone scoring: FinBERT-tone (yiyanghkust/finbert-tone), 110M params\n"
        "- Tone drift: 14d weighted mean change over 28d total window\n"
    )
    
    report_text = "\n".join(sections)
    with open(output_path, "w") as f:
        f.write(report_text)
    log.info(f"Saved report: {output_path}")
    return report_text


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    conn = get_connection()
    cur = get_cursor(conn)
    
    # Pull all topics and generate plots
    cur.execute("""
        SELECT DISTINCT research_topic FROM bdba_retrospective
        ORDER BY research_topic
    """)
    topics = [r["research_topic"] for r in cur.fetchall()]
    conn.close()
    
    # Plots
    for topic in topics:
        conn2 = get_connection()
        cur2 = get_cursor(conn2)
        rows = load_timeline(cur2, topic)
        conn2.close()
        plot_timeline(
            rows, topic,
            os.path.join(OUTPUT_DIR, f"bdba_{topic}_timeline.png"),
        )
    
    # Combined report
    write_report(os.path.join(OUTPUT_DIR, "covid_analysis.md"))


if __name__ == "__main__":
    main()
