"""Render the recorded baseline; this script does not run a benchmark."""

import json
import math
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/evidence/dashboard-baseline-results.json"
OUT = ROOT / "docs/assets"


def main():
    record = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    cases = ["rows-100", "rows-1000", "rows-10000"]
    labels = ["2,000", "20,000", "200,000"]
    calls = record["parameters"]["calls_per_batch"]
    points = []
    for case in cases:
        workers = [w for w in record["worker_results"] if w["case_id"] == case]
        values = [statistics.mean(w["raw_batches_ns"]) / calls / 1e6 for w in workers]
        assert len(values) == 5
        assert math.isclose(statistics.mean(values), record["case_statistics_ms"][case]["mean_ms"])
        points.append(sorted(values))

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 11,
            "svg.fonttype": "none",
            "svg.hashsalt": "currency-dashboard-baseline",
        }
    )
    fig, ax = plt.subplots(figsize=(10.8, 6.2), dpi=180)
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#ffffff")
    fig.subplots_adjust(left=0.105, right=0.965, bottom=0.26, top=0.76)
    fig.text(
        0.055,
        0.935,
        "More stored history, more preparation work",
        fontsize=21,
        weight="bold",
        color="#102b3c",
    )
    fig.text(
        0.055,
        0.882,
        "Selected SGD / THB dashboard • recorded service-level baseline",
        fontsize=12,
        color="#536776",
    )
    for x, values in enumerate(points):
        mean = statistics.mean(values)
        sd = statistics.stdev(values)
        ax.scatter(
            [x + j * 0.04 - 0.08 for j in range(5)],
            values,
            s=40,
            color="#91b8c2",
            edgecolors="white",
            linewidths=0.7,
            zorder=3,
        )
        ax.errorbar(
            x + 0.23,
            mean,
            yerr=sd,
            fmt="D",
            color="#006d77",
            capsize=6,
            markersize=6,
            linewidth=1.8,
            zorder=4,
        )
        ax.text(
            x + 0.23,
            mean + sd + 9,
            f"{mean:.2f} ms",
            ha="center",
            fontsize=12,
            weight="bold",
            color="#102b3c",
        )
    ax.set_xticks(range(3), labels)
    ax.set_xlim(-0.4, 2.7)
    ax.set_ylim(0, 223)
    ax.set_xlabel("Total stored synthetic quotes across 20 pairs", labelpad=10)
    ax.set_ylabel("Mean preparation time per call (ms)", labelpad=10)
    ax.grid(axis="y", color="#e4ebef", linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ["top", "right"]:
        ax.spines[side].set_visible(False)
    for side in ["left", "bottom"]:
        ax.spines[side].set_color("#c5d2da")
    ax.tick_params(length=0, pad=8, colors="#425766")
    fig.legend(
        handles=[
            Line2D(
                [],
                [],
                marker="o",
                linestyle="",
                color="#91b8c2",
                label="Each independent worker mean (5 per case)",
            ),
            Line2D(
                [],
                [],
                marker="D",
                linestyle="-",
                color="#006d77",
                label="Mean ± sample SD across workers",
            ),
        ],
        loc="lower left",
        bbox_to_anchor=(0.06, 0.122),
        frameon=False,
        ncol=2,
        fontsize=10,
        handlelength=1.4,
        columnspacing=2,
    )
    fig.text(
        0.055,
        0.087,
        "Each worker: 10 batches × 10 timed calls after warm-up. Same newest 90-point window; empty alert journals.",
        fontsize=9,
        color="#536776",
    )
    fig.text(
        0.055,
        0.055,
        "Includes fresh SQLite reads and data preparation. Excludes HTTP, provider calls and browser rendering.",
        fontsize=9,
        color="#536776",
    )
    fig.text(
        0.055,
        0.023,
        "Windows 11 / Python 3.12.14 • single-machine synthetic measurement • no speedup or page-latency claim",
        fontsize=9,
        color="#536776",
    )
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / "dashboard-benchmark.png", facecolor="white")
    fig.savefig(OUT / "dashboard-benchmark.svg", facecolor="white", metadata={"Date": None})
    plt.close(fig)


if __name__ == "__main__":
    main()
