# Dashboard history measurement

**Status: user-run baseline completed and verified on 3 October 2026.** The runner is [scripts/benchmark_dashboard.py](../scripts/benchmark_dashboard.py). Raw worker timings and reproducible settings are retained in [the portable result evidence](evidence/dashboard-baseline-results.json).

## Question and boundary

How does warm, repeated preparation of one reference-mode dashboard change as the stored quote history grows?

[AppService.dashboard](../src/currency_prompter/app_service.py) reads the selected pair's stored observations, prepares its visible history and statistics, and returns a Python result. The timed call is `dashboard("SGD", "THB", "reference", 90)`. It includes the SQLite connections opened and closed within each call. It does not include the separate watchlist endpoint or preparation of every watchlist card.

The benchmark varies stored quote history to investigate one part of this local tracker. The fixture deliberately holds other workloads fixed. It does not represent a measured distribution of real users or exchange-rate activity.

## Fixed workload

Each fixture contains 20 SGD/counter-currency pairs, matching the application's maximum watchlist size. SGD/THB is the selected pair in every case.

| Daily quotes per pair | Pairs | Total quote rows | Selected chart window |
| ---: | ---: | ---: | --- |
| 100 | 20 | 2,000 | 90 days |
| 1,000 | 20 | 20,000 | 90 days |
| 10,000 | 20 | 200,000 | 90 days |

The fixture uses seed **1729** and a fixed application clock of **2026-10-03 at 12:00:00 UTC**. The newest 90 daily observations are identical across sizes; larger cases add older history. Every case has **zero rules, outbox records and notifications**, so differences concern quote-history growth.

All observations are synthetic. Their database source field is `frankfurter` solely to exercise the application's reference-data branch. The label does not mean that these fixtures contain market data or observations downloaded from that provider. Fixture creation and measurement require no provider request.

## Timing and repetition

The default protocol launches **five fresh worker processes per case**. Cases are interleaved in a seeded order rather than running all repetitions of one size together.

Each worker initializes the application outside timing, performs one untimed preflight correctness call and **five untimed warm-up calls**, then records **10 timed batches of 10 calls**. A batch measures elapsed `time.perf_counter_ns()` around its 10 calls. Its elapsed time divided by 10 is a batch estimate of mean time per call.

Here, *warm* refers to repeated execution in the worker and warmed interpreter/operating-system caches. The application does not retain a database connection between timed calls. This is not a cold-start or cold-disk experiment, and fresh worker processes do not imply a cold operating-system cache. Preflight fixture hashing and count checks also read the database before measurement and can warm filesystem caches.

Excluded from timing:

- Fixture construction and hashing.
- Process startup, imports and `AppService` initialization.
- Warm-ups and independent correctness checks.
- Evidence writing and JSON serialization of the returned result.
- HTTP handling, provider networking, browser JavaScript and UI rendering.

Python's performance counter is intended for measuring short elapsed durations, and `perf_counter_ns()` returns integer nanoseconds. The unit of the result does not establish nanosecond measurement accuracy. See [Python's performance-counter documentation](https://docs.python.org/3/library/time.html#time.perf_counter_ns).

## Correctness and reporting

Outside timed batches, independently check the selected pair, latest observation, all 90 visible points, minimum, maximum and change from the previous observation. Check the fixture/data and source-code hashes outside timing too. A successful timing run must retain this correctness evidence alongside its measurements; plausible timing numbers alone are insufficient.

First derive one mean time per call for each worker from its raw batches. For each case, summarize the **five worker means** using the arithmetic mean, median, sample standard deviation and sample variance. Standard deviation and variance use **ddof = 1**, with denominator four. The five worker means are the replication unit; the individual calls are not treated as independent workers.

Retain every raw batch and every worker result from a completed run. A failed or interrupted worker makes the run incomplete: already completed workers remain saved, but partial batches still in the interrupted worker's memory may be lost. The runner does not add disk writes between timed batches. Do not remove slow batches or report only the fastest repetition. The measurements do not establish per-request tail latency, and this protocol has no before/after implementation comparison from which to claim a speedup.

## Recorded baseline results

All 15 worker receipts, 150 timed batches and 1,500 calls accounted for. Each case has five worker means. Recomputed means, medians, sample standard deviations and sample variances exactly match the saved summary. All three database hashes, integrity checks and retained source hashes passed; their common newest 90 observations match. The raw run was preserved unchanged during review.

| Quotes per pair | Total quote rows | Mean ms/call | Median ms/call | Sample SD ms | Sample variance ms² |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 100 | 2,000 | 36.9315 | 38.2911 | 4.2637 | 18.1790 |
| 1,000 | 20,000 | 46.8298 | 41.5502 | 10.3331 | 106.7722 |
| 10,000 | 200,000 | 175.0245 | 176.9703 | 12.7437 | 162.4012 |

The sample SD and variance describe variation across process means, using ddof = 1. The individual calls are not independent replication units. Units are milliseconds per call derived from batches; these are not request-tail measurements.

**User-reported operating conditions:** performance power mode, plugged in, no substantial background activity. These were supplied after the run and are retained as attributed context; the original configuration's “not supplied” note was preserved. The recorded runtime was Windows 11, Python 3.12.14, SQLite 3.53.1, with garbage collection enabled.

Increasing total stored quote rows from 2,000 to 200,000 (100×) increased the observed mean for the selected-pair path from 36.9315 ms to 175.0245 ms (about 4.74×) in this run. The 20,000-row case had a median of 41.5502 ms and a mean of 46.8298 ms, with a slowest worker mean of 64.9407 ms; all workers remain included. The cause of that variation was not established.

the current path reads up to 10,000 selected-pair observations and filters them for the displayed window. That gives a concrete area to investigate if optimization becomes a later goal. No profiling or before/after optimization comparison was performed here. These three sizes do not establish an asymptotic complexity bound or full-page responsiveness.

Run identity: `dashboard-baseline-20261003T082500718919Z-d662c8`. The original fixtures, raw receipts and source identities remain in the ignored local `benchmark-results` directory; the linked portable JSON contains exact retained raw batches and provenance without local user paths.

## Reproduce from the repository root

Use Python 3.10+ from the root of this repository. The runner loads the bundled application source and does not require a provider connection. It creates synthetic SQLite fixtures in a new output directory; keep those generated databases out of the repository.

First, exercise a small fixture and the multiprocess validation path. This check is **untimed** and produces **no benchmark statistics**:

```console
python scripts/benchmark_dashboard.py --check
```

For the full baseline protocol, provide the actual operating conditions at the time of the run. Replace the example note with your own circumstances:

```console
python scripts/benchmark_dashboard.py --notes "Power: plugged in; background apps: describe here; other conditions: describe here"
```

Record whether the machine was plugged in or on battery, meaningful background activity, and any known interruptions. These notes are user-reported context, not automatically verified claims about system idleness. The [pyperf guidance on system noise](https://pyperf.readthedocs.io/en/latest/system.html) explains why power state, CPU behaviour and other activity can affect measurements. This runner does not claim to apply pyperf's system tuning.

Each run writes a new timestamped folder under `benchmark-results`. Keep the entire folder: raw worker batches, correctness evidence, fixture/source identities, execution order, environment details, notes and summaries belong together. Review completion, correctness and provenance before interpreting the timing statistics. The untimed check is useful for validating the runner, but supplies no measurement.

## Interpretation limits

reference history preparation is capped at the selected pair's latest 10,000 observations. The largest case reaches that cap and stores 200,000 quote rows across all pairs. It does not characterize selected-pair histories beyond that cap.

Results describe this implementation, fixture, machine and recorded operating conditions. With rules and outbox empty, they cannot establish alert-history scalability. Sequential workers do not establish concurrent-user throughput. Local synthetic reads do not establish provider latency, live-feed behavior, bank-rate quality, conversion savings or browser responsiveness.
