"""The AI's image description lands on the media row it describes.

Offline, temporary SQLite database. Run: python tests/test_media_description_target.py
"""

import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_tmpdir = tempfile.mkdtemp(prefix="whatsbot_desc_")
from db import init_db  # noqa: E402

init_db(Path(_tmpdir) / "whatsbot.db")

from db.repositories import contact_repo, message_repo  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  OK {name}")
    else:
        failed += 1
        print(f"  FAIL {name}" + (f" -- {detail}" if detail else ""))


cid = contact_repo.get_or_create("5511999990200")["id"]
now = time.time()
img_a = message_repo.add(cid, "user", "", media_type="image", media_path="m/a.jpg", msg_id="IMG_A", ts=now)
img_b = message_repo.add(cid, "user", "", media_type="image", media_path="m/b.jpg", msg_id="IMG_B", ts=now + 1)
# A text saved later in the same cycle is the newest user row.
txt = message_repo.add(cid, "user", "e aí?", msg_id="TXT", ts=now + 2)

print("\nlookup by msg_id")
found = message_repo.get_user_message_by_msg_id(cid, "IMG_A")
check("finds the exact media row", found is not None and found["_id"] == img_a["id"])
check("unknown msg_id -> None", message_repo.get_user_message_by_msg_id(cid, "NOPE") is None)
check("empty msg_id -> None", message_repo.get_user_message_by_msg_id(cid, "") is None)
other = contact_repo.get_or_create("5511999990201")["id"]
check("never crosses contacts", message_repo.get_user_message_by_msg_id(other, "IMG_A") is None)

print("\nhandler.update_last_user_message_content")
from agent.handler import AgentHandler  # noqa: E402

h = AgentHandler.__new__(AgentHandler)


class _C:
    id = cid


h._get_contact = lambda phone: _C()

h.update_last_user_message_content("x", "[Descrição da imagem]: foto A", "IMG_A")
rows = {m["msg_id"]: m["content"] for m in message_repo.get_all(cid)}
check("description goes to IMG_A", rows["IMG_A"] == "[Descrição da imagem]: foto A", str(rows))
check("newest text row untouched", rows["TXT"] == "e aí?", str(rows))
check("other image untouched", rows["IMG_B"] == "", str(rows))

h.update_last_user_message_content("x", "legacy", None)
rows = {m["msg_id"]: m["content"] for m in message_repo.get_all(cid)}
check("no msg_id keeps the old fallback (newest user row)", rows["TXT"] == "legacy", str(rows))

h.update_last_user_message_content("x", "fb", "UNKNOWN_ID")
rows = {m["msg_id"]: m["content"] for m in message_repo.get_all(cid)}
check("unknown msg_id falls back too", rows["TXT"] == "fb", str(rows))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
