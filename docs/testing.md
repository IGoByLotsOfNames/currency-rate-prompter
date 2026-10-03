# Testing and automation

## Reproduce locally

Python 3.10+ is required for the application. Node.js 24.15+ within the 24.x line is required only for DOM tests. All provider responses in the automated tests are injected fixtures; no market data or external messaging is needed.

```powershell
# currency-rate-prompter/ (repository root)
python -m pip install . -r requirements-dev.txt
python -m ruff check .
python -m ruff format --check .
python -m coverage erase
python -m coverage run -m unittest discover -s tests -v
python -m coverage combine
python -m coverage report -m
python -m coverage html
npm ci --ignore-scripts --no-audit --no-fund
npm run check
npm run test:coverage
```

Run coverage collections sequentially. To add the standalone entry point to a collection, run the following before `coverage combine`, choosing a fresh output directory:

```powershell
# currency-rate-prompter/ (repository root)
python -m coverage run -m currency_prompter demo --output output/coverage-demo
```

The application itself has no Node service or third-party Python runtime dependency. Ruff, coverage.py and jsdom are development-only tools. Python pins are in requirements-dev.txt and the JavaScript dependency graph is fixed by package-lock.json.

## Publication-copy checks, 3 October 2026

The integrated public copy reproduced **141 passing Python tests and one Windows symlink skip**, **19 passing DOM tests**, and **nine passing demo tests from a newly built, extracted source distribution with `python -S`**. These repeated demo tests are a packaging check, not additional unique tests. All 19 application, static and data files retain the supplied revision hashes.

Fresh coverage combined the full Python suite, subprocesses and the deterministic CLI replay: **93.66% statements, 89.34% branches and 92.63% combined**, with zero source exclusions. JavaScript coverage remained 96.32% lines and 82.84% branches. Lint, formatting, dependency checks, untimed benchmark wiring and wheel/source builds passed. See the [integration receipt](evidence/integration-verification.json).

The source distribution now includes the demo launcher; [the distribution checker](../scripts/check_distribution.py) verifies wheel resources byte for byte, extracts the source bundle and runs its nine demo tests without site packages.

The [manual browser record](evidence/browser-verification.json) separately records a rendered desktop overview, THB-to-SGD calculation with an optional fee, repeated-refresh alert deduplication, CSV download initiation, catalogue search and a watchlist saved across page reload. It does not establish all responsive breakpoints, all keyboard/pointer interactions or cross-browser compatibility.

## Windows fixture correction, 3 October 2026

Hosted Windows tests exposed a temporary-directory alias mismatch in one regression test. The launcher already resolves its session directory; the test now does the same before directly reacquiring its internal lock. A genuine Windows 8.3 alias reproduced the original failure and passed after this fixture-only correction. The nine demo tests and full Python suite (141 passed, one symlink skip) passed locally again, as did lint and formatting.

Application logic, folder-boundary checks and benchmark evidence are unchanged. The original dated receipts remain intact; [the repair record](evidence/ci-repair.json) identifies the exact old/new test hashes and rerun logs. Hosted results remain separate from these local checks.

## Supplied regression results, 3 October 2026

The recorded complete Python suite collected **142 tests: 141 passed and one Windows symlink test was skipped**. The nine offline-demo tests, included in that suite, also passed separately from the extracted source bundle with site packages disabled. Ruff lint and formatting passed. The retained [portable verification receipt](evidence/local-verification.json) identifies the exact log summaries and source hashes.

These results extend the earlier 120 passing tests with twelve benchmark-harness checks and nine demo checks. The benchmark tests use fake timers or untimed checks; unit-test results are not performance measurements. The expanded suite did not have a new coverage collection in this recorded run. The application code remains identical to the measured baseline.

## Earlier coverage collection, 3 October 2026

On Windows with Python 3.12.14 and Node 24.19.0, version 1.2.0 passed 120 Python tests; one native-symlink test was skipped because the required Windows privilege was unavailable (121 collected). Hard-link and ordinary path-preservation tests passed. All 19 mock-API DOM tests passed.

| Measurement | Result |
| --- | ---: |
| Python statements | 1,179 / 1,261 (93.50%) |
| Python branches | 352 / 394 (89.34%) |
| Python combined statement-and-branch coverage | 92.51% |
| app.js line coverage | 96.32% |
| app.js branch coverage | 82.84% |
| app.js function coverage | 90.83% |

The Python figures are from the installed-package test suite; the separately successful CLI demo was outside that coverage collection. No source-line or branch patterns are excluded. The Python floor is 90% combined coverage, chosen below the measured result. Coverage is evidence of execution, not proof of correctness or a performance benchmark.

The tests cover every bundled currency code paired with SGD through offline application operations. They also preserve hashes for all 240 directed pairs among the original sixteen demo currencies, so catalogue expansion does not silently revise the original synthetic histories. This does not establish that every directed pair and historical date is available from the live provider.

## Remaining gaps

- The recorded local automated runs do not establish real browser layout, pointer-to-SVG geometry or file-download behaviour. DOM simulation cannot establish browser rendering, responsive layout or real click-through. Keep any later manual browser evidence separate from these test results.
- Native Windows symlink behavior needs a machine with the appropriate privilege. Hard-link collisions and non-symlink path checks are covered; the skipped case remains visible.
- The original long-running app/serve CLI lifecycle, some malformed HTTP and storage-failure branches, and a few provider-transport fallback paths were not covered by the earlier collection. The separate offline-demo launcher now has subprocess start/resume and interrupted-start tests; this does not close every original CLI lifecycle gap. These gaps are documented, not hidden by exclusions.
- The provider was read separately to verify 165 active codes and a complete SGD-base batch containing 164 non-identity rates. These one-time checks do not guarantee ongoing availability or every cross pair/date. Tests use deterministic transports to avoid treating network availability as an application test.
- The recorded local evidence is Windows/Python 3.12/Node 24 only. The configured Python 3.10-3.13 and Node 24 matrices target Windows and Linux; configured jobs are not evidence of a successful hosted run. Check the repository Actions page for results tied to a specific commit.

## CI contract

The GitHub Actions workflow defines 12 jobs: one quality job, eight Python version/OS combinations, two frontend OS jobs and one extracted-source packaging job. It installs the application, checks Ruff lint/format, runs Python tests with branch/subprocess coverage and a deterministic CLI demo, enforces the 90% combined floor, compiles Python, and runs JS syntax plus DOM tests. Coverage JSON/XML and demo artifacts are retained for each Python matrix entry. It has read-only repository permissions and does not deploy or send notifications.

The retained local receipt is a dated record of local verification. Hosted results, when available, are separate evidence and should be identified by their workflow run and commit.
