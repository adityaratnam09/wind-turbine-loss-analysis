#!/usr/bin/env python3
"""
wind_turbine_loss_analysis.py

A reproducible framework for diagnosing where a wind turbine loses potential
generation, using nothing but the daily operational reading log that most
turbine operators already maintain (no SCADA subsystem data required).

The script runs nine analyses, in logical sequence, over a daily log recording
generation (kWh) and five time categories per day - Run, Grid-Down, Breakdown,
Maintenance, and Lull (low wind) - plus a free-text remarks field:

  1. Loss decomposition       - how is total time split across the five categories,
                                 and what does the remarks field say caused the losses?
  2. Seasonal pattern          - how does generation and lull time vary by month?
  3. Export balance            - does metered export track generation consistently?
  4. Temporal trend            - has generation-per-run-hour changed year over year?
  5. Reliability (MTBF/MTTR)   - how often do failures happen, and for how long,
                                 with bootstrap confidence intervals?
  6. Outage clustering         - do failures cluster in time, tested against a
                                 Monte-Carlo-corrected Kolmogorov-Smirnov test?
  7. Cause vs. season          - is the documented cause of loss associated with
                                 season (chi-square, with a merged-category and a
                                 permutation-test robustness check)?
  8. Seasonal significance     - is the seasonal pattern statistically real
                                 (Kruskal-Wallis), not just a chart impression?
  9. Economic sensitivity      - what is each cause worth, using month-specific
                                 generation rates and a tariff sweep?

Usage:
    python wind_turbine_loss_analysis.py [path/to/daily_log.xls]
    python wind_turbine_loss_analysis.py --tariff 4.5 5.5
    python wind_turbine_loss_analysis.py --only reliability clustering
    python wind_turbine_loss_analysis.py --help

If no path is given, the script looks for a file named turbine_daily_log.xls
inside a data/ subfolder next to this script. All output tables (.csv) and charts (.png)
are written to output/, created automatically on first run.

Requires: pandas, numpy, scipy, matplotlib, xlrd (see requirements.txt).
"""
import os
import re
import sys
import argparse
import datetime
import warnings

import numpy as np
import pandas as pd
import xlrd
from scipy import stats
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# ============================================================================
# Constants
# ============================================================================

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_PATH = os.path.join(HERE, 'data', 'turbine_daily_log.xls')
DEFAULT_OUTPUT_DIR = os.path.join(HERE, 'output')

MONTH_MAP = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
    'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6, 'jul': 7, 'july': 7,
    'aug': 8, 'august': 8, 'sep': 9, 'sept': 9, 'september': 9,
    'oct': 10, 'october': 10, 'nov': 11, 'november': 11, 'dec': 12, 'december': 12,
}
MONTH_NAMES = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

COLUMNS = [
    'date', 'year', 'month', 'day',
    'g1_kwh', 'g2_kwh', 'total_kwh',
    'run_hrs', 'gd_hrs', 'bd_hrs', 'main_hrs', 'lull_hrs',
    'eb_export', 'eb_import', 'eb_net_export',
    'rkvah_imp', 'rkvar_pct', 'ma_pct', 'ga_pct', 'remarks',
]

# Rule-based keyword classifier for the free-text remarks field. This is a
# transparent, first-pass classifier, not a trained model - there is no
# labeled dataset of "true" cause categories to fit one against. Categories
# are checked in the order below; the first match wins. Full lexicon is
# printed to output/keyword_lexicon.csv by analysis_loss_decomposition() so
# it can be audited or extended without reading this source file.
CAUSE_RULES = {
    'Weather (rain/lightning)': [r'\brain\b', r'\blightning\b', r'\bstorm\b', r'\bcyclone\b'],
    'Grid infrastructure (EB/feeder/line)': [
        r'\beb\b', r'\bfeeder\b', r'\bline\b', r'\btrip', r'\bpole', r'\btransformer\b',
        r'\bsubstation\b', r'33kv', r'11kv', r'\bkv\b',
    ],
    'Vegetation (trees)': [r'\btree'],
    'Maintenance / rework': [
        r'maintenance', r'rework', r're work', r'\bservic', r'\binspect', r'\bpatrol',
        r'\bschedul', r'preventive',
    ],
    'Mechanical/electrical fault': [
        r'\bfault\b', r'\bbrake\b', r'\bnacelle\b', r'\bgear', r'\binsulator\b',
        r'\bbearing\b', r'\bblade\b', r'sensor', r'hydraulic', r'yaw',
    ],
    'Low wind': [r'low wind', r'\blull\b', r'wind speed'],
}

# Coarser 5-group scheme used for the chi-square robustness check (Section 7).
CAUSE_MERGE_MAP = {
    'Low wind': 'Wind/resource',
    'Grid infrastructure (EB/feeder/line)': 'Grid',
    'Mechanical/electrical fault': 'Equipment',
    'Maintenance / rework': 'Equipment',
    'Weather (rain/lightning)': 'Weather',
    'Vegetation (trees)': 'Other/unknown',
    'Other / unclassified': 'Other/unknown',
}

SEASON_MAP = {12: 'Winter', 1: 'Winter', 2: 'Winter',
              3: 'Summer', 4: 'Summer', 5: 'Summer',
              6: 'Monsoon', 7: 'Monsoon', 8: 'Monsoon', 9: 'Monsoon',
              10: 'Post-Monsoon', 11: 'Post-Monsoon'}
SEASON_ORDER = ['Winter', 'Summer', 'Monsoon', 'Post-Monsoon']

DEFAULT_TARIFFS = [3.0, 4.0, 5.0, 6.0]  # Rs/kWh - PLACEHOLDER, override with --tariff
N_BOOTSTRAP = 2000    # resamples for reliability confidence intervals
N_MONTE_CARLO = 2000  # resamples for the KS test's Monte Carlo p-value correction
RNG_SEED = 20260905    # fixed seed so bootstrap/Monte Carlo results are reproducible run to run


# ============================================================================
# 0. Data loading
# ============================================================================

def _parse_month_sheet(name):
    """Normalize an inconsistent sheet name (e.g. 'April2022', 'April 2026',
    'JULY2025') into a (year, month) tuple."""
    s = name.strip().lower().replace('-', ' ')
    m = re.match(r'([a-z]+)\s*(\d{4})', s)
    if not m:
        return None
    mon_str, year = m.groups()
    mon = MONTH_MAP.get(mon_str)
    return (int(year), mon) if mon else None


def _safe_float(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def load_daily_log(xls_path=None):
    """Read every monthly sheet in the daily reading workbook into one tidy
    DataFrame. Defaults to DEFAULT_DATA_PATH (data/turbine_daily_log.xls next to this script)."""
    xls_path = xls_path or DEFAULT_DATA_PATH
    if not os.path.exists(xls_path):
        raise FileNotFoundError(
            f"Could not find the source workbook at: {xls_path}\n"
            f"Put your daily reading .xls in a data/ subfolder next to this script, "
            f"named 'turbine_daily_log.xls', or pass its path as the first argument."
        )
    wb = xlrd.open_workbook(xls_path)
    rows = []
    for sh in wb.sheets():
        ym = _parse_month_sheet(sh.name)
        if ym is None:
            continue
        year, month = ym
        for r in range(2, sh.nrows):
            vals = [sh.cell_value(r, c) for c in range(sh.ncols)] + [''] * (20 - sh.ncols)
            day_raw = vals[0]
            if day_raw in ('', 'Total'):
                continue
            try:
                day = int(day_raw)
                d = datetime.date(year, month, day)
            except (ValueError, TypeError):
                continue
            rows.append([
                d.isoformat(), year, month, day,
                _safe_float(vals[4]), _safe_float(vals[5]), _safe_float(vals[6]),
                _safe_float(vals[7]), _safe_float(vals[8]), _safe_float(vals[9]),
                _safe_float(vals[10]), _safe_float(vals[11]),
                _safe_float(vals[12]), _safe_float(vals[13]), _safe_float(vals[14]),
                _safe_float(vals[15]), _safe_float(vals[16]),
                _safe_float(vals[17]), _safe_float(vals[18]),
                str(vals[19]).strip() if len(vals) > 19 else '',
            ])
    df = pd.DataFrame(rows, columns=COLUMNS)
    df['date'] = pd.to_datetime(df['date'])
    return df.sort_values('date').reset_index(drop=True)


def classify_remark(remark):
    """Classify one day's free-text remark into a cause category using the
    keyword rules in CAUSE_RULES. Returns 'No remark' if empty, or
    'Other / unclassified' if populated but no keyword matches."""
    if not remark or not remark.strip():
        return 'No remark'
    text = remark.lower()
    for cause, patterns in CAUSE_RULES.items():
        if any(re.search(p, text) for p in patterns):
            return cause
    return 'Other / unclassified'


# ============================================================================
# Shared event-extraction helper (used by reliability and clustering analyses)
# ============================================================================

def extract_events(df, hours_col):
    """Turn a daily hours-in-category column into discrete events: a run of
    one or more consecutive calendar days with hours > 0 is one event, not
    several independent one-day events."""
    d = df[['date', hours_col]].copy().sort_values('date').reset_index(drop=True)
    d['active'] = d[hours_col] > 0
    d['group'] = (d['active'] != d['active'].shift(1)).cumsum()
    events = []
    for _, g in d[d['active']].groupby('group'):
        events.append({
            'start_date': g['date'].min(),
            'end_date': g['date'].max(),
            'duration_days': (g['date'].max() - g['date'].min()).days + 1,
            'total_hours': g[hours_col].sum(),
        })
    return pd.DataFrame(events).sort_values('start_date').reset_index(drop=True)


def inter_arrival_days(events_df):
    """Days between the end of one event and the start of the next."""
    if len(events_df) < 2:
        return pd.Series(dtype=float)
    gaps = events_df['start_date'].values[1:] - events_df['end_date'].values[:-1]
    return pd.Series(gaps).dt.days.astype(float)


# ============================================================================
# 1. Loss decomposition: where is energy lost, and why?
# ============================================================================

def analysis_loss_decomposition(df, out_dir):
    print("\n" + "=" * 70)
    print("1. LOSS DECOMPOSITION")
    print("=" * 70)

    d = df.copy()
    d['cause_category'] = d['remarks'].apply(classify_remark)
    d['hours_check'] = d[['run_hrs', 'gd_hrs', 'bd_hrs', 'main_hrs', 'lull_hrs']].sum(axis=1)

    monthly = d.groupby(['year', 'month']).agg(
        days=('day', 'count'), total_kwh=('total_kwh', 'sum'),
        run_hrs=('run_hrs', 'sum'), gd_hrs=('gd_hrs', 'sum'),
        bd_hrs=('bd_hrs', 'sum'), main_hrs=('main_hrs', 'sum'), lull_hrs=('lull_hrs', 'sum'),
    ).reset_index()
    possible = monthly['days'] * 24
    for col in ['run_hrs', 'gd_hrs', 'bd_hrs', 'main_hrs', 'lull_hrs']:
        monthly[col.replace('_hrs', '_pct')] = monthly[col] / possible

    # Overall time allocation across the whole dataset
    totals = {c: d[c].sum() for c in ['run_hrs', 'gd_hrs', 'bd_hrs', 'main_hrs', 'lull_hrs']}
    possible_total = len(d) * 24
    print(f"Dataset: {len(d)} days, {possible_total} possible hours")
    for cat, hrs in totals.items():
        print(f"  {cat:10s}: {hrs:>10,.1f} hrs  ({hrs/possible_total*100:5.2f}%)")
    accounted = sum(totals.values())
    print(f"  {'accounted':10s}: {accounted:>10,.1f} hrs  ({accounted/possible_total*100:5.2f}%)")

    # Residual investigation (Reviewer note: don't just assert "rounding" -
    # actually check what the daily residual looks like.)
    d['sum5'] = d[['run_hrs', 'gd_hrs', 'bd_hrs', 'main_hrs', 'lull_hrs']].sum(axis=1)
    d['residual'] = 24 - d['sum5']
    blank_days = d[d['sum5'] == 0]
    nonblank = d[d['sum5'] > 0]
    nonblank_nz = nonblank[nonblank['residual'].abs() > 0.001]
    big_residual = nonblank[nonblank['residual'].abs() > 2]
    print(f"\nResidual audit (24 - sum of 5 categories, per day):")
    print(f"  Fully blank days (all 5 categories = 0, likely missing records): {len(blank_days)} "
          f"({len(blank_days)*24} hours, {len(blank_days)*24/possible_total*100:.2f}% of dataset)")
    print(f"  Non-blank days with |residual| > 2 hours (likely data entry issues): {len(big_residual)}")
    if len(big_residual):
        print(big_residual[['date', 'run_hrs', 'gd_hrs', 'bd_hrs', 'main_hrs', 'lull_hrs', 'residual']]
              .to_string(index=False))
    remaining = nonblank_nz[~nonblank_nz.index.isin(big_residual.index)]
    print(f"  Remaining small residuals (rounding/partial-hour recording): {len(remaining)} days, "
          f"median {remaining['residual'].median():.2f} hrs, "
          f"total {remaining['residual'].sum():.1f} hrs "
          f"({remaining['residual'].sum()/possible_total*100:.3f}% of dataset)")

    # Cause attribution for Grid-Down + Breakdown hours
    d['lost_hrs'] = d['gd_hrs'] + d['bd_hrs']
    causes = (d.groupby('cause_category')
              .agg(days=('date', 'count'), total_lost_hrs=('lost_hrs', 'sum'))
              .sort_values('total_lost_hrs', ascending=False).reset_index())
    print(f"\nCause attribution (Grid-Down + Breakdown hours only):")
    print(causes.to_string(index=False))

    # Selection-bias check: what share of actual lost hours are even covered by a remark?
    d['has_remark'] = d['remarks'].str.strip().str.len() > 0
    total_lost = d['lost_hrs'].sum()
    remarked_lost = d[d['has_remark']]['lost_hrs'].sum()
    print(f"\nRemark coverage check: {d['has_remark'].mean()*100:.1f}% of days have a remark, but "
          f"only {remarked_lost/total_lost*100:.1f}% of total Grid-Down+Breakdown HOURS occur on "
          f"a remarked day. The cause classification below covers a minority of actual lost hours, "
          f"not most of them - a selection-bias caveat, not just a completeness one.")

    # Export the keyword lexicon so it can be audited without reading this source file
    lexicon_rows = [{'cause_category': cause, 'keyword_pattern': pat}
                     for cause, patterns in CAUSE_RULES.items() for pat in patterns]
    pd.DataFrame(lexicon_rows).to_csv(os.path.join(out_dir, 'keyword_lexicon.csv'), index=False)

    # Plot: monthly stacked hour breakdown
    fig, ax = plt.subplots(figsize=(16, 6))
    m = monthly.copy()
    m['period'] = m['year'].astype(str) + '-' + m['month'].astype(str).str.zfill(2)
    bottom = None
    for col, label, color in [
        ('run_hrs', 'Run', '#2ca02c'), ('gd_hrs', 'Grid-Down', '#d62728'),
        ('bd_hrs', 'Breakdown', '#ff7f0e'), ('main_hrs', 'Maintenance', '#9467bd'),
        ('lull_hrs', 'Lull (low wind)', '#1f77b4'),
    ]:
        ax.bar(m['period'], m[col], bottom=bottom, label=label, color=color)
        bottom = m[col] if bottom is None else bottom + m[col]
    ax.set_xticks(range(0, len(m), 3))
    ax.set_xticklabels(m['period'][::3], rotation=90)
    ax.set_ylabel('Hours')
    ax.set_title('Monthly hour breakdown: Run vs Grid-Down vs Breakdown vs Maintenance vs Lull')
    ax.legend(loc='upper right')
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, 'monthly_hours_breakdown.png'), dpi=130)
    plt.close(fig)

    # Plot: cause attribution bar
    fig, ax = plt.subplots(figsize=(9, 5))
    cs = causes.sort_values('total_lost_hrs')
    ax.barh(cs['cause_category'], cs['total_lost_hrs'], color='#c0392b')
    ax.set_xlabel('Total Grid-Down + Breakdown hours attributed')
    ax.set_title('Lost operating hours by classified cause (from remarks)')
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, 'loss_cause_attribution.png'), dpi=130)
    plt.close(fig)

    monthly.to_csv(os.path.join(out_dir, 'monthly_loss_breakdown.csv'), index=False)
    causes.to_csv(os.path.join(out_dir, 'loss_cause_summary.csv'), index=False)
    print(f"\nSaved: monthly_loss_breakdown.csv, loss_cause_summary.csv, keyword_lexicon.csv,")
    print(f"       monthly_hours_breakdown.png, loss_cause_attribution.png")
    return {'monthly': monthly, 'causes': causes, 'd': d}


# ============================================================================
# 2. Seasonal pattern
# ============================================================================

def analysis_seasonal(df, out_dir):
    print("\n" + "=" * 70)
    print("2. SEASONAL PATTERN")
    print("=" * 70)

    g = df.groupby('month').agg(
        total_kwh=('total_kwh', 'sum'), run_hrs=('run_hrs', 'sum'),
        gd_hrs=('gd_hrs', 'sum'), bd_hrs=('bd_hrs', 'sum'),
        lull_hrs=('lull_hrs', 'sum'), n_days=('day', 'count'),
    ).reindex(range(1, 13)).reset_index()
    g['month_name'] = g['month'].map(lambda m: MONTH_NAMES[int(m)])
    g['avg_daily_kwh'] = g['total_kwh'] / g['n_days']
    g['lull_pct_of_possible'] = g['lull_hrs'] / (g['n_days'] * 24)

    print(g[['month_name', 'total_kwh', 'avg_daily_kwh', 'n_days', 'lull_pct_of_possible']]
          .to_string(index=False))

    dead_months = g[g['avg_daily_kwh'] < g['avg_daily_kwh'].mean() * 0.1]
    print(f"\nNear-zero generation months (avg daily kWh < 10% of yearly mean): "
          f"{list(dead_months['month_name'])}")

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    axes[0].bar(g['month_name'], g['total_kwh'], color='#2ca02c')
    axes[0].set_title('Total generation by calendar month (all years combined)')
    axes[0].set_ylabel('kWh')
    axes[0].tick_params(axis='x', rotation=45)
    axes[1].bar(g['month_name'], g['lull_pct_of_possible'] * 100, color='#1f77b4')
    axes[1].set_title('Share of possible hours lost to low wind (lull), by month')
    axes[1].set_ylabel('% of hours')
    axes[1].tick_params(axis='x', rotation=45)
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, 'seasonal_generation_and_lull.png'), dpi=130)
    plt.close(fig)

    g.to_csv(os.path.join(out_dir, 'seasonal_summary.csv'), index=False)
    print(f"\nSaved: seasonal_summary.csv, seasonal_generation_and_lull.png")
    return g


# ============================================================================
# 3. Export balance
# ============================================================================

def analysis_export_balance(df, out_dir):
    print("\n" + "=" * 70)
    print("3. EXPORT BALANCE")
    print("=" * 70)

    yearly = df.groupby('year').agg(
        generation_kwh=('total_kwh', 'sum'), eb_export=('eb_export', 'sum'),
        eb_import=('eb_import', 'sum'), eb_net_export=('eb_net_export', 'sum'),
    ).reset_index()
    yearly['export_over_generation'] = yearly['eb_export'] / yearly['generation_kwh']
    yearly['gap_kwh'] = yearly['generation_kwh'] - yearly['eb_export']
    print(yearly.to_string(index=False))

    d = df.copy()
    d['export_gen_ratio'] = d['eb_export'] / d['total_kwh'].replace(0, pd.NA)
    flagged = d[(d['export_gen_ratio'] > 1.5) | (d['export_gen_ratio'] < 0.5)]
    print(f"\nDays where export/generation ratio is outside [0.5, 1.5]: {len(flagged)}")

    fig, ax = plt.subplots(figsize=(9, 5))
    width = 0.35
    x = range(len(yearly))
    ax.bar([i - width / 2 for i in x], yearly['generation_kwh'], width, label='Generation (G1+G2)', color='#2ca02c')
    ax.bar([i + width / 2 for i in x], yearly['eb_export'], width, label='EB Export reading', color='#1f77b4')
    ax.set_xticks(list(x))
    ax.set_xticklabels(yearly['year'])
    ax.set_ylabel('kWh')
    ax.set_title('Generation vs EB Export reading, by year')
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, 'generation_vs_export_balance.png'), dpi=130)
    plt.close(fig)

    yearly.to_csv(os.path.join(out_dir, 'export_balance_yearly.csv'), index=False)
    flagged.to_csv(os.path.join(out_dir, 'export_balance_flagged_days.csv'), index=False)
    print(f"\nSaved: export_balance_yearly.csv, export_balance_flagged_days.csv, "
          f"generation_vs_export_balance.png")
    return yearly


# ============================================================================
# 4. Temporal trend
# ============================================================================

def analysis_temporal_trend(df, out_dir):
    print("\n" + "=" * 70)
    print("4. TEMPORAL TREND")
    print("=" * 70)

    g = df.groupby('year').agg(generation_kwh=('total_kwh', 'sum'), run_hrs=('run_hrs', 'sum')).reset_index()
    g['kwh_per_run_hr'] = g['generation_kwh'] / g['run_hrs']
    print(g.to_string(index=False))

    full_years = g[g['year'] < g['year'].max()]  # exclude the final, likely-partial year
    slope, intercept, r, p, se = stats.linregress(full_years['year'], full_years['kwh_per_run_hr'])
    print(f"\nLinear trend (excluding final partial year): slope={slope:.2f} kWh/run-hr per year, "
          f"r={r:.3f}, p={p:.3f}, n={len(full_years)} years")
    if p > 0.05:
        print(">>> Not statistically significant - report year-to-year figures, not a fitted trend, "
              "as the finding. Without wind speed data, do not attribute any change to equipment "
              "performance rather than a weaker/stronger wind year.")
    else:
        print(">>> Statistically significant, but still correlational, and confounded by unmeasured "
              "wind resource - report as 'productivity changed over time', not 'the turbine degraded'.")

    fig, ax = plt.subplots(figsize=(8, 5))
    colors = ['#1f77b4' if y < g['year'].max() else '#999999' for y in g['year']]
    ax.bar(g['year'].astype(str), g['kwh_per_run_hr'], color=colors)
    ax.set_ylabel('kWh generated per run hour')
    ax.set_title('Generation intensity by year (grey = final, partial year)')
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, 'yearly_generation_intensity.png'), dpi=130)
    plt.close(fig)

    g.to_csv(os.path.join(out_dir, 'yearly_productivity.csv'), index=False)
    print(f"\nSaved: yearly_productivity.csv, yearly_generation_intensity.png")
    return g


# ============================================================================
# 5. Reliability: MTBF, MTTR, Weibull fit, with bootstrap confidence intervals
# ============================================================================

def _bootstrap_ci(gaps, statistic_fn, n_boot=N_BOOTSTRAP, rng=None):
    """Percentile bootstrap CI for a statistic computed from an array of gaps.
    Resamples gaps with replacement n_boot times; returns (point estimate, 2.5%, 97.5%)."""
    rng = rng or np.random.default_rng(RNG_SEED)
    point = statistic_fn(gaps)
    if len(gaps) < 5:
        return point, np.nan, np.nan
    boot_stats = []
    for _ in range(n_boot):
        resample = rng.choice(gaps, size=len(gaps), replace=True)
        try:
            boot_stats.append(statistic_fn(resample))
        except Exception:
            continue
    if len(boot_stats) < n_boot * 0.5:
        return point, np.nan, np.nan
    lo, hi = np.percentile(boot_stats, [2.5, 97.5])
    return point, lo, hi


def _weibull_k(gaps):
    shape, loc, scale = stats.weibull_min.fit(gaps, floc=0)
    return shape


def _mean_stat(gaps):
    return np.mean(gaps)


def analysis_reliability(df, out_dir):
    print("\n" + "=" * 70)
    print("5. RELIABILITY: MTBF, MTTR, WEIBULL FIT (with bootstrap 95% CIs)")
    print("=" * 70)
    print("Note: Weibull is used here descriptively, to characterize the shape of the")
    print("inter-event gap distribution, not as a full point-process reliability model.")

    rng = np.random.default_rng(RNG_SEED)
    all_metrics = []
    for col, label in [('bd_hrs', 'Breakdown'), ('gd_hrs', 'Grid-Down')]:
        events = extract_events(df, col)
        gaps = inter_arrival_days(events).values
        metrics = {
            'category': label, 'n_events': len(events),
            'mttr_days_mean': events['duration_days'].mean() if len(events) else np.nan,
            'mttr_hours_mean': events['total_hours'].mean() if len(events) else np.nan,
            'n_gaps': len(gaps),
        }
        if len(gaps) >= 5:
            mtbf_point, mtbf_lo, mtbf_hi = _bootstrap_ci(gaps, _mean_stat, rng=rng)
            k_point, k_lo, k_hi = _bootstrap_ci(gaps, _weibull_k, rng=rng)
            shape, loc, scale = stats.weibull_min.fit(gaps, floc=0)
            metrics.update({
                'mtbf_mean_days': mtbf_point, 'mtbf_ci95_lo': mtbf_lo, 'mtbf_ci95_hi': mtbf_hi,
                'mtbf_median_days': np.median(gaps),
                'weibull_k': k_point, 'weibull_k_ci95_lo': k_lo, 'weibull_k_ci95_hi': k_hi,
                'weibull_lambda_days': scale,
            })
            if k_point < 0.9:
                interp = ('k<1: gaps are unevenly distributed, many short gaps with occasional '
                          'long quiet stretches, consistent with temporal clustering')
            elif k_point > 1.1:
                interp = 'k>1: gaps are more regular than random - fairly consistent recurrence'
            else:
                interp = 'k~=1: consistent with a memoryless, randomly timed (Poisson) process'
            metrics['interpretation'] = interp
        else:
            metrics.update({k: np.nan for k in
                             ['mtbf_mean_days', 'mtbf_ci95_lo', 'mtbf_ci95_hi', 'mtbf_median_days',
                              'weibull_k', 'weibull_k_ci95_lo', 'weibull_k_ci95_hi', 'weibull_lambda_days']})
            metrics['interpretation'] = 'Too few events (<5 gaps) for a meaningful fit'
        all_metrics.append(metrics)

        print(f"\n--- {label} ---")
        print(f"Events: {metrics['n_events']}")
        print(f"MTTR (mean event duration): {metrics['mttr_days_mean']:.2f} days "
              f"({metrics['mttr_hours_mean']:.2f} hours)")
        if not np.isnan(metrics.get('mtbf_mean_days', np.nan)):
            print(f"MTBF: {metrics['mtbf_mean_days']:.1f} days "
                  f"[95% CI: {metrics['mtbf_ci95_lo']:.1f}-{metrics['mtbf_ci95_hi']:.1f}], "
                  f"median {metrics['mtbf_median_days']:.1f} days (n={metrics['n_gaps']} gaps)")
            print(f"Weibull k: {metrics['weibull_k']:.2f} "
                  f"[95% CI: {metrics['weibull_k_ci95_lo']:.2f}-{metrics['weibull_k_ci95_hi']:.2f}], "
                  f"lambda={metrics['weibull_lambda_days']:.1f} days")
        print(f">>> {metrics['interpretation']}")

        events.to_csv(os.path.join(out_dir, f'reliability_events_{col.replace("_hrs","")}.csv'), index=False)
        if len(gaps) >= 5:
            fig, ax = plt.subplots(figsize=(8, 5))
            counts, _, _ = ax.hist(gaps, bins=min(15, max(5, len(gaps) // 2)), density=True, alpha=0.6,
                    color='#1f77b4', label='Observed inter-failure gaps')
            x = np.linspace(1e-6, gaps.max() * 1.1, 200)
            weibull_pdf = stats.weibull_min.pdf(x, metrics['weibull_k'], 0, metrics['weibull_lambda_days'])
            ax.plot(x, weibull_pdf,
                    color='#d62728', linewidth=2,
                    label=f"Fitted Weibull (k={metrics['weibull_k']:.2f}, "
                          f"lambda={metrics['weibull_lambda_days']:.1f})")
            ax.set_xlabel('Days between failures')
            ax.set_ylabel('Density')
            ax.set_title(f'{label} inter-failure intervals (n={len(gaps)} gaps)')
            # For a Weibull shape parameter below 1, the fitted density diverges
            # near x=0 and would otherwise dwarf the histogram bars on a linear
            # y-axis. Cap the y-axis to the readable range: the taller of the
            # tallest histogram bar and the curve's height away from the origin
            # (excluding the first 5% of the x-range, where the divergence lives),
            # with headroom. The curve is still drawn in full; only the axis view
            # is cropped, so a k<1 curve will run off the top of the plot near
            # x=0, which is expected and informative rather than an error.
            core = x > (x.max() * 0.05)
            curve_ref = np.percentile(weibull_pdf[core], 99) if core.any() else weibull_pdf.max()
            y_cap = max(counts.max() if len(counts) else 0, curve_ref) * 1.3
            if y_cap > 0:
                ax.set_ylim(0, y_cap)
            ax.legend()
            fig.tight_layout()
            fig.savefig(os.path.join(out_dir, f'reliability_weibull_{col.replace("_hrs","")}.png'), dpi=130)
            plt.close(fig)

    summary = pd.DataFrame(all_metrics)
    summary.to_csv(os.path.join(out_dir, 'reliability_summary.csv'), index=False)
    print(f"\nSaved: reliability_summary.csv, reliability_events_bd.csv, reliability_events_gd.csv,")
    print(f"       reliability_weibull_bd.png, reliability_weibull_gd.png")
    return summary


# ============================================================================
# 6. Outage clustering: KS test with a Monte Carlo p-value correction
# ============================================================================

def _monte_carlo_ks_pvalue(gaps, n_sim=N_MONTE_CARLO, rng=None):
    """A classic one-sample KS test against an exponential distribution whose
    rate is ESTIMATED FROM THE SAME DATA has a p-value that is not exact -
    the asymptotic KS null distribution assumes a fully-specified null, not
    one fitted to the sample (a point Stanford/GPT-style reviews both raised).
    This simulates the null directly: repeatedly draw a synthetic sample of
    the same size from an exponential with the fitted rate, refit the rate to
    THAT sample (mirroring what was done to the real data), and compute its KS
    statistic against its own fitted exponential. The corrected p-value uses the
    standard Monte Carlo estimator (b+1)/(n+1), where b is the number of simulated
    statistics at least as extreme as the one observed - not the naive b/n, since a
    raw count of zero exceedances does not mean the true p-value is exactly zero.
    """
    rng = rng or np.random.default_rng(RNG_SEED)
    mean_gap = np.mean(gaps)
    observed_ks, _ = stats.kstest(gaps, 'expon', args=(0, mean_gap))
    sim_ks = np.empty(n_sim)
    for i in range(n_sim):
        sim = rng.exponential(scale=mean_gap, size=len(gaps))
        sim_mean = sim.mean()
        sim_ks[i], _ = stats.kstest(sim, 'expon', args=(0, sim_mean))
    # Standard Monte Carlo p-value estimator (Davison & Hinkley, 1997): (b+1)/(n+1),
    # not the naive b/n, since a raw count of 0 exceedances does not mean the true
    # p-value is exactly zero - it means the true p-value is bounded above by the
    # simulation's resolution (1/(n+1) here).
    b = int((sim_ks >= observed_ks).sum())
    corrected_p = (b + 1) / (n_sim + 1)
    return observed_ks, corrected_p


def analysis_clustering(df, out_dir):
    print("\n" + "=" * 70)
    print("6. OUTAGE CLUSTERING (Kolmogorov-Smirnov, Monte Carlo-corrected)")
    print("=" * 70)
    print("Framing note: rejecting the exponential/Poisson null means the inter-failure gaps")
    print("are NOT consistent with a homogeneous (constant-rate) random process. That is")
    print("evidence of temporal clustering, but does not by itself prove events are causally")
    print("dependent - a seasonally-varying failure rate could produce the same signature.")
    print("See Limitations for this caveat.")

    rng = np.random.default_rng(RNG_SEED)
    results = []
    for col, label in [('bd_hrs', 'Breakdown'), ('gd_hrs', 'Grid-Down')]:
        events = extract_events(df, col)
        gaps = inter_arrival_days(events).values
        if len(gaps) < 8:
            print(f"\n--- {label}: too few gaps (<8) for a meaningful test ---")
            continue
        mean_gap = gaps.mean()
        cv = gaps.std() / mean_gap
        asymptotic_ks, asymptotic_p = stats.kstest(gaps, 'expon', args=(0, mean_gap))
        mc_ks, mc_p = _monte_carlo_ks_pvalue(gaps, rng=rng)

        result = {
            'category': label, 'n_gaps': len(gaps), 'cv': cv,
            'ks_statistic': asymptotic_ks, 'asymptotic_p_value': asymptotic_p,
            'monte_carlo_p_value': mc_p,
        }
        results.append(result)

        print(f"\n--- {label} ---")
        print(f"n gaps: {len(gaps)}, CV: {cv:.2f}")
        print(f"KS statistic: {asymptotic_ks:.3f}")
        print(f"Asymptotic p-value: {asymptotic_p:.4f}  (uncorrected for estimating the rate from data)")
        print(f"Monte Carlo-corrected p-value: {mc_p:.4f}  ({N_MONTE_CARLO} simulations)")
        if mc_p < 0.05:
            print(f">>> Reject the homogeneous-Poisson null even after correction. Consistent with "
                  f"temporal clustering (CV={cv:.2f}{'>' if cv>1 else '<'}1).")
        else:
            print(">>> Cannot reject the homogeneous-Poisson null after correction - weaker "
                  "evidence of clustering than the asymptotic test alone suggested.")

        fig, ax = plt.subplots(figsize=(8, 5))
        ax.hist(gaps, bins=min(20, max(5, len(gaps) // 3)), density=True, alpha=0.6,
                color='#1f77b4', label='Observed inter-failure gaps')
        x = np.linspace(0, gaps.max() * 1.1, 200)
        ax.plot(x, stats.expon.pdf(x, scale=mean_gap), color='#d62728', linewidth=2,
                label=f'Exponential null (mean={mean_gap:.1f} days)')
        ax.set_xlabel('Days between failures')
        ax.set_ylabel('Density')
        ax.set_title(f'{label}: observed gaps vs. random (Poisson) expectation (n={len(gaps)})')
        ax.legend()
        fig.tight_layout()
        fig.savefig(os.path.join(out_dir, f'clustering_{col.replace("_hrs","")}.png'), dpi=130)
        plt.close(fig)

    pd.DataFrame(results).to_csv(os.path.join(out_dir, 'outage_clustering_summary.csv'), index=False)
    print(f"\nSaved: outage_clustering_summary.csv, clustering_bd.png, clustering_gd.png")
    return pd.DataFrame(results)


# ============================================================================
# 7. Cause of loss vs. season: chi-square, merged-category, and permutation test
# ============================================================================

def _chi_square_with_effect_size(table):
    chi2, p, dof, expected = stats.chi2_contingency(table)
    expected_df = pd.DataFrame(expected, index=table.index, columns=table.columns)
    pct_ok = (expected_df >= 5).values.mean()
    n = table.values.sum()
    cramers_v = (chi2 / (n * (min(table.shape) - 1))) ** 0.5
    return {'chi2': chi2, 'p_value': p, 'dof': dof, 'pct_expected_cells_ge_5': pct_ok,
            'cramers_v': cramers_v, 'n': n}


def _permutation_chi_square_pvalue(d, category_col, n_perm=N_MONTE_CARLO, rng=None):
    """A permutation-test alternative to the chi-square p-value that makes no
    distributional assumption about expected cell counts at all: repeatedly
    shuffle the season label among the remarked days (holding cause category
    fixed), recompute the chi-square statistic each time, and report what
    fraction of shuffles produced a statistic at least as extreme as observed.
    Valid under the null of "no association" because season is exchangeable
    across days under that null."""
    rng = rng or np.random.default_rng(RNG_SEED)
    observed_table = pd.crosstab(d['season'], d[category_col]).reindex(SEASON_ORDER).fillna(0)
    observed_chi2 = stats.chi2_contingency(observed_table)[0]

    seasons = d['season'].values.copy()
    causes = d[category_col].values
    sim_chi2 = np.empty(n_perm)
    for i in range(n_perm):
        shuffled = rng.permutation(seasons)
        tmp = pd.crosstab(pd.Series(shuffled), pd.Series(causes)).reindex(SEASON_ORDER).fillna(0)
        sim_chi2[i] = stats.chi2_contingency(tmp)[0]
    b = int((sim_chi2 >= observed_chi2).sum())
    return (b + 1) / (n_perm + 1)


def analysis_cause_season(df, out_dir):
    print("\n" + "=" * 70)
    print("7. CAUSE OF LOSS VS. SEASON")
    print("=" * 70)

    d = df.copy()
    d['cause_category'] = d['remarks'].apply(classify_remark)
    d['season'] = d['month'].map(SEASON_MAP)
    d = d[d['cause_category'] != 'No remark']
    rng = np.random.default_rng(RNG_SEED)

    # --- Original 6-category test ---
    table = pd.crosstab(d['season'], d['cause_category']).reindex(SEASON_ORDER).fillna(0).astype(int)
    print("--- 6-category contingency table ---")
    print(table.to_string())
    result = _chi_square_with_effect_size(table)
    print(f"\nchi2={result['chi2']:.2f}, dof={result['dof']}, p={result['p_value']:.4f}, "
          f"Cramer's V={result['cramers_v']:.3f}, "
          f"expected cells >=5: {result['pct_expected_cells_ge_5']*100:.1f}%")
    if result['pct_expected_cells_ge_5'] < 0.8:
        print(">>> CAUTION: below the 80% adequacy threshold - see merged-category and "
              "permutation-test checks below.")

    # --- Merged 5-group robustness check ---
    d['merged_cause'] = d['cause_category'].map(CAUSE_MERGE_MAP)
    merged_table = pd.crosstab(d['season'], d['merged_cause']).reindex(SEASON_ORDER).fillna(0).astype(int)
    print("\n--- Merged 5-category contingency table ---")
    print(merged_table.to_string())
    merged_result = _chi_square_with_effect_size(merged_table)
    print(f"\nchi2={merged_result['chi2']:.2f}, dof={merged_result['dof']}, "
          f"p={merged_result['p_value']:.6f}, Cramer's V={merged_result['cramers_v']:.3f}, "
          f"expected cells >=5: {merged_result['pct_expected_cells_ge_5']*100:.1f}%")
    if merged_result['pct_expected_cells_ge_5'] >= 0.8:
        print(f">>> Merging raises expected-cell adequacy to "
              f"{merged_result['pct_expected_cells_ge_5']*100:.1f}% and the association survives "
              f"with a similar effect size ({result['cramers_v']:.3f} -> {merged_result['cramers_v']:.3f}).")

    # --- Permutation-test p-value (assumption-free alternative) on the merged table ---
    perm_p = _permutation_chi_square_pvalue(d, 'merged_cause', rng=rng)
    print(f"\nPermutation-test p-value on the merged table ({N_MONTE_CARLO} shuffles, "
          f"no distributional assumptions): {perm_p:.4f}")
    if perm_p < 0.05:
        print(">>> Confirms the association independent of the chi-square approximation's assumptions.")
    else:
        print(">>> Does not confirm the chi-square result - treat the association as unresolved.")

    fig, ax = plt.subplots(figsize=(10, 5))
    table.plot(kind='bar', stacked=True, ax=ax, colormap='tab10')
    ax.set_ylabel('Days with this cause classification')
    ax.set_title('Cause category by season (6 categories)')
    ax.legend(bbox_to_anchor=(1.02, 1), loc='upper left', fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, 'cause_season_breakdown.png'), dpi=130)
    plt.close(fig)

    table.to_csv(os.path.join(out_dir, 'cause_season_contingency.csv'))
    merged_table.to_csv(os.path.join(out_dir, 'cause_season_contingency_merged.csv'))
    combined = pd.DataFrame([
        {**result, 'scheme': '6-category'},
        {**merged_result, 'scheme': 'merged-5-category'},
        {'scheme': 'permutation-test (merged)', 'p_value': perm_p},
    ])
    combined.to_csv(os.path.join(out_dir, 'cause_season_test_result.csv'), index=False)
    print(f"\nSaved: cause_season_contingency.csv, cause_season_contingency_merged.csv, "
          f"cause_season_test_result.csv, cause_season_breakdown.png")
    return combined


# ============================================================================
# 8. Seasonal significance (Kruskal-Wallis)
# ============================================================================

def analysis_seasonal_significance(df, out_dir):
    print("\n" + "=" * 70)
    print("8. SEASONAL SIGNIFICANCE (Kruskal-Wallis)")
    print("=" * 70)
    print("Note: this tests whether the DISTRIBUTIONS of daily generation differ")
    print("systematically among calendar months. Daily observations from the same site")
    print("across multiple years are not necessarily fully independent (weather and")
    print("operating conditions can be serially correlated) - see Limitations.")

    results = []
    for col, label, fname in [
        ('total_kwh', 'Daily generation (kWh)', 'kruskal_generation_boxplot.png'),
        ('lull_hrs', 'Daily lull (low-wind) hours', 'kruskal_lull_boxplot.png'),
    ]:
        groups = [g[col].values for _, g in df.groupby('month')]
        h_stat, p_value = stats.kruskal(*groups)
        n, k = len(df), len(groups)
        epsilon_sq = (h_stat - k + 1) / (n - k) if n > k else np.nan
        results.append({'variable': col, 'h_stat': h_stat, 'p_value': p_value,
                         'epsilon_sq': epsilon_sq, 'n': n, 'n_groups': k})
        print(f"\n{label}: H={h_stat:.1f}, p={p_value:.2e}, epsilon^2={epsilon_sq:.3f}, n={n}")
        if p_value < 0.05:
            size = 'small' if epsilon_sq < 0.06 else 'moderate' if epsilon_sq < 0.14 else 'large'
            print(f">>> Statistically significant (p<0.05), {size} effect size.")

        fig, ax = plt.subplots(figsize=(12, 5))
        data_by_month = [df[df['month'] == m][col].values for m in range(1, 13)]
        ax.boxplot(data_by_month, tick_labels=MONTH_NAMES[1:], showfliers=False)
        ax.set_ylabel(label)
        ax.set_title(f'{label} by calendar month')
        fig.tight_layout()
        fig.savefig(os.path.join(out_dir, fname), dpi=130)
        plt.close(fig)

    pd.DataFrame(results).to_csv(os.path.join(out_dir, 'seasonal_significance_test.csv'), index=False)
    print(f"\nSaved: seasonal_significance_test.csv, kruskal_generation_boxplot.png, "
          f"kruskal_lull_boxplot.png")
    return pd.DataFrame(results)


# ============================================================================
# 9. Economic sensitivity, using MONTH-SPECIFIC generation rates
# ============================================================================

def analysis_economic_sensitivity(df, out_dir, tariffs=None):
    tariffs = tariffs or DEFAULT_TARIFFS
    print("\n" + "=" * 70)
    print("9. ECONOMIC SENSITIVITY (month-specific generation rates)")
    print("=" * 70)
    print("Improvement over a single site-wide average: lost hours are now valued using")
    print("THAT MONTH's own average kWh-per-run-hour, not one blanket rate for the whole")
    print("dataset. This still assumes the lost hours would have generated at that month's")
    print("average rate; it is a generation-opportunity-loss estimate, not an audited or")
    print("guaranteed-recoverable figure - actual generation during any specific downed")
    print("hour depends on the wind conditions at that exact time, which this dataset")
    print("does not record. During storms, downed hours may not have generated much even")
    print("if available, making this proxy optimistic in that case and conservative in others.")

    d = df.copy()
    d['cause_category'] = d['remarks'].apply(classify_remark)
    d['lost_hrs'] = d['gd_hrs'] + d['bd_hrs']

    # Month-specific proxy rate (kWh per run-hour), falling back to the overall average
    # for any month with too few run-hours to estimate a rate reliably.
    monthly_rate = d.groupby('month').apply(
        lambda g: g['total_kwh'].sum() / g['run_hrs'].sum() if g['run_hrs'].sum() > 10 else np.nan
    )
    overall_rate = d['total_kwh'].sum() / d['run_hrs'].sum()
    monthly_rate = monthly_rate.fillna(overall_rate)
    print(f"\nOverall average rate: {overall_rate:.2f} kWh/run-hr")
    print("Month-specific rates used:")
    for m in range(1, 13):
        print(f"  {MONTH_NAMES[m]}: {monthly_rate.get(m, overall_rate):.2f} kWh/run-hr")

    d['proxy_rate'] = d['month'].map(monthly_rate)
    d['estimated_lost_kwh'] = d['lost_hrs'] * d['proxy_rate']

    years_in_dataset = (df['date'].max() - df['date'].min()).days / 365.25

    # Relabel the undocumented category so "documentation status" isn't confused
    # with "cause" (a distinction raised by review feedback).
    d['cause_label'] = d['cause_category'].replace({'No remark': 'Unattributed (no documented cause)'})

    g = d.groupby('cause_label').agg(
        total_lost_hrs=('lost_hrs', 'sum'), days=('date', 'count'),
        estimated_lost_kwh_total=('estimated_lost_kwh', 'sum'),
    ).reset_index()
    g['estimated_lost_kwh_per_year'] = g['estimated_lost_kwh_total'] / years_in_dataset
    g = g.sort_values('estimated_lost_kwh_per_year', ascending=False)

    for tariff in tariffs:
        g[f'revenue_per_year_at_Rs{tariff}'] = g['estimated_lost_kwh_per_year'] * tariff

    print("\n" + g.to_string(index=False))
    print(f"\n>>> Read this as a PRIORITIZATION tool across cause categories, not an audited "
          f"loss figure. 'Estimated lost kWh' means 'generation opportunity loss at that "
          f"month's average rate', not physically or economically guaranteed recoverable energy.")

    top_cause = g.iloc[0]['cause_label']
    if 'Unattributed' in top_cause:
        print(f"\n>>> NOTE: the largest category is unattributed downtime - a documentation status, "
              f"not a cause. The highest-leverage intervention this points to may be better "
              f"downtime logging, which would let a future run of this analysis actually "
              f"attribute those hours to a real cause.")

    g.to_csv(os.path.join(out_dir, 'economic_sensitivity_by_cause.csv'), index=False)
    print(f"\nSaved: economic_sensitivity_by_cause.csv")
    return g


# ============================================================================
# Orchestration
# ============================================================================

ANALYSES = [
    ('loss_decomposition', 'Where is energy lost, and why?', analysis_loss_decomposition),
    ('seasonal', 'Generation and lull hours by calendar month', analysis_seasonal),
    ('export_balance', 'Generation vs. metered export', analysis_export_balance),
    ('temporal_trend', 'Year-over-year generation intensity', analysis_temporal_trend),
    ('reliability', 'MTBF/MTTR and Weibull fit, with bootstrap CIs', analysis_reliability),
    ('clustering', 'Do outages cluster? (Monte Carlo-corrected KS test)', analysis_clustering),
    ('cause_season', 'Is cause of loss associated with season?', analysis_cause_season),
    ('seasonal_significance', 'Kruskal-Wallis test of the seasonal pattern', analysis_seasonal_significance),
    ('economic_sensitivity', 'Revenue sensitivity by cause (month-specific rates)', analysis_economic_sensitivity),
]


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog='wind_turbine_loss_analysis.py',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        'xls_path', nargs='?', default=None,
        help='Path to the daily reading .xls file. Defaults to '
             'data/turbine_daily_log.xls, in a data/ subfolder next to this script.'
    )
    parser.add_argument(
        '--output-dir', default=None,
        help=f'Where to write .csv/.png outputs. Default: {DEFAULT_OUTPUT_DIR}'
    )
    parser.add_argument(
        '--tariff', type=float, nargs='+', default=None, metavar='RS_PER_KWH',
        help='Override the assumed tariff sweep for the economic sensitivity analysis '
             f'(default: {DEFAULT_TARIFFS} Rs/kWh). Example: --tariff 4.5 5.5'
    )
    parser.add_argument(
        '--only', nargs='+', default=None, metavar='ANALYSIS',
        choices=[name for name, _, _ in ANALYSES],
        help='Run only these analyses instead of all nine, in the order given below. '
             'Useful while iterating on one section.'
    )
    parser.add_argument(
        '--list', action='store_true',
        help='List the nine analyses in run order, with a one-line description, and exit.'
    )
    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.list:
        print("Analyses, in run order (pass one or more names to --only to run a subset):\n")
        for name, desc, _ in ANALYSES:
            print(f"  {name:24s} {desc}")
        return

    out_dir = args.output_dir or DEFAULT_OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)

    print(f"Loading daily log from: {args.xls_path or DEFAULT_DATA_PATH}")
    df = load_daily_log(args.xls_path)
    print(f"Loaded {len(df)} daily rows, {df['date'].min().date()} to {df['date'].max().date()}")

    to_run = args.only or [name for name, _, _ in ANALYSES]
    for name, desc, fn in ANALYSES:
        if name not in to_run:
            continue
        if name == 'economic_sensitivity':
            fn(df, out_dir, tariffs=args.tariff)
        else:
            fn(df, out_dir)

    print("\n" + "=" * 70)
    print(f"All requested analyses complete. Outputs saved in: {out_dir}")
    print("=" * 70)


if __name__ == '__main__':
    warnings.filterwarnings('ignore', category=RuntimeWarning)
    main()
