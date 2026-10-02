"""Dependency-free charts and a static, escaped local history report."""

from decimal import Decimal
from html import escape
from pathlib import Path

from .domain import utc


def chart_svg(rows, *, title="Quote history"):
    rows = sorted(rows, key=lambda row: row["observed_at"])
    if not rows:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="960" height="320"><text x="30" y="60">No observations yet</text></svg>'
    values = [float(Decimal(row["rate"])) for row in rows]
    times = [utc(row["observed_at"]).timestamp() for row in rows]
    low, high = min(values), max(values)
    margin = (high - low) * .15 or max(abs(low) * .01, .01)
    low, high = low - margin, high + margin
    x = lambda i: 80 + (times[i] - times[0]) / (times[-1] - times[0] or 1) * 810
    y = lambda value: 250 - (value - low) / (high - low) * 175
    points = " ".join(f"{x(i):.2f},{y(value):.2f}" for i, value in enumerate(values))
    grid = "".join(f'<line x1="80" y1="{y(low + (high-low)*i/4):.1f}" x2="890" y2="{y(low + (high-low)*i/4):.1f}" stroke="#dbe5ed"/><text x="68" y="{y(low + (high-low)*i/4)+4:.1f}" text-anchor="end" font-size="12">{low + (high-low)*i/4:.3f}</text>' for i in range(5))
    dots = "".join(f'<circle cx="{x(i):.2f}" cy="{y(value):.2f}" r="4" fill="#007f86"><title>{escape(row["observed_at"])}: {escape(row["rate"])}</title></circle>' for i, (row, value) in enumerate(zip(rows, values)))
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="960" height="320" viewBox="0 0 960 320" role="img" aria-label="{escape(title, quote=True)}"><rect width="960" height="320" rx="16" fill="#f6f9fc"/><g font-family="Arial,sans-serif" fill="#183247"><text x="32" y="38" font-size="21" font-weight="bold">{escape(title)}</text>{grid}<polyline points="{points}" fill="none" stroke="#007f86" stroke-width="3"/>{dots}<text x="80" y="282" font-size="12">{escape(rows[0]["observed_at"][:10])}</text><text x="890" y="282" font-size="12" text-anchor="end">{escape(rows[-1]["observed_at"][:10])}</text><text x="480" y="306" text-anchor="middle" font-size="12">Observation time (UTC); hover over points for exact values</text></g></svg>'''


def html_report(store):
    rows = store.quotes(limit=1000)
    groups = {}
    for row in rows:
        groups.setdefault((row["base"], row["counter"], row["source"]), []).append(row)
    charts = "".join(chart_svg(group, title=f"{base}/{counter} · {source}") for (base, counter, source), group in groups.items())
    counts = store.counts()
    cards = "".join(f'<div class="metric"><strong>{value}</strong><span>{escape(key.replace("_", " "))}</span></div>' for key, value in counts.items())
    table = "".join(f'<tr><td>{escape(row["observed_at"])}</td><td>{escape(row["base"] + "/" + row["counter"])}</td><td>{escape(row["rate"])}</td><td>{escape(row["source"])}</td></tr>' for row in rows)
    alerts = "".join(f'<tr><td>{escape(item["event"]["rule"]["name"])}</td><td>{escape(item["event"]["quote"]["rate"])}</td><td>{"recorded locally" if item["delivered_at"] else "pending"}</td><td>{item["attempts"]}</td></tr>' for item in store.alerts())
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Currency Rate Prompter · History</title><style>body{{font:16px/1.6 system-ui,sans-serif;margin:0;background:#eef3f7;color:#183247}}main{{max-width:1100px;margin:auto;padding:36px 20px}}h1{{font-size:38px;line-height:1.15;margin-bottom:12px}}h2{{margin-top:38px}}.eyebrow{{color:#007f86;font-weight:700;letter-spacing:.08em}}.metrics{{display:flex;gap:12px;flex-wrap:wrap;margin:28px 0}}.metric{{background:white;padding:18px 24px;border-radius:12px;flex:1}}.metric strong{{display:block;font-size:30px}}.metric span{{color:#526879}}svg{{width:100%;height:auto;margin:12px 0}}.scroll{{overflow:auto;background:white;border-radius:12px}}table{{border-collapse:collapse;width:100%;white-space:nowrap}}td,th{{padding:12px 18px;border-bottom:1px solid #e3eaf0;text-align:left}}.note{{color:#526879}}footer{{margin-top:32px;font-size:14px}}</style><main><p class="eyebrow">CURRENCY RATE PROMPTER</p><h1>A record of the rate.<br>A reason for each alert.</h1><p class="note">Local snapshot · latest 1,000 observations and alerts · amounts are counter currency per 1 base currency.</p>{cards and '<div class="metrics">' + cards + '</div>'}<h2>History</h2>{charts or '<p>No observations yet.</p>'}<h2>Alert journal</h2><p class="note">The included notifier records locally. No email, desktop message or trade is sent.</p><div class="scroll"><table><thead><tr><th>Rule</th><th>Rate</th><th>Status</th><th>Delivery attempts</th></tr></thead><tbody>{alerts}</tbody></table></div><h2>Observations</h2><div class="scroll"><table><thead><tr><th>Observed at (UTC)</th><th>Pair</th><th>Rate</th><th>Source</th></tr></thead><tbody>{table}</tbody></table></div><footer>Reference-rate monitoring, not a forecast. Synthetic fixture data is labelled <code>synthetic-demo</code>. Provider dates are stored at 00:00 UTC as a date convention, not an intraday measurement.</footer></main></html>'''


def write_report(store, path):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html_report(store), encoding="utf-8")
