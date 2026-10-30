"""Synthetic member fixture: render real outbound payloads per channel. No network."""
import asyncio, json, sqlite3, sys, tempfile, re
from pathlib import Path
from types import SimpleNamespace

REL = sys.argv[1]
sys.path.insert(0, REL)
from gateway import kanban_watchers as kw
from gateway import kanban_delivery_receipts as rc
from gateway import member_document as md
from hermes_cli import kanban_db as kb

OUT = Path(sys.argv[2]); OUT.mkdir(parents=True, exist_ok=True)
PLUMB = re.compile(r"\bt_[0-9a-f]{6,}|kanban|\[[\w-]+-home\]|@\w+|ready for review|blocked|worker|review", re.I)
MD = b"# Follow-up research\n\n## Summary\nSynthetic **fixture** brief.\n\n- Point one\n- Point two with `code`\n\n1. Step\n2. Step\n"
TITLE = "Research synthetic creator's follow-up offer"

# 1) notifier gating: members only see 'completed'
fails = []
for plat in ("photon", "api_server", "telegram"):
    sub = {"platform": plat}
    print(plat, "member_chat=", kw.is_member_chat(sub))
if not (kw.is_member_chat({"platform": "photon"}) and kw.is_member_chat({"platform": "api_server"}) and not kw.is_member_chat({"platform": "telegram"})):
    fails.append("gating")

# 2) human text
for t in (TITLE, "[josh-home] @grace Kanban t_3efcecc6 Research X", ""):
    for f in (True, False):
        s = kw.member_completion_text(t, f); print(repr(s))
        if PLUMB.search(s): fails.append("plumbing in text: " + s)

# 3) real deliver() with fake Photon adapter
class Photon:
    platform = SimpleNamespace(value="photon"); _http_client = object(); sent = []
    async def send(self, chat_id, text, metadata=None):
        self.sent.append(("text", text)); return SimpleNamespace(success=True, message_id="m%d" % len(self.sent))
    async def send_document(self, chat_id, file_path, metadata=None):
        data = Path(file_path).read_bytes(); (OUT / Path(file_path).name).write_bytes(data)
        self.sent.append(("document", Path(file_path).name, data[:5])); return SimpleNamespace(success=True, message_id="m%d" % len(self.sent))

c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
sub = {"task_id": "t_deadbeef", "platform": "photon", "chat_id": "synthetic-member", "thread_id": "", "notifier_profile": "default", "delivery_mode": "notify", "delivery_metadata": {}}
art = {"id": 1, "name": "follow-up-research.md", "bytes": MD, "sha256": rc.sha(MD)}
import gateway.owner_delegation as od, gateway.secondary_documents as sd
od.verify_return = lambda *a, **k: None
sd.current = lambda *a, **k: None
rc.reviewed_artifacts = lambda *a, **k: [art]
kb.list_notify_subs = lambda *a, **k: [sub]
kb.get_task = lambda *a, **k: SimpleNamespace(title=TITLE, status="done", current_run_id=None)
class Txn:
    def __init__(s, c): pass
    def __enter__(s): return s
    def __exit__(s, *a): return False
kb.write_txn = lambda c: Txn(c)
ad = Photon()
raw = "✔ [josh-home] @grace Kanban t_deadbeef done — Research...\nRaw worker summary"
asyncio.run(rc.deliver(c, sub, 405, raw, ad, {}))
asyncio.run(rc.deliver(c, sub, 405, raw, ad, {}))  # replay -> must be no-op
print(json.dumps([list(map(str, s)) for s in ad.sent], indent=1))
texts = [s for s in ad.sent if s[0] == "text"]; docs = [s for s in ad.sent if s[0] == "document"]
if len(texts) != 1 or len(docs) != 1: fails.append("dup/count %s" % ad.sent)
if PLUMB.search(texts[0][1]): fails.append("plumbing outbound")
if not docs[0][1].endswith(".pdf") or docs[0][2] != b"%PDF-": fails.append("not pdf")
print("FAILS:", fails)
sys.exit(1 if fails else 0)
