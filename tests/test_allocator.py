"""End-to-end checks against fake Plex, NostalgiaTV and Discord. Run: python tests/test_allocator.py"""
import asyncio, os, sys, tempfile, time
ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
tmp = tempfile.mkdtemp()
os.environ.update(PLEX_URL="http://x", PLEX_TOKEN="t", DISCORD_TOKEN="d", DISCORD_CHANNEL_ID="1",
                  RULES_PATH=ROOT + "/rules.yaml", STATE_PATH=tmp + "/s.db", DRY_RUN="false")
from app import main as M
from app.rules import Title

PASS = []
def check(name, cond):
    PASS.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name)

T = {
 "1": Title("1","movie","X-Men",2000,"Movies","20th Century Fox","PG-13",["Action","Adventure","Science Fiction"],""),
 "2": Title("2","show","The Office (US)",2005,"TV Shows","Deedle-Dee","TV-14",["Comedy"],""),
 "3": Title("3","show","Old Show",2001,"TV Shows","HBO","TV-MA",["Comedy"],""),
 "4": Title("4","show","Boom",2027,"TV Shows","x","TV-MA",["Drama"],""),      # lookup will raise
 "5": Title("5","movie","Refused Film",2027,"Movies","x","PG-13",["Comedy"],""),
}

class Plex:
    machine_id = "MID"
    def __init__(self, keys): self.keys = keys
    async def init(self): pass
    async def recently_added(self, d, ign): return self.keys
    async def title(self, k): return T.get(k)
    def qualified(self, k): return f"MID:{k}"
    async def exists(self, k): return k not in getattr(self, "gone", set())

class NTV:
    def __init__(self): self.calls = []; self.unassigned = []; self.fail_add = False
    async def init(self): pass
    async def unassigned_keys(self): return self.unassigned
    async def config_homes(self, nah):
        manual = {"app_nb_sea","app_comedy_middle","app_ef_x","app_genre_action","app_genre_superhero","app_lackluster","app_hb_yo_min"}
        return {"3": {"app_hb_yo_min"}}, manual, {"app_am_sea","app_watch_on_repeat"}
    async def channels_containing(self, q):
        if q == "MID:4": raise RuntimeError("boom")
        if q == "MID:1": return ["app_genre_action","app_am_sea","custom_newarrivals"]
        if q == "MID:2": return ["app_nb_sea","app_comedy_middle","app_watch_on_repeat"]
        return ["custom_newarrivals"]
    async def add_to_channel(self, ch, keys):
        if self.fail_add: raise RuntimeError("ntv down")
        self.calls.append(("add", ch, tuple(keys)))
        refused = [{"ratingKey": k, "reason": "not allowed"} for k in keys if k == "MID:5"]
        return {"ratingKeys": [k for k in keys if k != "MID:5"], "refused": refused}
    async def backup(self): self.calls.append(("backup",))
    async def refresh_channel(self, ch): self.calls.append(("refresh", ch))

class Chan:
    def __init__(self): self.sent = []; self.n = 100
    async def send(self, content=None, embed=None, view=None):
        self.sent.append(dict(content=content, embed=embed, view=view))
        class Msg: pass
        m = Msg(); self.n += 1; m.id = self.n; return m

class Resp:
    def __init__(self, it): self.it = it
    async def defer(self): self.it.log.append("defer")
    async def send_message(self, text, ephemeral=False): self.it.log.append(("msg", text))
    async def edit_message(self, **kw): self.it.log.append(("edit", kw.get("content")))
class Follow:
    def __init__(self, it): self.it = it
    async def send(self, text, ephemeral=False): self.it.log.append(("followup", text))
class MsgObj:
    def __init__(self, it): self.it = it
    async def edit(self, **kw): self.it.log.append(("edit", kw.get("content")))
class User: id = 42; name = "dustin"
class Inter:
    def __init__(self, cid, values=None):
        self.data = {"custom_id": cid, **({"values": values} if values else {})}
        self.user = User(); self.log = []
        self.response = Resp(self); self.followup = Follow(self); self.message = MsgObj(self)

def make(keys):
    a = M.Allocator.__new__(M.Allocator)
    a.rules = M.Rules(os.environ["RULES_PATH"]); a.state = M.State(os.environ["STATE_PATH"])
    a.plex, a.ntv, a.lock, a.titles, a.missing_since = Plex(keys), NTV(), asyncio.Lock(), {}, {}
    ch = Chan()
    async def _channel(): return ch
    a._channel = _channel
    return a, ch

async def run():
    a, ch = make(["1","2","3","4","5"])
    await a.scan()
    adds = [c for c in a.ntv.calls if c[0] == "add"]
    check("X-Men placed automatically on Superhero, Action, Lackluster",
          {c[1] for c in adds if "MID:1" in c[2]} == {"app_genre_superhero","app_genre_action","app_lackluster"})
    check("hidden/manual lookup matches did not count as a home for X-Men", a.state.is_known("1"))
    p2 = a.state.get_pending("2")
    check("The Office asked in Discord with NostalgiaTV hint first: Comedy Netwerk, NB Sea, EF X",
          p2 and p2["suggested"] == ["app_comedy_middle","app_nb_sea","app_ef_x"])
    check("existing title (HBO) recognised from saved config, no action", ("add","app_hb_yo_min",("MID:3",)) not in adds)
    check("failing title 4 did not stop the scan and stays retryable", not a.state.is_known("4"))
    check("refused film reported in Discord", any(m["content"] and "refused" in m["content"] and "Refused Film" in m["content"] for m in ch.sent))
    check("refused-everywhere film not recorded as placed (skipped)", a.state.db.execute("SELECT 1 FROM skipped WHERE key='5'").fetchone() is not None)
    q = [m for m in ch.sent if m["view"] is not None]
    check("one question posted (The Office)", len(q) == 1 and q[0]["embed"].title == "The Office (US) (2005)")
    sel = q[0]["view"].children[0]
    defaults = [o.value for o in sel.options if o.default]
    check("picker pre-selects the three suggestions and lists them first",
          defaults == ["app_comedy_middle","app_nb_sea","app_ef_x"] and [o.value for o in sel.options[:3]] == defaults)
    check("picker max is 3 and has <= 25 options", sel.max_values == 3 and len(sel.options) <= 25)

    # unassigned report must not delete or re-post an open question
    n = len(ch.sent); a.ntv.unassigned = ["2"]; await a.scan()
    check("open question survives an 'unassigned' report and is not re-posted",
          a.state.get_pending("2") is not None and len(ch.sent) == n)

    # recently skipped title is not re-asked; old skip is
    a.state.mark_skipped("9", "Nine"); T["9"] = Title("9","show","Nine",2027,"TV Shows","x","TV-MA",["Drama"],"")
    a.ntv.unassigned = ["9"]; await a.scan()
    check("recently skipped title is not re-asked", a.state.get_pending("9") is None)
    a.state.db.execute("UPDATE skipped SET at=? WHERE key='9'", (time.time() - 31 * 86400,)); a.state.db.commit()
    await a.scan()
    check("title skipped over 30 days ago is asked again", a.state.get_pending("9") is not None)

    # approve flow: change selection, approve
    a.ntv.calls.clear()
    i = Inter("ntva:sel:2", ["app_nb_sea","app_tee_bs"]); await a.on_interaction(i)
    check("selection change stored", a.state.get_pending("2")["selected"] == ["app_nb_sea","app_tee_bs"])
    i = Inter("ntva:ok:2"); await a.on_interaction(i)
    adds = {c[1] for c in a.ntv.calls if c[0] == "add"}
    check("approve applies the changed selection only", adds == {"app_nb_sea","app_tee_bs"})
    check("approve edits the post to 'added to NB Sea, Tee BS'", any(isinstance(x, tuple) and x[0]=="edit" and "NB Sea, Tee BS" in x[1] for x in i.log))
    i = Inter("ntva:ok:2"); await a.on_interaction(i)
    check("second press says already handled", ("msg", "This title has already been handled.") in i.log)

    # approve failure: error shown, question stays open
    i = Inter("ntva:ok:9"); await a.on_interaction(i)
    check("approve with nothing selected asks for a channel", ("msg","Pick at least one channel first.") in i.log)
    await a.on_interaction(Inter("ntva:sel:9", ["app_ef_x"]))
    a.ntv.fail_add = True
    i = Inter("ntva:ok:9"); await a.on_interaction(i)
    check("failed approve reports an error and keeps the question", any(isinstance(x, tuple) and x[0]=="followup" for x in i.log) and a.state.get_pending("9") is not None)
    a.ntv.fail_add = False
    i = Inter("ntva:skip:9"); await a.on_interaction(i)
    check("Skip for now closes the question", a.state.get_pending("9") is None)

    # approving a question for a title deleted from Plex in the meantime adds nothing
    a.state.add_pending("77", "Gone Show (2027)", ["app_ef_x"]); a.plex.gone = {"77"}; a.ntv.calls.clear()
    i = Inter("ntva:ok:77"); await a.on_interaction(i)
    check("approve on a title deleted since the question adds nothing and closes it",
          not [c for c in a.ntv.calls if c[0] == "add"] and a.state.get_pending("77") is None
          and any(isinstance(x, tuple) and x[0] == "edit" and "no longer in Plex" in (x[1] or "") for x in i.log))
    a.plex.gone = set()

    # restart: nothing repeats
    n = len(ch.sent); a.ntv.calls.clear(); a.ntv.unassigned = []
    a2, ch2 = make(["1","2","3","5"]); a2.state = a.state; await a2.scan()
    check("after restart nothing is re-placed or re-posted", not [c for c in a2.ntv.calls if c[0]=="add"] and not ch2.sent)

    # dry run: decisions flagged, cleared and applied when going live
    os.environ["STATE_PATH"] = tmp + "/d.db"; M.CFG["dry_run"] = True; M.CFG["state"] = tmp + "/d.db"
    d, chd = make(["1"]); await d.scan()
    check("dry run changes nothing in NostalgiaTV", not [c for c in d.ntv.calls if c[0] in ("add","backup")])
    M.CFG["dry_run"] = False
    cleared = d.state.clear_dry_run(); await d.scan()
    check("going live clears dry-run decisions and applies them", cleared == 1 and [c for c in d.ntv.calls if c[0]=="add"])

async def run_cleanup():
    M.CFG["dry_run"] = False
    os.environ["STATE_PATH"] = tmp + "/c.db"; M.CFG["state"] = tmp + "/c.db"
    a, ch = make([])
    a.state = M.State(tmp + "/c.db")
    lists = {"app_genre_action": ["MID:1", "MID:7", "MID:8"], "app_genre_superhero": ["MID:1"],
             "app_ntv": ["local:abc:ff01"], "app_ef_x": ["OTHER:1", "MID:2", "MID:20"]}
    plex = {"1": "X-Men (2000)", "7": "Seven (1995)", "8": "Eight (2001)", "2": "The Office (US) (2005)",
            "20": "Twenty (2020)"}
    plex_404 = set()
    removed = []
    a.ntv.manual_lists = lambda: _ret({c: list(v) for c, v in lists.items()})
    async def rm(c, k):
        removed.append((c, k)); lists[c].remove(k)
    a.ntv.remove_from_channel = rm
    a.ntv.labels = lambda keys: _ret({"MID:99": "Ninety Nine (1999)"})
    a.plex.library_index = lambda: _ret(dict(plex))
    async def exists(k): return k not in plex_404
    a.plex.exists = exists
    a.plex.machine_id = "MID"
    edits = {}
    async def fetch_message(mid):
        class Msg:
            async def edit(self, **kw): edits[mid] = kw.get("content")
        return Msg()
    ch.fetch_message = fetch_message
    def delete(k): plex.pop(k, None); plex_404.add(k)
    def restore(k, lbl): plex[k] = lbl; plex_404.discard(k)
    questions = lambda: [m for m in ch.sent if m["view"] is not None]

    await a.cleanup()                       # all present: names remembered, nothing asked
    check("clean-up: nothing asked while everything is in Plex", not ch.sent)
    delete("1"); delete("20")               # a movie on two channels, and a show
    await a.cleanup()
    check("clean-up: nothing asked on the first sighting", not ch.sent)
    await a.cleanup()
    q = questions()
    check("clean-up: one question per title (2 titles, 3 channel entries -> 2 questions)", len(q) == 2)
    xq = [m for m in q if "X-Men (2000)" in m["content"]]
    check("clean-up: question names the title and every channel it is on",
          xq and "Action" in xq[0]["content"] and "Superhero" in xq[0]["content"])
    check("clean-up: nothing removed before an answer", not removed)
    check("clean-up: NTV local keys and other-server keys never asked about",
          not any("local:" in m["content"] or "OTHER" in m["content"] for m in ch.sent))
    await a.cleanup()
    check("clean-up: open questions are not re-posted", len(questions()) == 2)

    a.ntv.calls.clear()
    i = Inter("ntva:rm:1"); await a.on_interaction(i)
    check("clean-up: Remove takes the title off every channel",
          sorted(removed) == [("app_genre_action", "MID:1"), ("app_genre_superhero", "MID:1")])
    check("clean-up: Remove takes a backup and refreshes the touched channels",
          ("backup",) in a.ntv.calls and {c[1] for c in a.ntv.calls if c[0] == "refresh"} == {"app_genre_action", "app_genre_superhero"})
    check("clean-up: Remove edits the post", any(isinstance(x, tuple) and x[0] == "edit" and "removed from" in (x[1] or "") for x in i.log))
    i = Inter("ntva:rm:1"); await a.on_interaction(i)
    check("clean-up: second press says already handled", ("msg", "This title has already been handled.") in i.log)

    removed.clear()
    i = Inter("ntva:keep:20"); await a.on_interaction(i)
    check("clean-up: Keep removes nothing", not removed and a.state.get_removal("20")["status"] == "kept")
    n = len(ch.sent); await a.cleanup(); await a.cleanup()
    check("clean-up: a kept title is never asked about again", len(ch.sent) == n)

    # unknown name falls back to NostalgiaTV's content cache
    lists["app_tcn"] = ["MID:99"]; plex_404.add("99")
    await a.cleanup(); await a.cleanup()
    check("clean-up: name from NostalgiaTV when Plex no longer knows it",
          any("Ninety Nine (1999)" in (m["content"] or "") for m in questions()))

    # title comes back while its question is open
    delete("8"); await a.cleanup(); await a.cleanup()
    q8 = a.state.get_removal("8")
    restore("8", "Eight (2001)"); await a.cleanup()
    check("clean-up: a title back in Plex closes its open question",
          a.state.get_removal("8") is None and "back in Plex" in (edits.get(int(q8["message_id"])) or ""))

    # Remove pressed after the title came back (before the next pass noticed)
    delete("7"); await a.cleanup(); await a.cleanup()
    plex_404.discard("7"); removed.clear()
    i = Inter("ntva:rm:7"); await a.on_interaction(i)
    check("clean-up: Remove on a title that is back in Plex removes nothing", not removed)
    plex["7"] = "Seven (1995)"

    # Plex error during the check: nothing asked
    async def boom(k): raise RuntimeError("plex down")
    a.plex.exists = boom; plex.pop("2")
    n = len(ch.sent)
    try:
        await a.cleanup(); raised = False
    except RuntimeError:
        raised = True
    check("clean-up: a Plex error aborts the pass without asking anything", raised and len(ch.sent) == n)
    a.plex.exists = exists; plex["2"] = "The Office (US) (2005)"

    # mass disappearance: one warning, no questions, not repeated
    lists["app_genre_comedy"] = [f"MID:{k}" for k in range(1000, 1100)]
    plex_404.update(str(k) for k in range(1000, 1100))
    n = len(ch.sent); await a.cleanup(); await a.cleanup(); await a.cleanup()
    new = ch.sent[n:]
    check("clean-up: over CLEANUP_MAX_REMOVALS posts one warning and no questions",
          len(new) == 1 and new[0]["view"] is None and "paused" in new[0]["content"])
    lists.pop("app_genre_comedy")

    # Remove after you took it off one channel yourself: only the remaining channel is touched
    lists["app_genre_family"] = ["MID:66"]; lists["app_tcn"] = ["MID:66"]; plex_404.add("66")
    await a.cleanup(); await a.cleanup()
    lists["app_tcn"].remove("MID:66"); removed.clear()
    i = Inter("ntva:rm:66"); await a.on_interaction(i)
    check("clean-up: Remove skips channels it was already taken off and still closes the question",
          removed == [("app_genre_family", "MID:66")] and a.state.get_removal("66")["status"] == "removed")

    # dry run posts nothing
    lists["app_lackluster"] = ["MID:55"]; plex_404.add("55")
    M.CFG["dry_run"] = True; n = len(ch.sent)
    await a.cleanup(); await a.cleanup()
    check("clean-up: dry run asks nothing", len(ch.sent) == n)
    M.CFG["dry_run"] = False

def run_profile():
    from app.clients import choose_profile
    check("profile: the only real profile is chosen (legacy blank entry ignored)",
          choose_profile([{"id": "", "name": "Default"}, {"id": "ac80", "name": "Default"}]) == "ac80")
    check("profile: wrapped list and active flag understood",
          choose_profile({"profiles": [{"id": "a"}, {"id": "b", "active": True}]}) == "b")
    try:
        choose_profile([{"id": "a"}, {"id": "b"}]); ok = False
    except RuntimeError as e:
        ok = "NTV_PROFILE_ID" in str(e)
    check("profile: several profiles and none active stops with a clear error", ok)
    st = M.State(tmp + "/p.db")
    st.mark_done("1", "Placed On Legacy", ["app_genre_action"], "auto")
    check("profile: first run with a profile clears placements made without one",
          st.switch_profile("ac80") == 1 and not st.is_known("1"))
    st.mark_done("2", "Placed Properly", ["app_genre_action"], "auto")
    check("profile: same profile on restart keeps placements", st.switch_profile("ac80") == 0 and st.is_known("2"))

async def _ret(v): return v

asyncio.run(run())
asyncio.run(run_cleanup())
run_profile()
print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
sys.exit(0 if all(PASS) else 1)
