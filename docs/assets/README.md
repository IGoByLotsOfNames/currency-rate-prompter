# Documentation visuals

These assets show different parts of the project; their counts and timings should not be combined.

| Asset | What it shows | Source |
| --- | --- | --- |
| `dashboard-demo.jpg` | The real local browser interface using the fixed, synthetic SGD/THB demo | Captured during the [desktop browser checks](../evidence/browser-verification.json); no interface or values were fabricated |
| `dashboard-benchmark.png` / `.svg` | Five worker means per history size, with the overall mean and sample standard deviation | [Recorded dashboard baseline](../evidence/dashboard-baseline-results.json); service preparation only, excluding HTTP and browser rendering |
| `overview.png` / `.svg` | The separate 48-observation CLI replay and its repeat-run behavior | Bundled `demo-quotes.json` and [replay evidence](../evidence/replay-results.json) |
| `architecture.png` / `.svg` | The transaction and local journal path, illustrated with the CLI replay counts | `scripts/render_visuals.py`; the README diagram adds the current browser/service boundaries |
| `history.svg` | The earlier CLI report's synthetic rate-history chart | Retained alongside the [generated report](../evidence/demo-report.html) |

## Rebuild the generated figures

From the repository root, install the optional documentation tools and run:

```console
python -m pip install -r requirements-visuals.txt
python docs/assets/plot_dashboard_baseline.py
python scripts/render_visuals.py
```

The first script recomputes worker means from the retained raw batches before plotting. It does not run a new benchmark. The second rebuilds the CLI overview and architecture assets from the committed fixtures. Font availability can affect the older Pillow-rendered images. Matplotlib and Pillow are documentation tools; neither is an application dependency.

The dashboard screenshot is a browser capture, not a generated figure. To replace it, follow the [offline demo walkthrough](../demo.md), keep the synthetic label visible, capture the actual rendered interface, and record the checks performed. Do not treat the screenshot as live exchange-rate data.
