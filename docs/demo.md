# Repeatable offline demo

This walkthrough follows the original THB-to-SGD use case: inspect a rate, estimate a conversion, then record a target alert. All prices and dates in this demo are synthetic. It uses the same application, with a demo-only service wrapper that blocks reference-rate, reference-history and currency-catalogue downloads.

## Start with one command

Prerequisite: Python 3.10 or later. Extract the source bundle into a writable folder, open a terminal in its root, and run:

```console
# currency-rate-prompter/scripts/demo_app.py
python scripts/demo_app.py
```

No installation, API key, internet connection, npm process or Docker daemon is needed. On Windows, `Run Offline Demo.cmd` in the source folder runs the same command.

The command creates a fresh `demo-sessions/<unique-session>/` directory and chooses a free loopback port. Open the printed address in your browser; the launcher does not open a browser automatically. Example output, with the variable port and path replaced by placeholders:

```text
# Terminal output from scripts/demo_app.py
OFFLINE DEMO ready: http://127.0.0.1:<port>/
Session: <source-folder>/demo-sessions/<unique-session>
Select Demo and SGD / THB. Synthetic rate: 1 SGD = 26.0364 THB.
Fresh session: one example target; first Refresh creates one local alert.
Reference downloads are disabled. No browser opens automatically.
Keep this terminal open; Ctrl+C stops the demo. See docs/demo.md.
```

Before printing Ready, the launcher requests its own health, HTML, JavaScript and CSS routes and checks their status and content type. These checks do not consume the first example alert. Keep the terminal open; press Ctrl+C to stop.

## A three-minute walkthrough

Select **Demo**, then **SGD / THB**, even if your browser remembers another selection. Leave auto-refresh **Off** for the steps below.

| Action | Expected result in a fresh session |
| --- | --- |
| Inspect the selected quote | 1 SGD = **26.0364 THB**, observed **30 September 2026**; labelled synthetic |
| Choose **90D**, then expand **View history as a table** | **90** observations; period low **25.74**, high **26.26** |
| In the converter, select THB to SGD, enter **1000** and fee **2%** | Exact API result **37.63961224 SGD**; the interface formats the display |
| Select SGD to THB, enter **100** and fee **2%** | Exact API result **2551.5672 THB** |
| Inspect **Your targets** | One enabled sample target: **SGD buy target (demo)**, at or below **26.0364**, 24-hour cooldown |
| Click **Refresh rates** once | One local journal entry; equality qualifies because the threshold is inclusive |
| Click **Refresh rates** again | The journal still contains **one** entry: the same rule/observation was already evaluated |
| Click **Export CSV** | **90 data rows** plus a header, with source **synthetic-demo** |
| Add **GBP / JPY** to the watchlist | Four saved pairs; selecting it shows a separate synthetic history |
| Select **Reference**, then **Refresh rates** | Clear offline-demo error; no provider is contacted and no reference quote appears |
| Return to **Demo** and **SGD / THB** | Existing target and journal remain available |

The chart generates 90 synthetic points without storing them all as quote rows. The first refresh stores only the latest example observation. The original CLI replay is a different fixture containing 48 observations; do not mix its counts with this browser walkthrough.

The fixed demo service clock is **3 October 2026 at 12:00 UTC**. Alert receipt times therefore remain predictable even when you run the demonstration later. Rule IDs and the per-server request token are deliberately random. The demo does not send email, desktop notifications or trades.

## Repeat or reopen

Running the initial command again creates an independent session without deleting the previous one. To choose a session location:

```console
# currency-rate-prompter/scripts/demo_app.py
python scripts/demo_app.py --output "demo-sessions/my saved demo"
```

That directory must not already exist, even if empty. This makes accidental replacement explicit. To reopen that session after stopping its server:

```console
# currency-rate-prompter/scripts/demo_app.py
python scripts/demo_app.py --resume "demo-sessions/my saved demo"
```

The saved watchlist, targets and journal return. The original walkthrough's first alert may already exist. Only reopen sessions created by this launcher. An operating-system lock allows one server per session; it releases even if the process is forcefully stopped. A session marker records readiness; it is not a background-process monitor. If a process is forcefully stopped, its marker can still say Ready; rerunning with `--resume` starts a new server and prints its current address.

Both options resolve relative to the terminal's working directory. Default sessions resolve relative to the source folder. There is no reset/delete command. Normal application data in `app-data/tracker.sqlite` is not used.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Python is missing or too old | Install Python 3.10+; check `python --version` or use `py -3` on Windows |
| Existing output directory | Choose another `--output`, or use `--resume` for a valid saved demo |
| Address is occupied | Omit `--port`, or use `--port 0`; a free port is chosen automatically |
| Browser shows an empty Reference chart | Select **Demo** and **SGD / THB** |
| Reference/history/catalogue refresh gives an error | Expected in this offline launcher; use the normal application for explicit provider access |
| Browser loses its connection | Restart the launcher and open the newly printed URL |
| Storage cannot be written | Extract the source into a writable folder; avoid launching inside a ZIP archive |

## Why these small mechanisms exist

Exclusive directory creation prevents a new run from replacing earlier data. The session lock belongs to the operating system, so a competing process is refused and a crash releases the lock automatically. Writing the complete session marker to a temporary file before replacing the old marker lets readers see a complete JSON document. The fixed clock makes example timestamps reproducible; the per-server token remains fresh to protect state-changing requests. The offline wrapper changes only this launcher instance, so the measured application code and normal provider workflow stay intact.

## Recorded verification

The recorded local run on Windows with Python 3.12.14 collected 142 tests: 141 passed and one platform-specific symlink test was skipped. The nine demo tests are included in that suite and also passed separately from an extracted source bundle with site packages disabled. Ruff lint and formatting checks passed. This recorded run checks the service and launcher; it does not establish a hosted CI result or rendered-browser behaviour.

The focused suite exercises the real loopback API and standalone child launcher, including source-only execution with Python site packages disabled, paths containing spaces, saved-session recovery, invalid input, provider blocking and overwrite refusal:

```console
# currency-rate-prompter/tests/test_demo_launcher.py
python -m unittest discover -s tests -p test_demo_launcher.py -v
```

HTTP/service checks and simulated DOM tests have a different scope from a rendered-browser walkthrough. The [testing notes](testing.md) separate those forms of evidence. When checking the interface, keep the synthetic label visible and verify the 90-point chart, conversion output, one journal entry after repeated refresh, CSV content and saved watchlist state.
