import datetime as dt

from app.services import skills
from app.services.contacts import ContactRef, normalize_phone, spoken_email
from app.services.preferences import CustomSkill, IntegratedApp, Preferences, save_preferences
from app.voice.lexicon import build_lexicon
from app.windows.sysctl import SystemControl, SystemControlError

NOW = dt.datetime(2026, 10, 16, 9, 0)
PEOPLE = [ContactRef(1, "Mom", ["mother"], "15551230001", "mom@example.com"),
          ContactRef(2, "Alexandra Reed", ["alex"], "15551230002", "alexandra@example.com"),
          ContactRef(3, "Alexander Hill", [], "15551230003", "")]


def ctx(**kw):
    prefs = Preferences(integrated_apps=[IntegratedApp(name="WhatsApp", kind="uri", target="whatsapp:"),
                                         IntegratedApp(name="Notepad", kind="exe", target="C:\\Windows\\notepad.exe")], **kw)
    return skills.SkillContext(prefs, PEOPLE, NOW)


class FakeSys(SystemControl):
    def __init__(self, fg=("WhatsApp", "whatsapp.exe")):
        self.fg, self.log = fg, []

    def focus(self, needles):
        self.log.append(("focus", needles))
        return needles[0]

    def foreground(self):
        return self.fg

    def send_keys(self, combo):
        self.log.append(("keys", combo))

    def paste_text(self, text):
        self.log.append(("paste", text))


def test_whatsapp_message_variants():
    for phrase in ["message Mom on WhatsApp I'll be late", "send a message to Mom saying I'll be late",
                   "tell Mom I'll be late", "WhatsApp Mom I'll be late"]:
        p = skills.match(phrase, ctx())
        assert isinstance(p, skills.Plan), phrase
        assert p.steps[0].kind == "uri" and "phone=15551230001" in p.steps[0].value
        assert "late" in p.steps[0].value and not p.autosend and p.ask is None


def test_tell_me_a_joke_is_not_a_message():
    assert skills.match("tell me a joke", ctx()) is None


def test_autosend_and_ambiguity_and_fuzzy():
    p = skills.match("message Mom hello and send it", ctx())
    assert isinstance(p, skills.Plan) and p.autosend and "send" not in p.steps[0].value.lower().split("text=")[1]
    c = skills.match("message Alex hello", ctx())  # alias of Alexandra, but also a first name of Alexander? alias wins
    assert isinstance(c, skills.Plan) and "15551230002" in c.steps[0].value
    f = skills.match("message Alexandre Reed hi", ctx())
    assert isinstance(f, skills.Plan) and f.ask and "Alexandra" in f.ask


def test_unknown_person_and_missing_phone():
    r = skills.match("message Zed hello", ctx())
    assert r is None or isinstance(r, skills.Clarify)
    r = skills.match("message Alexander hello", ctx())
    assert isinstance(r, skills.Plan)
    assert isinstance(skills.plan_message(ctx(), "Mom", ""), skills.Clarify)
    assert isinstance(skills.plan_message(ctx(), "Mom", "hi", "telegram"), skills.Clarify)


def test_email_gmail_draft():
    p = skills.match("write an email to Mom about dinner on Friday", ctx())
    assert isinstance(p, skills.Plan) and p.draft and p.draft[1] == "mom@example.com"
    assert p.steps[0].value.startswith("https://mail.google.com/mail/?view=cm") and "to=mom@example.com" in p.steps[0].value
    assert p.send and p.send.keys == "ctrl+enter"
    p = skills.apply_draft(p, ctx(), "Dinner Friday", "Hi Mom,\nAre we on?")
    assert "su=Dinner%20Friday" in p.steps[0].value and p.draft is None
    m = skills.match("email alex at example dot com about the report", ctx())
    assert isinstance(m, skills.Plan) and "to=alex@example.com" in m.steps[0].value


def test_mailto_when_preferred():
    p = skills.match("write an email to Mom about dinner", ctx(email_app="mailto"))
    assert p.steps[0].kind == "uri" and p.steps[0].value.startswith("mailto:mom@example.com") and p.send is None


def test_calendar_maps_youtube_docs():
    p = skills.match("schedule a meeting with Sam tomorrow at 3pm", ctx())
    assert isinstance(p, skills.Plan) and "20261017T150000" in p.steps[0].value
    assert "destination=the+airport" in skills.match("directions to the airport", ctx()).steps[0].value
    assert "search_query=lo-fi" in skills.match("play lo-fi beats on YouTube", ctx()).steps[0].value.replace("+", "-", 0)
    assert skills.match("create a new google doc", ctx()).steps[0].value == "https://docs.new"
    assert skills.match("open document", ctx()) is None


def test_custom_skill_and_disabled():
    c = ctx(custom_skills=[CustomSkill(name="Amazon", phrase="search amazon for {q}", url="https://www.amazon.com/s?k={q}")])
    p = skills.match("search amazon for usb cables", c)
    assert p.steps[0].value.endswith("k=usb+cables")
    c2 = ctx(disabled_skills=["message"])
    assert skills.match("message Mom hi", c2) is None


def test_type_needs_a_named_app():
    assert skills.match("type hints are useful in python", ctx()) is None
    assert skills.match("type hello", ctx()) is None


def test_type_skill_guards_terminals():
    p = skills.match("type hello world in Notepad", ctx())
    assert isinstance(p, skills.Plan) and [s.kind for s in p.steps][-1] == "paste"
    sysc = FakeSys(("Terminal", "windowsterminal.exe"))
    r = skills.Runner(lambda *_: None, sysc, sleep=lambda _s: None)
    try:
        r.run_steps(skills.plan_type(ctx(), "rm -rf /"), ctx().prefs)
    except SystemControlError:
        pass
    else:
        raise AssertionError("typed into a terminal")
    assert ("paste", "rm -rf /") not in sysc.log


def test_send_requires_confirmation_and_right_window():
    store = skills.PendingStore(clock=lambda: 0.0)
    launched = []
    sysc = FakeSys()
    runner = skills.Runner(lambda t, k: launched.append((k, t)), sysc, sleep=lambda _s: None)
    plan = skills.match("message Mom hi", ctx())
    status, msg = skills.execute_plan(plan, runner, ctx().prefs, store)
    assert status == "succeeded" and launched and ("keys", "enter") not in sysc.log and store.get().kind == "send"
    ok, _ = runner.press_send(store.get().send, ctx().prefs)
    assert ok and ("keys", "enter") in sysc.log
    wrong = FakeSys(("Some Document", "word.exe"))
    wrong.focus = lambda needles: None  # type: ignore[method-assign]
    ok, msg = skills.Runner(lambda *_: None, wrong, sleep=lambda _s: None).press_send(store.get().send, ctx().prefs)
    assert not ok and "haven't sent" in msg


def test_pending_expiry_and_verdicts():
    t = [0.0]
    store = skills.PendingStore(ttl_s=10, clock=lambda: t[0])
    store.set(skills.Pending("send", "x", "y"))
    assert store.get() is not None
    t[0] = 11
    assert store.get() is None
    assert skills.verdict("Yes please") == "yes" and skills.verdict("send it.") == "yes"
    assert skills.verdict("no") == "no" and skills.verdict("cancel that") == "no" and skills.verdict("tell Mom hi") is None


def test_contacts_helpers():
    assert normalize_phone("+1 (555) 123-0001") == "15551230001" and normalize_phone("12") is None
    assert spoken_email("alex at example dot com") == "alex@example.com" and spoken_email("hello there") is None


def test_lexicon_snaps_apps_not_names():
    lex = build_lexicon(["Spotify"], ["Alexandra"])
    assert lex.snap("open spotifi please") == "open Spotify please"
    assert lex.snap("message what's app") == "message WhatsApp"
    assert lex.snap("hi Alexander") == "hi Alexander"
    assert "Alexandra" in lex.prompt()


# ---------------------------------------------------------------- end to end through the API
import json  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from tests.test_api_chat import new_conv, send  # noqa: E402


def text_of(events):
    return "".join(d.get("text", "") for n, d in events if n == "token")


@pytest.fixture
def wired(client: TestClient, launched: list):
    state = client.app.state.nexus
    sysc = FakeSys()
    a = state.conversations.assistant
    a.system = sysc
    a.runner = skills.Runner(lambda t, k: launched.append((t, k)), sysc, sleep=lambda _s: None)
    mom = {"name": "Mom", "aliases": ["mother"], "phone": "+1 555 123 0001", "email": "mom@example.com"}
    alex = {"name": "Alexandra Reed", "aliases": [], "phone": "+1 555 123 0002", "email": "al@example.com"}
    client.post("/api/contacts", json=mom)
    client.post("/api/contacts", json=alex)
    with state.db.session() as db:
        save_preferences(db, {"integrated_apps": [IntegratedApp(name="WhatsApp", kind="uri", target="whatsapp:").model_dump()]})
    return state, sysc


def test_contacts_api_validates(client: TestClient):
    r = client.post("/api/contacts", json={"name": "Bob", "phone": "12"})
    assert r.status_code == 422
    r = client.post("/api/contacts", json={"name": "Bob"})
    assert r.status_code == 422
    ok = client.post("/api/contacts", json={"name": "Bob", "phone": "+44 7700 900123", "aliases": ["bobby", "bobby"]})
    assert ok.status_code == 201 and ok.json()["phone"] == "447700900123" and ok.json()["aliases"] == ["bobby"]
    assert client.post("/api/contacts", json={"name": "Bob", "email": "b@x.io"}).status_code == 409
    cid = ok.json()["id"]
    assert client.put(f"/api/contacts/{cid}", json={"name": "Bob B", "email": "bob@x.io"}).json()["name"] == "Bob B"
    assert client.delete(f"/api/contacts/{cid}").json() == {"ok": True}
    assert client.get("/api/contacts").json() == []


def test_message_then_confirm_by_voice_phrase(client, wired, launched):
    _state, sysc = wired
    cid = new_conv(client)
    out = text_of(send(client, cid, "message Mom I'll be late"))
    assert "Mom" in out and "send it" in out.lower()
    assert launched and "15551230001" in str(launched[-1][0]) and ("keys", "enter") not in sysc.log
    assert client.get("/api/pending").json()["pending"]["kind"] == "send"
    out = text_of(send(client, cid, "send it"))
    assert "Pressed Send" in out and ("keys", "enter") in sysc.log
    assert client.get("/api/pending").json()["pending"] is None


def test_message_cancel_and_button_confirm(client, wired):
    _state, sysc = wired
    cid = new_conv(client)
    send(client, cid, "text Mom hello")
    assert "cancelled" in text_of(send(client, cid, "cancel")).lower() and ("keys", "enter") not in sysc.log
    send(client, cid, "text Mom hello again")
    r = client.post("/api/pending/confirm").json()
    assert "Pressed Send" in r["message"] and r["pending"] is None
    assert client.post("/api/pending/confirm").json()["status"] == "unsupported"


def test_autosend_in_one_sentence(client, wired):
    _state, sysc = wired
    cid = new_conv(client)
    out = text_of(send(client, cid, "message Mom on the way and send it"))
    assert "Pressed Send" in out and ("keys", "enter") in sysc.log


def test_unknown_confidence_asks_first_for_voice(client, wired, launched):
    state, sysc = wired
    cid = new_conv(client)
    conv = state.conversations
    import asyncio

    from app.services.conversation import TurnRequest

    async def go(text, conf):
        return [e async for e in conv.respond(TurnRequest(conversation_id=cid, content=text, source="voice", confidence=conf))]

    ev = asyncio.run(go("message Mom see you soon", 0.2))
    msg = "".join(e.get("text", "") for e in ev if e["event"] == "token")
    assert "I heard" in msg and not launched
    ev = asyncio.run(go("yes", 0.9))
    assert launched and "15551230001" in str(launched[-1][0])
    # a clear hearing goes straight ahead
    launched.clear()
    ev = asyncio.run(go("message Mom see you", 0.9))
    assert launched


def test_fuzzy_name_requires_yes(client, wired, launched):
    cid = new_conv(client)
    out = text_of(send(client, cid, "message Alexandre Reed hello"))
    assert "Did you mean Alexandra Reed" in out and not launched
    out = text_of(send(client, cid, "yes"))
    assert launched and "15551230002" in str(launched[-1][0])


def test_unknown_person_does_not_chat_or_send(client, wired, launched):
    cid = new_conv(client)
    out = text_of(send(client, cid, "message Zebedee are you there"))
    assert not launched and out


def test_email_flow_with_model_drafting(client, wired, launched, provider):
    provider.reply = json.dumps({"subject": "Dinner Friday", "body": "Hi Mom,\nAre we still on for dinner on Friday?\nBest,"})
    cid = new_conv(client)
    out = text_of(send(client, cid, "write an email to Mom asking about dinner on Friday"))
    assert "draft" in out.lower()
    url = launched[-1][0]
    assert url.startswith("https://mail.google.com/mail/?view=cm") and "Dinner%20Friday" in url and "mom@example.com" in url
    assert client.get("/api/pending").json()["pending"]["kind"] == "send"


def test_email_draft_survives_model_failure(client, wired, launched, provider):
    provider.reply = "not json at all"
    cid = new_conv(client)
    out = text_of(send(client, cid, "write an email to Mom about dinner"))
    assert "empty" in out.lower() and launched


def test_router_understands_freeform(client, wired, launched, provider):
    provider.reply = json.dumps({"tool": "message", "args": {"person": "Mom", "text": "Dinner is at seven"}})
    cid = new_conv(client)
    out = text_of(send(client, cid, "can you ping my mom that dinner is at seven"))
    assert launched and "Dinner%20is%20at%20seven" in str(launched[-1][0]) and "Mom" in out


def test_router_ignores_questions_and_bad_output(client, wired, launched, provider):
    cid = new_conv(client)
    provider.reply = json.dumps({"tool": "rm_rf", "args": {}})
    text_of(send(client, cid, "please tell the story of the three little pigs"))
    assert not launched
    provider.reply = json.dumps({"tool": "message", "args": {"person": "Nobody", "text": "x"}})
    text_of(send(client, cid, "ping nobody that dinner is at seven"))
    assert not launched


def test_skills_api(client):
    d = client.get("/api/skills").json()
    assert {s["id"] for s in d["skills"]} >= {"message", "email", "calendar"} and all(s["enabled"] for s in d["skills"])
    d = client.put("/api/skills/message", json={"enabled": False}).json()
    assert [s["enabled"] for s in d["skills"] if s["id"] == "message"] == [False]
    assert client.put("/api/skills/nope", json={"enabled": True}).status_code == 404
    bad = client.post("/api/skills/custom", json={"name": "X", "phrase": "find x", "url": "https://x.com/?q={q}"})
    assert bad.status_code == 422
    insecure = {"name": "X", "phrase": "find {q}", "url": "http://x.com/?q={q}"}
    assert client.post("/api/skills/custom", json=insecure).status_code == 422
    d = client.post("/api/skills/custom", json={"name": "Amazon", "phrase": "search amazon for {q}",
                                                "url": "https://www.amazon.com/s?k={q}"}).json()
    assert d["custom"][0]["name"] == "Amazon"
    assert client.delete("/api/skills/custom/Amazon").json()["custom"] == []


def test_calendar_and_docs_through_chat(client, launched):
    cid = new_conv(client)
    out = text_of(send(client, cid, "schedule a meeting with Sam tomorrow at 3pm"))
    assert "calendar.google.com" in str(launched[-1][0]) and "Calendar" in out
    send(client, cid, "create a new spreadsheet")
    assert launched[-1] == ("https://sheets.new", "url")


def test_decode_options_match_the_real_whisper_signature():
    import inspect

    fw = pytest.importorskip("faster_whisper")
    from app.voice.stt import decode_options

    sig = inspect.signature(fw.WhisperModel.transcribe)
    for mode in ("final", "wake"):
        sig.bind(None, None, **decode_options(mode, "hey nexus", "Names and apps: Spotify, WhatsApp."))
