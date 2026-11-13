"""Read-only status page: http://<nas>:<STATUS_PORT>/ (HTML) and /api/status (JSON).

Shows no tokens or passwords. Served with aiohttp, which discord.py already depends on.
"""
from __future__ import annotations

import base64
import html
import re
import logging
import time
from datetime import datetime

from aiohttp import web

log = logging.getLogger("status")

# The app logo, embedded so the page needs no static files (source: assets/logo.svg)
LOGO_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><defs><clipPath id="ntva-sq"><rect width="512" height="512" rx="104"/></clipPath><clipPath id="ntva-tri"><path d="M30.0 82.4 Q30.0 32.0 77.6 57.2 L338.0 216.8 Q382.8 256.0 338.0 295.2 L77.6 454.8 Q30.0 480.0 30.0 429.6 Z"/></clipPath></defs><rect width="512" height="512" rx="104" fill="#0f1a2b"/><g clip-path="url(#ntva-sq)"><g transform="translate(47.2 -26.2) scale(1.1)"><path d="M172.5 115.4 L209.5 138.0 L909.5 138.0 L872.5 115.4 Z" fill="#ef5350"/><path d="M209.5 138.0 L246.5 160.7 L946.5 160.7 L909.5 138.0 Z" fill="#47b86b"/><path d="M246.5 160.7 L283.4 183.4 L983.4 183.4 L946.5 160.7 Z" fill="#3f86e8"/><path d="M30.0 82.4 Q30.0 32.0 77.6 57.2 L338.0 216.8 Q382.8 256.0 338.0 295.2 L77.6 454.8 Q30.0 480.0 30.0 429.6 Z" fill="#e8c98f"/><path d="M172.5 115.4 L283.4 183.4 L181.4 307.6 L81.8 225.8 Z" fill="#ffffff" clip-path="url(#ntva-tri)"/><path d="M30.0 82.4 Q30.0 32.0 77.6 57.2 L338.0 216.8 Q382.8 256.0 338.0 295.2 L77.6 454.8 Q30.0 480.0 30.0 429.6 Z" fill="none" stroke="#0f1a2b" stroke-width="12" stroke-linejoin="round"/><g stroke="#172437" stroke-width="12.5" stroke-linecap="round"><path d="M131.0 203.0 L98.3 150.7"/><path d="M145.0 203.0 L177.7 150.7"/></g><circle cx="98.3" cy="150.7" r="11.8" fill="#172437"/><circle cx="177.7" cy="150.7" r="11.8" fill="#172437"/><rect x="53.0" y="201.0" width="170.0" height="142.0" rx="26.5" fill="#172437"/><rect x="71.3" y="215.0" width="32.3" height="114.0" rx="5" fill="#ef5350"/><rect x="121.9" y="215.0" width="32.3" height="114.0" rx="5" fill="#47b86b"/><rect x="172.4" y="215.0" width="32.3" height="114.0" rx="5" fill="#3f86e8"/></g></g></svg>'
FAVICON = "data:image/svg+xml;base64," + base64.b64encode(LOGO_SVG.encode()).decode()


EDITOR_JS = '<script>\nfunction getKey(){ try{ return localStorage.getItem("ntva-edit-key") || ""; }catch(e){ return ""; } }\nfunction setKey(k){ try{ localStorage.setItem("ntva-edit-key", k); }catch(e){} }\ndocument.addEventListener("click", async function(ev){\n  var b = ev.target;\n  if (b.classList.contains("edit")) {\n    var row = b.closest("tr"), key = row.dataset.key;\n    document.querySelectorAll("tr.editor").forEach(function(x){ x.remove(); });\n    var ed = document.createElement("tr"); ed.className = "editor";\n    var cols = row.children.length; ed.innerHTML = \'<td colspan=\' + cols + \' class=dim>Loading current channels...</td>\';\n    row.after(ed);\n    try {\n      var r = await fetch("/api/placements/" + encodeURIComponent(key)); var d = await r.json();\n      var html = \'<td colspan=\' + cols + \'><div class=dim>Channels for <b>\' + d.label.replace(/</g,"&lt;") + \'</b> (as currently saved in NostalgiaTV):</div><div class=opts>\';\n      d.options.forEach(function(o){ html += \'<label><input type=checkbox value="\' + o.id + \'"\' + (d.current.indexOf(o.id) >= 0 ? " checked" : "") + \'>\' + o.name.replace(/</g,"&lt;") + \'</label>\'; });\n      html += \'</div><button class=save type=button>Save</button> <button class=cancel type=button>Cancel</button><span class=msg></span></td>\';\n      ed.innerHTML = html; ed.dataset.key = key;\n    } catch(e) { ed.innerHTML = \'<td colspan=\' + cols + \'>Could not load channels.</td>\'; }\n  }\n  if (b.classList.contains("cancel")) { b.closest("tr").remove(); }\n  if (b.classList.contains("save")) {\n    var ed = b.closest("tr"), msg = ed.querySelector(".msg");\n    var chans = Array.from(ed.querySelectorAll("input:checked")).map(function(i){ return i.value; });\n    var k = getKey(); if (!k) { k = prompt("Edit key (STATUS_EDIT_KEY):") || ""; }\n    msg.textContent = "Saving...";\n    var r = await fetch("/api/placements/" + encodeURIComponent(ed.dataset.key), {method:"POST",\n      headers:{"Content-Type":"application/json","X-Edit-Key":k}, body: JSON.stringify({channels: chans})});\n    if (r.status === 403) { setKey(""); msg.textContent = "Wrong edit key."; return; }\n    var d = await r.json(); setKey(k); msg.textContent = d.message;\n    if (d.names) {\n      var row = ed.previousElementSibling, cell = row && row.querySelector(".chans");\n      if (cell) cell.textContent = d.names.join(", ") || "-";\n      if (window.ntvaUpdated) window.ntvaUpdated(ed.dataset.key, d.channels, d.names);\n    }\n    if (d.ok) setTimeout(function(){ ed.remove(); }, 2200);\n  }\n});\n</script>'


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
        "placements": [{**p_, "names": [a.rules.names.get(c, c) for c in p_["channels"]]}
                       for p_ in a.state.recent_placements()],
        "editable": bool(a.cfg.get("edit_key")),
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
    def how_label(h):
        h = h or ""
        base = "Automatic" if h.startswith("auto") else ("Approved by " + h.split("approved by ", 1)[1].split(" (")[0]
                                                         if "approved by " in h else h)
        return base + (" (edited)" if "edited on status page" in h else "")
    prow = "".join(
        f'<tr data-key="{e(p["key"])}"><td class=nowrap>{e(_t(p["at"]))}</td><td>{e(p["label"])}</td>'
        f'<td class=chans>{e(", ".join(p["names"]) or "-")}</td><td class=dim>{e(how_label(p["how"]))}</td>'
        f'<td>{"<button class=edit type=button>Edit</button>" if s["editable"] else ""}</td></tr>'
        for p in s["placements"])
    places = (f'<table class=places><tr><th>When</th><th>Title</th><th>Channels</th><th>How</th><th></th></tr>{prow}</table>'
              if prow else '<p class=dim>Nothing placed yet.</p>')
    if not s["editable"]:
        places += '<p class=dim>Editing is off. Set STATUS_EDIT_KEY in the container environment to fix placements here.</p>'
    st = s["settings"]
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
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
.edit,.save,.cancel{{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:4px 10px;cursor:pointer;font:inherit;font-size:13px}}
.save{{border-color:var(--ok);color:var(--ok)}}.editor td{{background:var(--bg)}}
.opts{{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:4px 14px;margin:6px 0 10px}}
.opts label{{display:flex;gap:6px;align-items:center;font-size:14px}}.msg{{font-size:13px;margin-left:8px}}
.nav{{display:flex;gap:4px;margin:2px 0 4px}}.nav a{{color:var(--dim);text-decoration:none;padding:2px 10px;border:1px solid transparent;border-radius:6px;font-size:14px}}
.nav a.on{{color:var(--fg);border-color:var(--line);background:var(--card)}}.nav a:hover{{color:var(--fg)}}
.tools{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px}}
.tools input,.tools select{{background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:inherit;font-size:14px}}
.tools input{{flex:1;min-width:180px}}
@media (max-width:600px){{.libt th:nth-child(2),.libt td:nth-child(2){{display:none}}.places th:nth-child(4),.places td:nth-child(4){{display:none}}}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}}.num{{font-size:22px;font-weight:600}}
</style></head><body><main>
<div class=head><div class=brand><span class=logo>__LOGO__</span><div><h1>NTV Allocator</h1><nav class=nav><a href="/" class=on>Status</a><a href="/library">Library</a></nav><p class=dim id=sub>Refreshes every 30 seconds. Running since {e(_t(s["started"]))}.</p></div></div>
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
<div class=card><h2>Recent placements</h2>{places}</div>
<div class=card><h2>Recent activity</h2><table>{acts}</table></div>
<div class=card><h2>Settings</h2><table>
<tr><td>NostalgiaTV profile</td><td>{e(st["profile"])}</td></tr>
<tr><td>Dry run</td><td>{"On (nothing is changed)" if st["dry_run"] else "Off"}</td></tr>
<tr><td>Clean-up of deleted titles</td><td>{"On" if st["cleanup"] else "Off"}</td></tr>
<tr><td>Scan interval</td><td>{st["scan_interval_min"]} min</td></tr>
<tr><td>Look-back for new titles</td><td>{st["lookback_days"]} days</td></tr>
<tr><td>Skipped titles asked again after</td><td>{st["reask_days"]} days</td></tr>
</table><p class=dim>JSON for dashboards: <code>/api/status</code></p></div>
</main>
<script>
setInterval(function(){{ if(!document.querySelector("tr.editor")) location.reload(); }}, 30000);
</script>
__EDITOR__</body></html>""".replace("__LOGO__", LOGO_SVG).replace("__FAVICON__", FAVICON).replace("__EDITOR__", EDITOR_JS)


def render_library(s: dict) -> str:
    """The Library page: every managed show and movie, searchable and editable (data from /api/library)."""
    page = render(s)
    head = page[: page.index("<div class=card><div class=banner>")]
    head = head.replace('<a href="/" class=on>Status</a><a href="/library">Library</a>',
                        '<a href="/">Status</a><a href="/library" class=on>Library</a>')
    head = re.sub(r'<p class=dim id=sub>.*?</p>', '<p class=dim id=sub>Every show and movie in the managed libraries. '
                  'Shows are listed once, without seasons or episodes.</p>', head, count=1, flags=re.S)
    note = ("" if s["editable"] else
            '<p class=dim>Editing is off. Set STATUS_EDIT_KEY in the container environment to change channels here.</p>')
    body = f'''<div class=card>
<div class=tools><input id=q type=search placeholder="Search titles" autocomplete=off>
<select id=lib><option value="">All libraries</option></select>
<select id=chan><option value="">Any channel</option><option value="__none">No channel</option></select>
<span id=count class=dim>Loading...</span></div>{note}
<p class=dim style="font-size:13px;margin:0 0 8px">Taking every channel off a title sends it back to Discord as a question on the next scan.</p>
<table class=libt><thead><tr><th>Title</th><th>Library</th><th>Channels</th><th></th></tr></thead><tbody id=rows></tbody></table>
</div></main>
<script>
var DATA = null;
function esc(t){{ return String(t).replace(/[&<>"]/g, function(c){{ return {{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}}[c]; }}); }}
function draw(){{
  var q = document.getElementById("q").value.trim().toLowerCase(), lib = document.getElementById("lib").value,
      ch = document.getElementById("chan").value, out = [], n = 0;
  DATA.items.forEach(function(it){{
    if (q && it.label.toLowerCase().indexOf(q) < 0) return;
    if (lib && it.library !== lib) return;
    if (ch === "__none" && it.channels.length) return;
    if (ch && ch !== "__none" && it.channels.indexOf(ch) < 0) return;
    n++;
    out.push('<tr data-key="' + esc(it.key) + '"><td>' + esc(it.label) + '</td><td class=dim>' + esc(it.library) +
             '</td><td class=chans>' + esc(it.names.join(", ") || "-") + '</td><td>' +
             (DATA.editable ? '<button class=edit type=button>Edit</button>' : '') + '</td></tr>');
  }});
  document.getElementById("rows").innerHTML = out.join("");
  document.getElementById("count").textContent = n + " of " + DATA.items.length + " titles";
}}
window.ntvaUpdated = function(key, chans, names){{
  DATA.items.forEach(function(it){{ if (it.key === key) {{ it.channels = chans; it.names = names; }} }});
}};
fetch("/api/library").then(function(r){{ return r.json(); }}).then(function(d){{
  if (d.error) {{ document.getElementById("count").textContent = d.error; return; }}
  DATA = d;
  var lib = document.getElementById("lib"), chan = document.getElementById("chan");
  d.libraries.forEach(function(l){{ lib.insertAdjacentHTML("beforeend", '<option>' + esc(l) + '</option>'); }});
  d.options.forEach(function(o){{ chan.insertAdjacentHTML("beforeend", '<option value="' + esc(o.id) + '">' + esc(o.name) + '</option>'); }});
  ["q","lib","chan"].forEach(function(id){{ document.getElementById(id).addEventListener("input", draw); }});
  draw();
}}).catch(function(){{ document.getElementById("count").textContent = "Could not load the library."; }});
</script>
__EDITOR__</body></html>'''
    return (head + body).replace("__LOGO__", LOGO_SVG).replace("__FAVICON__", FAVICON).replace("__EDITOR__", EDITOR_JS)


async def start(a, port: int) -> None:
    async def page(_):
        return web.Response(text=render(snapshot(a)), content_type="text/html")

    async def api(_):
        return web.json_response(snapshot(a))

    import hmac

    def _sort(name):
        n = name.casefold()
        return n[4:] if n.startswith("the ") else n

    async def placement_get(req):
        key = req.match_info["key"]
        row = a.state.get_done(key)
        try:
            current = await a.current_channels(key)
        except Exception as exc:
            return web.json_response({"error": f"NostalgiaTV not reachable ({type(exc).__name__})"}, status=502)
        opts = sorted(({"id": c, "name": n} for c, n in a.rules.names.items()), key=lambda o: _sort(o["name"]))
        return web.json_response({"label": row["label"] if row else key, "current": current, "options": opts})

    async def library_page(_):
        return web.Response(text=render_library(snapshot(a)), content_type="text/html")

    async def library_api(_):
        try:
            items = await a.library_view()
        except Exception as exc:
            return web.json_response({"error": f"Could not read the library ({type(exc).__name__})"}, status=502)
        names = a.rules.names
        opts = sorted(({"id": c, "name": n} for c, n in names.items()), key=lambda o: _sort(o["name"]))
        return web.json_response({
            "items": [{**i, "names": [names.get(c, c) for c in i["channels"]]} for i in items],
            "libraries": sorted({i["library"] for i in items}), "options": opts,
            "editable": bool(a.cfg.get("edit_key"))})

    async def placement_post(req):
        need = a.cfg.get("edit_key") or ""
        got = req.headers.get("X-Edit-Key", "")
        if not need or not hmac.compare_digest(need.encode(), got.encode()):
            return web.json_response({"ok": False, "message": "Editing is not allowed."}, status=403)
        try:
            body = await req.json()
            chans = [str(c) for c in body.get("channels", [])]
        except Exception:
            return web.json_response({"ok": False, "message": "Bad request."}, status=400)
        try:
            res = await a.edit_placement(req.match_info["key"], chans)
        except Exception as exc:
            log.exception("status page edit failed")
            return web.json_response({"ok": False, "message": f"Failed: {type(exc).__name__}. Nothing recorded."}, status=500)
        res["names"] = [a.rules.names.get(c, c) for c in res.get("channels", [])]
        return web.json_response(res)

    app = web.Application()
    # the page answers on any path, so a stale or mistyped address (e.g. a cached /admin/ redirect) still works
    app.add_routes([web.get("/api/status", api), web.get("/api/placements/{key}", placement_get),
                    web.post("/api/placements/{key}", placement_post),
                    web.get("/api/library", library_api), web.get("/library", library_page),
                    web.get("/", page), web.get("/{tail:.*}", page)])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("status page on port %d", port)
