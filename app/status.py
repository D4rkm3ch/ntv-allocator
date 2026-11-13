"""Status page: http://<nas>:<STATUS_PORT>/

- First visit: create the admin password (needs the one-time setup code from the container log).
- After that, viewing and editing need a login (30-day session per browser).
- Pages: Status (connections, scans, recent placements, settings, account) and Library.
- /api/status stays open for dashboards and carries health information only (no titles).

Served with aiohttp, which discord.py already depends on.
"""
from __future__ import annotations

import base64
import html
import json
import logging
import time
from datetime import datetime

from aiohttp import web

from . import settings as settings_mod

log = logging.getLogger("status")
COOKIE = "ntva_session"

# The app logo, embedded so the page needs no static files (source: assets/logo.svg)
LOGO_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><defs><clipPath id="ntva-sq"><rect width="512" height="512" rx="104"/></clipPath><clipPath id="ntva-tri"><path d="M30.0 82.4 Q30.0 32.0 77.6 57.2 L338.0 216.8 Q382.8 256.0 338.0 295.2 L77.6 454.8 Q30.0 480.0 30.0 429.6 Z"/></clipPath></defs><rect width="512" height="512" rx="104" fill="#0f1a2b"/><g clip-path="url(#ntva-sq)"><g transform="translate(47.2 -26.2) scale(1.1)"><path d="M172.5 115.4 L209.5 138.0 L909.5 138.0 L872.5 115.4 Z" fill="#ef5350"/><path d="M209.5 138.0 L246.5 160.7 L946.5 160.7 L909.5 138.0 Z" fill="#47b86b"/><path d="M246.5 160.7 L283.4 183.4 L983.4 183.4 L946.5 160.7 Z" fill="#3f86e8"/><path d="M30.0 82.4 Q30.0 32.0 77.6 57.2 L338.0 216.8 Q382.8 256.0 338.0 295.2 L77.6 454.8 Q30.0 480.0 30.0 429.6 Z" fill="#e8c98f"/><path d="M172.5 115.4 L283.4 183.4 L181.4 307.6 L81.8 225.8 Z" fill="#ffffff" clip-path="url(#ntva-tri)"/><path d="M30.0 82.4 Q30.0 32.0 77.6 57.2 L338.0 216.8 Q382.8 256.0 338.0 295.2 L77.6 454.8 Q30.0 480.0 30.0 429.6 Z" fill="none" stroke="#0f1a2b" stroke-width="12" stroke-linejoin="round"/><g stroke="#172437" stroke-width="12.5" stroke-linecap="round"><path d="M131.0 203.0 L98.3 150.7"/><path d="M145.0 203.0 L177.7 150.7"/></g><circle cx="98.3" cy="150.7" r="11.8" fill="#172437"/><circle cx="177.7" cy="150.7" r="11.8" fill="#172437"/><rect x="53.0" y="201.0" width="170.0" height="142.0" rx="26.5" fill="#172437"/><rect x="71.3" y="215.0" width="32.3" height="114.0" rx="5" fill="#ef5350"/><rect x="121.9" y="215.0" width="32.3" height="114.0" rx="5" fill="#47b86b"/><rect x="172.4" y="215.0" width="32.3" height="114.0" rx="5" fill="#3f86e8"/></g></g></svg>'
FAVICON = "data:image/svg+xml;base64," + base64.b64encode(LOGO_SVG.encode()).decode()

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--dim:#6b7385;--line:#e3e6ec;--ok:#1f8a4c;--bad:#c62f2f;--wait:#a26b00}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#14171c;--card:#1c2027;--fg:#e6e9ef;--dim:#8a93a6;--line:#2b313b;--ok:#3fbf73;--bad:#ef5a5a;--wait:#e0a83a}}
:root[data-theme=dark]{--bg:#14171c;--card:#1c2027;--fg:#e6e9ef;--dim:#8a93a6;--line:#2b313b;--ok:#3fbf73;--bad:#ef5a5a;--wait:#e0a83a}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:960px;margin:0 auto;padding:24px 16px}h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:0 0 10px}
.brand{display:flex;align-items:center;gap:14px}.logo svg{width:52px;height:52px;display:block}
.head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:14px}
.theme{background:var(--card);border:1px solid var(--line);color:var(--fg);border-radius:8px;width:38px;height:38px;display:grid;place-items:center;cursor:pointer;padding:0;flex:none}
.theme:hover{border-color:var(--dim)}.theme svg{width:18px;height:18px}.theme .sun{display:none}
:root[data-theme=dark] .theme .sun{display:block}:root[data-theme=dark] .theme .moon{display:none}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]) .theme .sun{display:block}:root:not([data-theme=light]) .theme .moon{display:none}}
.nav{display:flex;gap:4px;margin:2px 0 4px}.nav a{color:var(--dim);text-decoration:none;padding:2px 10px;border:1px solid transparent;border-radius:6px;font-size:14px}
.nav a.on{color:var(--fg);border-color:var(--line);background:var(--card)}.nav a:hover{color:var(--fg)}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px;margin:0 0 16px;overflow-x:auto}
.narrow{max-width:420px;margin:40px auto}
.banner{display:flex;align-items:center;gap:10px;font-weight:600;font-size:17px}.dot{width:12px;height:12px;border-radius:50%}
.dot.ok{background:var(--ok)}.dot.problem{background:var(--bad)}.dot.starting{background:var(--wait)}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:7px 8px;border-top:1px solid var(--line);vertical-align:top}
th{color:var(--dim);font-weight:500;border-top:0}.dim{color:var(--dim)}.nowrap{white-space:nowrap}
.pill{font-size:12px;font-weight:600;padding:2px 8px;border-radius:10px;white-space:nowrap}
.pill.ok{color:var(--ok);border:1px solid var(--ok)}.pill.bad{color:var(--bad);border:1px solid var(--bad)}.pill.wait{color:var(--wait);border:1px solid var(--wait)}
button.b,.edit,.save,.cancel{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:5px 12px;cursor:pointer;font:inherit;font-size:14px}
.save,button.primary{border-color:var(--ok);color:var(--ok)}.editor td{background:var(--bg)}
.opts{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:4px 14px;margin:6px 0 10px}
.opts label{display:flex;gap:6px;align-items:center;font-size:14px}.msg{font-size:13px;margin-left:8px}
.tools{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px}
input[type=text],input[type=password],input[type=search],input[type=number],select{background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:7px 9px;font:inherit;font-size:14px;box-sizing:border-box}
.tools input{flex:1;min-width:180px}
.field{display:block;margin:0 0 12px}.field span{display:block;font-size:13px;color:var(--dim);margin-bottom:4px}.field input{width:100%}
.err{color:var(--bad);font-size:14px;min-height:20px;margin:4px 0 8px}
.set{display:grid;grid-template-columns:1fr auto;gap:4px 16px;align-items:center;padding:8px 0;border-top:1px solid var(--line)}
.set:first-of-type{border-top:0}.set .help{grid-column:1/-1;font-size:13px;color:var(--dim)}
.set input[type=number]{width:96px}
.set input[type=checkbox]{appearance:none;-webkit-appearance:none;width:42px;height:24px;border-radius:12px;background:var(--line);position:relative;cursor:pointer;margin:0;transition:background .15s}
.set input[type=checkbox]::after{content:"";position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:#fff;transition:left .15s}
.set input[type=checkbox]:checked{background:var(--ok)}.set input[type=checkbox]:checked::after{left:21px}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}.num{font-size:22px;font-weight:600}
@media (max-width:600px){.libt th:nth-child(2),.libt td:nth-child(2){display:none}}
"""

THEME_BUTTON = """<button class=theme id=theme type=button aria-label="Toggle dark mode" title="Toggle dark mode">
<svg class=moon viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>
<svg class=sun viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
</button>"""

COMMON_JS = """<script>
document.getElementById("theme").onclick=function(){var r=document.documentElement,
d=r.dataset.theme?r.dataset.theme==="dark":matchMedia("(prefers-color-scheme: dark)").matches,n=d?"light":"dark";
r.dataset.theme=n;try{localStorage.setItem("ntva-theme",n)}catch(e){}};
async function post(url, data){
  var r = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json","X-Requested-With":"ntva"},
                            body: JSON.stringify(data || {})});
  if (r.status === 401) { location.href = "/"; return {ok:false, message:"Please log in again."}; }
  try { return await r.json(); } catch(e) { return {ok:false, message:"Unexpected response."}; }
}
function esc(t){ return String(t).replace(/[&<>"]/g, function(c){ return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]; }); }
</script>"""

EDITOR_JS = """<script>
document.addEventListener("click", async function(ev){
  var b = ev.target;
  if (b.classList.contains("edit")) {
    var row = b.closest("tr"), key = row.dataset.key, cols = row.children.length;
    document.querySelectorAll("tr.editor").forEach(function(x){ x.remove(); });
    var ed = document.createElement("tr"); ed.className = "editor";
    ed.innerHTML = '<td colspan=' + cols + ' class=dim>Loading current channels...</td>';
    row.after(ed);
    try {
      var r = await fetch("/api/placements/" + encodeURIComponent(key));
      if (r.status === 401) { location.href = "/"; return; }
      var d = await r.json();
      var h = '<td colspan=' + cols + '><div class=dim>Channels for <b>' + esc(d.label) + '</b> (as currently saved in NostalgiaTV):</div><div class=opts>';
      d.options.forEach(function(o){ h += '<label><input type=checkbox value="' + esc(o.id) + '"' + (d.current.indexOf(o.id) >= 0 ? " checked" : "") + '>' + esc(o.name) + '</label>'; });
      h += '</div><button class=save type=button>Save</button> <button class=cancel type=button>Cancel</button><span class=msg></span></td>';
      ed.innerHTML = h; ed.dataset.key = key;
    } catch(e) { ed.innerHTML = '<td colspan=' + cols + '>Could not load channels.</td>'; }
  }
  if (b.classList.contains("cancel")) { b.closest("tr").remove(); }
  if (b.classList.contains("save")) {
    var ed = b.closest("tr"), msg = ed.querySelector(".msg");
    var chans = Array.from(ed.querySelectorAll("input:checked")).map(function(i){ return i.value; });
    msg.textContent = "Saving...";
    var d = await post("/api/placements/" + encodeURIComponent(ed.dataset.key), {channels: chans});
    msg.textContent = d.message || "";
    if (d.names) {
      var row = ed.previousElementSibling, cell = row && row.querySelector(".chans");
      if (cell) cell.textContent = d.names.join(", ") || "-";
      if (window.ntvaUpdated) window.ntvaUpdated(ed.dataset.key, d.channels, d.names);
    }
    if (d.ok) setTimeout(function(){ ed.remove(); }, 2200);
  }
});
</script>"""


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


def _sort(name: str) -> str:
    n = name.casefold()
    return n[4:] if n.startswith("the ") else n


def shell(sub: str, body: str, nav: str | None = None, scripts: str = "") -> str:
    """Shared page layout: head, logo, title, optional Status/Library tabs, theme toggle."""
    tabs = ""
    if nav:
        tabs = ('<nav class=nav>' + "".join(f'<a href="{href}"{" class=on" if key == nav else ""}>{name}</a>'
                for key, href, name in (("status", "/", "Status"), ("library", "/library", "Library"))) + '</nav>')
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>NTV Allocator</title><link rel=icon type="image/svg+xml" href="{FAVICON}">
<script>try{{var t=localStorage.getItem("ntva-theme");if(t)document.documentElement.dataset.theme=t}}catch(e){{}}</script>
<style>{CSS}</style></head><body><main>
<div class=head><div class=brand><span class=logo>{LOGO_SVG}</span><div><h1>NTV Allocator</h1>{tabs}
<p class=dim id=sub>{sub}</p></div></div>{THEME_BUTTON}</div>
{body}</main>{COMMON_JS}{scripts}</body></html>"""


# ------------------------------------------------------------------ data
def health(a) -> dict:
    """Connection and scan health (also the public /api/status payload)."""
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
    return {"overall": overall, "services": services, "last_scan": sc.get("at"),
            "last_scan_summary": sc.get("summary", ""), "next_run": a.next_run,
            "open_questions": a.state.count_pending(), "open_removals": a.state.count_removals_pending(),
            "dry_run": a.cfg["dry_run"], "now": time.time()}


def snapshot(a) -> dict:
    """Everything the status page shows (logged-in only)."""
    return {**health(a),
            "placements": [{**p, "names": [a.rules.names.get(c, c) for c in p["channels"]]}
                           for p in a.state.recent_placements()],
            "profile": a.ntv.profile_id or "-", "settings": {k: a.cfg[k] for k in settings_mod.SPEC},
            "started": a.started}


# ------------------------------------------------------------------ pages
def render(s: dict) -> str:
    e = html.escape
    label = {"ok": "All systems working", "problem": "Problem detected", "starting": "Starting up"}[s["overall"]]

    def pill(ok):
        return ('<span class="pill ok">Working</span>' if ok else
                '<span class="pill bad">Down</span>' if ok is False else '<span class="pill wait">Waiting</span>')

    rows = "".join(
        f"<tr><td>{e(x['name'])}</td><td>{pill(x['ok'])}</td>"
        f"<td class=nowrap>{e(_t(x['checked']))} <span class=dim>{e(_ago(x['checked']))}</span></td>"
        f"<td>{e(x['reason'] or '')}"
        f"{(' <span class=dim>(since ' + e(_t(x['since'])) + ')</span>') if x.get('since') and x['ok'] is False else ''}</td></tr>"
        for x in s["services"])
    prow = "".join(
        f'<tr data-key="{e(p["key"])}"><td class=nowrap>{e(_t(p["at"]))}</td><td>{e(p["label"])}</td>'
        f'<td class=chans>{e(", ".join(p["names"]) or "-")}</td><td><button class=edit type=button>Edit</button></td></tr>'
        for p in s["placements"])
    places = (f'<table><tr><th>When</th><th>Title</th><th>Channels</th><th></th></tr>{prow}</table>'
              if prow else '<p class=dim>Nothing placed in the last 14 days.</p>')
    dry = ('<div class=card><div class=banner><span class="dot starting"></span>Dry run is on: questions are posted '
           'in Discord but nothing changes in NostalgiaTV. Turn it off under Settings when ready.</div></div>'
           if s["dry_run"] else "")
    sets = ""
    for key, (lab, kind, _, lo, hi, _, helptext) in settings_mod.SPEC.items():
        v = s["settings"][key]
        ctl = (f'<input type=checkbox name="{key}"{" checked" if v else ""}>' if kind is bool else
               f'<input type=number name="{key}" value="{v}" min="{lo}" max="{hi}" step=1>')
        sets += f'<div class=set><label for="{key}">{e(lab)}</label>{ctl}<div class=help>{e(helptext)}</div></div>'
    body = f"""{dry}
<div class=card><div class=banner><span class="dot {s['overall']}"></span>{e(label)}</div></div>
<div class=card><h2>Connections</h2><table><tr><th>Service</th><th>Status</th><th>Last check</th><th>Detail</th></tr>{rows}</table></div>
<div class=card><div class=grid>
<div><div class=dim>Last scan</div><div>{e(_t(s["last_scan"]))}</div><div class=dim>{e(s["last_scan_summary"])}</div></div>
<div><div class=dim>Next scan</div><div>{e(_t(s["next_run"]))}</div><div class=dim>{e(_ago(s["next_run"]))}</div></div>
<div><div class=dim>Open placement questions</div><div class=num>{s["open_questions"]}</div></div>
<div><div class=dim>Open removal questions</div><div class=num>{s["open_removals"]}</div></div>
</div></div>
<div class=card><h2>Recent placements</h2>{places}</div>
<div class=card><h2>Settings</h2><form id=settings>{sets}
<div class=row><button class="b primary" type=submit>Save settings</button><span class=msg id=setmsg></span></div></form>
<p class=dim style="font-size:13px;margin:12px 0 0">NostalgiaTV profile: {e(s["profile"])}. Dashboards can read health from <code>/api/status</code>.</p></div>
<div class=card><h2>Account</h2><form id=pw>
<label class=field><span>Current password</span><input type=password name=current autocomplete=current-password></label>
<label class=field><span>New password</span><input type=password name=password autocomplete=new-password></label>
<label class=field><span>Confirm new password</span><input type=password name=confirm autocomplete=new-password></label>
<div class=err id=pwerr></div>
<div class=row><button class="b primary" type=submit>Change password</button><button class=b type=button id=logout>Log out</button></div>
</form><p class=dim style="font-size:13px;margin:12px 0 0">Forgot the password? In the TrueNAS shell run
<code>sudo docker exec ntv-allocator python -m app.reset_password</code>, then create a new one here.</p></div>"""
    scripts = """<script>
setInterval(function(){
  var busy = document.querySelector("tr.editor") || document.activeElement && document.activeElement.closest("form");
  if (!busy && !window.ntvaDirty) location.reload();
}, 30000);
document.getElementById("settings").addEventListener("input", function(){ window.ntvaDirty = true; });
document.getElementById("settings").addEventListener("submit", async function(ev){
  ev.preventDefault(); var f = ev.target, data = {};
  f.querySelectorAll("input").forEach(function(i){ data[i.name] = i.type === "checkbox" ? i.checked : i.value; });
  var m = document.getElementById("setmsg"); m.textContent = "Saving...";
  var d = await post("/api/settings", data); m.textContent = d.message || "";
  if (d.ok) { window.ntvaDirty = false; setTimeout(function(){ location.reload(); }, 1200); }
});
document.getElementById("pw").addEventListener("submit", async function(ev){
  ev.preventDefault(); var f = ev.target;
  var d = await post("/api/password", {current: f.current.value, password: f.password.value, confirm: f.confirm.value});
  var el = document.getElementById("pwerr"); el.style.color = d.ok ? "var(--ok)" : ""; el.textContent = d.message || "";
  if (d.ok) f.reset();
});
document.getElementById("logout").onclick = async function(){ await post("/api/logout"); location.href = "/"; };
</script>""" + EDITOR_JS
    return shell(f"Refreshes every 30 seconds. Running since {e(_t(s['started']))}.", body, "status", scripts)


def render_library() -> str:
    body = """<div class=card>
<div class=tools><input id=q type=search placeholder="Search titles" autocomplete=off>
<select id=lib><option value="">All libraries</option></select>
<select id=chan><option value="">Any channel</option><option value="__none">No channel</option></select>
<span id=count class=dim>Loading...</span></div>
<p class=dim style="font-size:13px;margin:0 0 8px">Taking every channel off a title sends it back to Discord as a question on the next scan.</p>
<table class=libt><thead><tr><th>Title</th><th>Library</th><th>Channels</th><th></th></tr></thead><tbody id=rows></tbody></table>
</div>"""
    scripts = """<script>
var DATA = null;
function draw(){
  var q = document.getElementById("q").value.trim().toLowerCase(), lib = document.getElementById("lib").value,
      ch = document.getElementById("chan").value, out = [], n = 0;
  DATA.items.forEach(function(it){
    if (q && it.label.toLowerCase().indexOf(q) < 0) return;
    if (lib && it.library !== lib) return;
    if (ch === "__none" && it.channels.length) return;
    if (ch && ch !== "__none" && it.channels.indexOf(ch) < 0) return;
    n++;
    out.push('<tr data-key="' + esc(it.key) + '"><td>' + esc(it.label) + '</td><td class=dim>' + esc(it.library) +
             '</td><td class=chans>' + esc(it.names.join(", ") || "-") + '</td><td><button class=edit type=button>Edit</button></td></tr>');
  });
  document.getElementById("rows").innerHTML = out.join("");
  document.getElementById("count").textContent = n + " of " + DATA.items.length + " titles";
}
window.ntvaUpdated = function(key, chans, names){
  DATA.items.forEach(function(it){ if (it.key === key) { it.channels = chans; it.names = names; } });
};
fetch("/api/library").then(function(r){ if (r.status === 401) { location.href = "/"; throw 0; } return r.json(); }).then(function(d){
  if (d.error) { document.getElementById("count").textContent = d.error; return; }
  DATA = d;
  var lib = document.getElementById("lib"), chan = document.getElementById("chan");
  d.libraries.forEach(function(l){ lib.insertAdjacentHTML("beforeend", '<option>' + esc(l) + '</option>'); });
  d.options.forEach(function(o){ chan.insertAdjacentHTML("beforeend", '<option value="' + esc(o.id) + '">' + esc(o.name) + '</option>'); });
  ["q","lib","chan"].forEach(function(id){ document.getElementById(id).addEventListener("input", draw); });
  draw();
}).catch(function(e){ if (e !== 0) document.getElementById("count").textContent = "Could not load the library."; });
</script>""" + EDITOR_JS
    return shell("Every show and movie in the managed libraries. Shows are listed once, without seasons or episodes.",
                 body, "library", scripts)


def render_setup() -> str:
    body = """<div class="card narrow"><h2>Create the admin password</h2>
<p class=dim style="font-size:14px">This password protects the status page and its settings. Enter the setup code
printed in the container log (Dockge, ntv-allocator, logs), then choose a password of at least 8 characters.</p>
<form id=f>
<label class=field><span>Setup code</span><input type=text name=code autocomplete=off placeholder="XXXX-XXXX"></label>
<label class=field><span>Password</span><input type=password name=password autocomplete=new-password></label>
<label class=field><span>Confirm password</span><input type=password name=confirm autocomplete=new-password></label>
<div class=err id=err></div><button class="b primary" type=submit>Create password</button></form></div>"""
    scripts = """<script>
document.getElementById("f").addEventListener("submit", async function(ev){
  ev.preventDefault(); var f = ev.target;
  var d = await post("/api/setup", {code: f.code.value, password: f.password.value, confirm: f.confirm.value});
  if (d.ok) location.href = "/"; else document.getElementById("err").textContent = d.message;
});
</script>"""
    return shell("First-time setup", body, None, scripts)


def render_login() -> str:
    body = """<div class="card narrow"><h2>Log in</h2><form id=f>
<label class=field><span>Password</span><input type=password name=password autocomplete=current-password autofocus></label>
<div class=err id=err></div><button class="b primary" type=submit>Log in</button></form>
<p class=dim style="font-size:13px;margin:14px 0 0">Forgot it? In the TrueNAS shell run
<code>sudo docker exec ntv-allocator python -m app.reset_password</code>, then reload this page.</p></div>"""
    scripts = """<script>
document.getElementById("f").addEventListener("submit", async function(ev){
  ev.preventDefault();
  var d = await post("/api/login", {password: ev.target.password.value});
  if (d.ok) location.reload(); else document.getElementById("err").textContent = d.message;
});
</script>"""
    return shell("Log in to manage this instance.", body, None, scripts)


# ------------------------------------------------------------------ server
def _client_ip(req) -> str:
    fwd = req.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (req.remote or "?")


def _set_cookie(req, resp, token: str) -> None:
    secure = req.secure or req.headers.get("X-Forwarded-Proto", "") == "https"
    resp.set_cookie(COOKIE, token, max_age=30 * 86400, httponly=True, samesite="Strict", secure=secure, path="/")


async def start(a, port: int) -> None:
    auth = a.auth

    def logged_in(req) -> bool:
        return auth.is_set_up() and auth.valid(req.cookies.get(COOKIE))

    def deny():
        return web.json_response({"ok": False, "message": "Please log in."}, status=401)

    async def read_json(req) -> dict:
        if req.headers.get("X-Requested-With") != "ntva":          # blocks cross-site form posts
            raise web.HTTPForbidden()
        try:
            data = await req.json()
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}

    def html_resp(text: str):
        return web.Response(text=text, content_type="text/html", headers={"Cache-Control": "no-store"})

    def gate():
        if not auth.is_set_up():
            return html_resp(render_setup())
        return html_resp(render_login())

    async def page(req):
        return html_resp(render(snapshot(a))) if logged_in(req) else gate()

    async def library_page(req):
        return html_resp(render_library()) if logged_in(req) else gate()

    async def api_status(_):
        return web.json_response(health(a))

    async def api_setup(req):
        data = await read_json(req)
        err = auth.set_up(str(data.get("code", "")), str(data.get("password", "")), str(data.get("confirm", "")))
        if err:
            return web.json_response({"ok": False, "message": err})
        log.info("status page: admin password created")
        token = auth.login(_client_ip(req), str(data.get("password", "")))
        resp = web.json_response({"ok": True})
        _set_cookie(req, resp, token)
        return resp

    async def api_login(req):
        data = await read_json(req)
        ip = _client_ip(req)
        wait = auth.locked(ip)
        if wait:
            return web.json_response({"ok": False, "message": f"Too many attempts. Try again in {wait // 60 + 1} min."})
        token = auth.login(ip, str(data.get("password", "")))
        if not token:
            log.warning("status page: failed login from %s", ip)
            return web.json_response({"ok": False, "message": "That password is not correct."})
        resp = web.json_response({"ok": True})
        _set_cookie(req, resp, token)
        return resp

    async def api_logout(req):
        await read_json(req)
        auth.logout(req.cookies.get(COOKIE))
        resp = web.json_response({"ok": True})
        resp.del_cookie(COOKIE, path="/")
        return resp

    async def api_password(req):
        if not logged_in(req):
            return deny()
        data = await read_json(req)
        err = auth.change_password(str(data.get("current", "")), str(data.get("password", "")), str(data.get("confirm", "")))
        if err:
            return web.json_response({"ok": False, "message": err})
        token = auth.login(_client_ip(req), str(data.get("password", "")))   # keep this browser signed in
        resp = web.json_response({"ok": True, "message": "Password changed. Other browsers have been signed out."})
        _set_cookie(req, resp, token)
        return resp

    async def api_settings(req):
        if not logged_in(req):
            return deny()
        data = await read_json(req)
        clean, errors = settings_mod.validate(data)
        if errors:
            return web.json_response({"ok": False, "message": "; ".join(errors)})
        changed = await a.apply_settings(clean)
        return web.json_response({"ok": True, "message": ("Saved: " + ", ".join(changed)) if changed else "No changes."})

    async def placement_get(req):
        if not logged_in(req):
            return deny()
        key = req.match_info["key"]
        row = a.state.get_done(key)
        try:
            current = await a.current_channels(key)
        except Exception as exc:
            return web.json_response({"error": f"NostalgiaTV not reachable ({type(exc).__name__})"}, status=502)
        label = row["label"] if row else next((i["label"] for i in a.library_cache[1] if i["key"] == key), key)
        opts = sorted(({"id": c, "name": n} for c, n in a.rules.names.items()), key=lambda o: _sort(o["name"]))
        return web.json_response({"label": label, "current": current, "options": opts})

    async def placement_post(req):
        if not logged_in(req):
            return deny()
        data = await read_json(req)
        chans = [str(c) for c in data.get("channels", []) if isinstance(c, str)]
        try:
            res = await a.edit_placement(req.match_info["key"], chans)
        except Exception as exc:
            log.exception("status page edit failed")
            return web.json_response({"ok": False, "message": f"Failed: {type(exc).__name__}. Nothing recorded."}, status=500)
        res["names"] = [a.rules.names.get(c, c) for c in res.get("channels", [])]
        return web.json_response(res)

    async def library_api(req):
        if not logged_in(req):
            return deny()
        try:
            items = await a.library_view()
        except Exception as exc:
            return web.json_response({"error": f"Could not read the library ({type(exc).__name__})"}, status=502)
        names = a.rules.names
        opts = sorted(({"id": c, "name": n} for c, n in names.items()), key=lambda o: _sort(o["name"]))
        return web.json_response({"items": [{**i, "names": [names.get(c, c) for c in i["channels"]]} for i in items],
                                  "libraries": sorted({i["library"] for i in items}), "options": opts})

    app = web.Application()
    app.add_routes([
        web.get("/api/status", api_status),
        web.post("/api/setup", api_setup), web.post("/api/login", api_login), web.post("/api/logout", api_logout),
        web.post("/api/password", api_password), web.post("/api/settings", api_settings),
        web.get("/api/placements/{key}", placement_get), web.post("/api/placements/{key}", placement_post),
        web.get("/api/library", library_api), web.get("/library", library_page),
        # any other path shows the main page, so a stale or mistyped address still works
        web.get("/", page), web.get("/{tail:.*}", page)])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("status page on port %d", port)
