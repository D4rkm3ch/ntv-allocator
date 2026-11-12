"""Read-only status page: http://<nas>:<STATUS_PORT>/ (HTML) and /api/status (JSON).

Shows no tokens or passwords. Served with aiohttp, which discord.py already depends on.
"""
from __future__ import annotations

import base64
import html
import logging
import time
from datetime import datetime

from aiohttp import web

log = logging.getLogger("status")

# The app logo, embedded so the page needs no static files (source: assets/logo.svg)
LOGO_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><defs><clipPath id="ntva-sq"><rect width="512" height="512" rx="104"/></clipPath><clipPath id="ntva-tri"><path d="M30.0 82.4 Q30.0 32.0 77.6 57.2 L338.0 216.8 Q382.8 256.0 338.0 295.2 L77.6 454.8 Q30.0 480.0 30.0 429.6 Z"/></clipPath></defs><rect width="512" height="512" rx="104" fill="#0f1a2b"/><g clip-path="url(#ntva-sq)"><g transform="translate(47.2 -26.2) scale(1.1)"><path d="M172.5 115.4 L209.5 138.0 L909.5 138.0 L872.5 115.4 Z" fill="#ef5350"/><path d="M209.5 138.0 L246.5 160.7 L946.5 160.7 L909.5 138.0 Z" fill="#47b86b"/><path d="M246.5 160.7 L283.4 183.4 L983.4 183.4 L946.5 160.7 Z" fill="#3f86e8"/><path d="M30.0 82.4 Q30.0 32.0 77.6 57.2 L338.0 216.8 Q382.8 256.0 338.0 295.2 L77.6 454.8 Q30.0 480.0 30.0 429.6 Z" fill="#e8c98f"/><path d="M172.5 115.4 L283.4 183.4 L181.4 307.6 L81.8 225.8 Z" fill="#ffffff" clip-path="url(#ntva-tri)"/><path d="M30.0 82.4 Q30.0 32.0 77.6 57.2 L338.0 216.8 Q382.8 256.0 338.0 295.2 L77.6 454.8 Q30.0 480.0 30.0 429.6 Z" fill="none" stroke="#0f1a2b" stroke-width="12" stroke-linejoin="round"/><g stroke="#172437" stroke-width="12.5" stroke-linecap="round"><path d="M131.0 203.0 L98.3 150.7"/><path d="M145.0 203.0 L177.7 150.7"/></g><circle cx="98.3" cy="150.7" r="11.8" fill="#172437"/><circle cx="177.7" cy="150.7" r="11.8" fill="#172437"/><rect x="53.0" y="201.0" width="170.0" height="142.0" rx="26.5" fill="#172437"/><rect x="71.3" y="215.0" width="32.3" height="114.0" rx="5" fill="#ef5350"/><rect x="121.9" y="215.0" width="32.3" height="114.0" rx="5" fill="#47b86b"/><rect x="172.4" y="215.0" width="32.3" height="114.0" rx="5" fill="#3f86e8"/></g></g></svg>'
FAVICON = "data:image/svg+xml;base64," + base64.b64encode(LOGO_SVG.encode()).decode()


def _t(ts: float | None) -> str:
    """24-hour time; the date is added only when it is not today."""
    if not ts:
        return "-"
    d = datetime.fromtimestamp(ts)
    return d.strftime("%H:%M:%S") if d.date() == datetime.now().date() else d.strftime("%Y-%m-%d %H:%M")


def _ago(ts: float | None) -> str:
    if not ts:
        return ""
    s = int(time.time() - ts)
    if s < 0:
        m = (-s) // 60
        return f"in {m} min" if m else "in under a minute"
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60} min ago"
    return f"{s // 86400} d ago"


def snapshot(a) -> dict:
    """Everything the page shows, from the running allocator."""
    services = []
    for name in ("Plex", "NostalgiaTV"):
        c = a.checks.get(name, {})
        services.append({"name": name, "ok": c.get("ok"), "checked": c.get("at"), "reason": c.get("reason", ""),
                         "since": c.get("since")})
    services.append({"name": "Discord", "ok": a.is_ready() and not a.is_closed(), "checked": time.time(),
                     "reason": "" if a.is_ready() else "not connected", "since": None})
    sc = a.last_scan
    services.append({"name": "Scans", "ok": sc.get("ok"), "checked": sc.get("at"), "reason": sc.get("reason", ""),
                     "since": None})
    oks = [x["ok"] for x in services]
    overall = "problem" if False in oks else "ok" if all(o is True for o in oks) else "starting"
    return {
        "overall": overall,
        "services": services,
        "last_scan": sc.get("at"), "last_scan_summary": sc.get("summary", ""),
        "next_run": a.next_run,
        "open_questions": a.state.count_pending(),
        "open_removals": a.state.count_removals_pending(),
        "placed_total": a.state.count_done(),
        "settings": {"profile": a.ntv.profile_id or "-", "dry_run": a.cfg["dry_run"], "cleanup": a.cfg["cleanup"],
                     "scan_interval_min": a.cfg["interval"], "lookback_days": a.cfg["lookback"],
                     "reask_days": a.cfg["reask_days"]},
        "activity": [{"at": t, "text": x} for t, x in reversed(a.recent_events)],
        "started": a.started,
        "now": time.time(),
    }


def render(s: dict) -> str:
    e = html.escape
    label = {"ok": "All systems working", "problem": "Problem detected", "starting": "Starting up"}[s["overall"]]

    def state(ok):
        return ('<span class="pill ok">Working</span>' if ok else
                '<span class="pill bad">Down</span>' if ok is False else '<span class="pill wait">Waiting</span>')

    rows = "".join(
        f"<tr><td>{e(x['name'])}</td><td>{state(x['ok'])}</td>"
        f"<td>{e(_t(x['checked']))} <span class=dim>{e(_ago(x['checked']))}</span></td>"
        f"<td>{e(x['reason'] or '')}{(' <span class=dim>(since ' + e(_t(x['since'])) + ')</span>') if x.get('since') and x['ok'] is False else ''}</td></tr>"
        for x in s["services"])
    acts = "".join(f"<tr><td class=nowrap>{e(_t(x['at']))}</td><td>{e(x['text'])}</td></tr>" for x in s["activity"]) \
        or '<tr><td colspan=2 class=dim>Nothing yet since the container started.</td></tr>'
    st = s["settings"]
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><meta http-equiv=refresh content=30>
<title>NTV Allocator</title><link rel=icon type="image/svg+xml" href="__FAVICON__">
<script>try{{var t=localStorage.getItem("ntva-theme");if(t)document.documentElement.dataset.theme=t}}catch(e){{}}</script><style>
:root{{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--dim:#6b7385;--line:#e3e6ec;--ok:#1f8a4c;--bad:#c62f2f;--wait:#a26b00}}
@media (prefers-color-scheme:dark){{:root:not([data-theme=light]){{--bg:#14171c;--card:#1c2027;--fg:#e6e9ef;--dim:#8a93a6;--line:#2b313b;--ok:#3fbf73;--bad:#ef5a5a;--wait:#e0a83a}}}}
:root[data-theme=dark]{{--bg:#14171c;--card:#1c2027;--fg:#e6e9ef;--dim:#8a93a6;--line:#2b313b;--ok:#3fbf73;--bad:#ef5a5a;--wait:#e0a83a}}
.brand{{display:flex;align-items:center;gap:14px}}.logo svg{{width:52px;height:52px;display:block}}
.head{{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}}
.theme{{background:var(--card);border:1px solid var(--line);color:var(--fg);border-radius:8px;width:38px;height:38px;
display:grid;place-items:center;cursor:pointer;padding:0;flex:none}}.theme:hover{{border-color:var(--dim)}}
.theme svg{{width:18px;height:18px}}.theme .sun{{display:none}}
:root[data-theme=dark] .theme .sun{{display:block}}:root[data-theme=dark] .theme .moon{{display:none}}
@media (prefers-color-scheme:dark){{:root:not([data-theme=light]) .theme .sun{{display:block}}:root:not([data-theme=light]) .theme .moon{{display:none}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:960px;margin:0 auto;padding:24px 16px}}h1{{font-size:20px;margin:0 0 4px}}h2{{font-size:15px;margin:0 0 10px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px;margin:0 0 16px;overflow-x:auto}}
.banner{{display:flex;align-items:center;gap:10px;font-weight:600;font-size:17px}}
.dot{{width:12px;height:12px;border-radius:50%;background:var(--{"ok" if s["overall"]=="ok" else "bad" if s["overall"]=="problem" else "wait"})}}
table{{width:100%;border-collapse:collapse}}td,th{{text-align:left;padding:7px 8px;border-top:1px solid var(--line);vertical-align:top}}
th{{color:var(--dim);font-weight:500;border-top:0}}.dim{{color:var(--dim)}}.nowrap{{white-space:nowrap}}
.pill{{font-size:12px;font-weight:600;padding:2px 8px;border-radius:10px;white-space:nowrap}}
.pill.ok{{color:var(--ok);border:1px solid var(--ok)}}.pill.bad{{color:var(--bad);border:1px solid var(--bad)}}.pill.wait{{color:var(--wait);border:1px solid var(--wait)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}}.num{{font-size:22px;font-weight:600}}
</style></head><body><main>
<div class=head><div class=brand><span class=logo>__LOGO__</span><div><h1>NTV Allocator</h1><p class=dim>Refreshes every 30 seconds. Running since {e(_t(s["started"]))}.</p></div></div>
<button class=theme id=theme type=button aria-label="Toggle dark mode" title="Toggle dark mode">
<svg class=moon viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>
<svg class=sun viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
</button></div>
<script>document.getElementById("theme").onclick=function(){{var r=document.documentElement,
d=r.dataset.theme?r.dataset.theme==="dark":matchMedia("(prefers-color-scheme: dark)").matches,n=d?"light":"dark";
r.dataset.theme=n;try{{localStorage.setItem("ntva-theme",n)}}catch(e){{}}}};</script>
<div class=card><div class=banner><span class=dot></span>{e(label)}</div></div>
<div class=card><h2>Connections</h2><table><tr><th>Service</th><th>Status</th><th>Last check</th><th>Detail</th></tr>{rows}</table></div>
<div class=card><div class=grid>
<div><div class=dim>Last scan</div><div>{e(_t(s["last_scan"]))}</div><div class=dim>{e(s["last_scan_summary"])}</div></div>
<div><div class=dim>Next scan</div><div>{e(_t(s["next_run"]))}</div><div class=dim>{e(_ago(s["next_run"]))}</div></div>
<div><div class=dim>Open placement questions</div><div class=num>{s["open_questions"]}</div></div>
<div><div class=dim>Open removal questions</div><div class=num>{s["open_removals"]}</div></div>
</div></div>
<div class=card><h2>Recent activity</h2><table>{acts}</table></div>
<div class=card><h2>Settings</h2><table>
<tr><td>NostalgiaTV profile</td><td>{e(st["profile"])}</td></tr>
<tr><td>Dry run</td><td>{"On (nothing is changed)" if st["dry_run"] else "Off"}</td></tr>
<tr><td>Clean-up of deleted titles</td><td>{"On" if st["cleanup"] else "Off"}</td></tr>
<tr><td>Scan interval</td><td>{st["scan_interval_min"]} min</td></tr>
<tr><td>Look-back for new titles</td><td>{st["lookback_days"]} days</td></tr>
<tr><td>Skipped titles asked again after</td><td>{st["reask_days"]} days</td></tr>
</table><p class=dim>JSON for dashboards: <code>/api/status</code></p></div>
</main></body></html>""".replace("__LOGO__", LOGO_SVG).replace("__FAVICON__", FAVICON)


async def start(a, port: int) -> None:
    async def page(_):
        return web.Response(text=render(snapshot(a)), content_type="text/html")

    async def api(_):
        return web.json_response(snapshot(a))

    app = web.Application()
    # the page answers on any path, so a stale or mistyped address (e.g. a cached /admin/ redirect) still works
    app.add_routes([web.get("/api/status", api), web.get("/", page), web.get("/{tail:.*}", page)])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("status page on port %d", port)
