# Wind Turbine Operational Data Analysis

A reproducible framework for diagnosing where a wind turbine loses potential
generation, using nothing but the daily operational reading log that most
turbine operators already maintain (no SCADA subsystem data required).

Demonstrated on 1,613 consecutive days (1 April 2022 to 31 August 2026) of
daily logs from a single 250 kW turbine, covering generation, five recorded
time categories (Run, Grid-Down, Breakdown, Maintenance, Lull), and free-text
operational remarks.

## Setup

```
pip install -r requirements.txt
```

Requires Python 3.9+.

## Data

Put your daily reading workbook in a `data/` subfolder next to the script, as
`data/turbine_daily_log.xls`, or pass its path as the first argument to the
script. The expected format is a set of monthly worksheets (sheet names like
`April2022`, `April 2026`, or `JULY2025` are all recognized), each with daily
rows for: day of month, two generation readings, five time-category hour
columns, three EB meter readings, two power-quality fields, two availability
percentage fields, and a free-text remarks field.

## Usage

Run everything:

```
python wind_turbine_loss_analysis.py
```

Run against a specific file:

```
python wind_turbine_loss_analysis.py path/to/your_log.xls
```

Run only a subset of analyses:

```
python wind_turbine_loss_analysis.py --only reliability clustering
```

List the nine analyses without running them:

```
python wind_turbine_loss_analysis.py --list
```

Override the tariff sweep used in the economic sensitivity analysis:

```
python wind_turbine_loss_analysis.py --tariff 4.5 5.5
```

Full option list:

```
python wind_turbine_loss_analysis.py --help
```

All outputs (`.csv` tables and `.png` charts) are written to `output/`,
created automatically on first run. Tested end-to-end in a clean virtual
environment built only from `requirements.txt`.

## What each analysis does

The script runs nine analyses in one logical sequence (see `--list`), each
its own function in `wind_turbine_loss_analysis.py`:

1. **Loss decomposition** - splits each day into Run / Grid-Down / Breakdown
   / Maintenance / Lull hours, classifies the free-text remarks into cause
   categories with a keyword-based rule set (exported in full to
   `output/keyword_lexicon.csv` for auditing), investigates the small
   residual left over after summing the five categories rather than just
   attributing it to rounding, and reports what share of actual lost hours
   are covered by a remark at all (a selection-bias check, not just a
   completeness one).
2. **Seasonal pattern** - generation and lull hours by calendar month.
3. **Export balance** - turbine-side generation against the utility's
   export meter reading.
4. **Temporal trend** - year-over-year generation per run-hour, with a
   significance test rather than an eyeballed trend line.
5. **Reliability (MTBF/MTTR)** - discrete Breakdown and Grid-Down events,
   with a Weibull fit to the gaps between them and bootstrap 95% confidence
   intervals on both the mean time between failures and the Weibull shape
   parameter.
6. **Outage clustering** - tests whether failure timing is consistent with
   a random (Poisson) process, using a Kolmogorov-Smirnov test whose
   p-value is corrected by Monte Carlo simulation (the classic asymptotic
   p-value is not exact when the null distribution's rate parameter is
   estimated from the same sample being tested).
7. **Cause vs. season** - chi-square test of independence between
   classified cause and season, re-run on a merged, coarser category
   scheme to address sparse expected cell counts, and cross-checked with
   an assumption-free permutation test.
8. **Seasonal significance** - Kruskal-Wallis test confirming the seasonal
   pattern in generation and lull hours is statistically real, not just a
   chart impression.
9. **Economic sensitivity** - estimated annual generation-opportunity-loss
   and revenue sensitivity by cause, using **month-specific** average
   generation rates rather than one blanket site-wide average, swept
   across a range of assumed tariffs.

