"""Offline tests for server/file_manager.py and server/media_policy.py.

No server, no database: only the filesystem logic that decides what the user
can see and delete. Run: python tests/test_file_manager.py
"""

import base64
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from server import file_manager as fm
from server import media_policy as mp

passed = 0
failed = 0

def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK {name}")
    else:
        failed += 1
        print(f"  FAIL {name}" + (f" -- {detail}" if detail else ""))

def section(title: str):
    print(f"\n{'-'*60}\n  {title}\n{'-'*60}")

def forge(root: str, rel: str) -> str:
    return base64.urlsafe_b64encode(f"{root}:{rel}".encode()).decode().rstrip("=")

def write(path: Path, size: int = 10):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)

def refused(manager, file_id) -> bool:
    try:
        manager.resolve(file_id)
    except fm.FileAccessError:
        return True
    return False

tmp = Path(tempfile.mkdtemp(prefix="whatsbot_fm_"))
data = tmp / "app"
media = data / "statics" / "media"
operator = data / "statics" / "operator"
storages = data / "storages"
write(media / "5511999990001" / "2026-10-01" / "a.jpg", 1000)
write(media / "5511999990001" / "2026-10-02" / "b.ogg", 300)
write(media / "5511999990002" / "2026-10-02" / "c.mp4", 5000)
write(media / "5511999990002" / "2026-10-03" / "d.pdf", 200)
write(operator / "1.png", 700)
# things that must NEVER be reachable
write(storages / "whatsbot.db", 9999)
write(storages / "whatsapp.db", 8888)
write(data / ".env", 50)
write(data / "storages" / "plugins" / "x" / "tools.py", 77)
secret = tmp / "outside.txt"
secret.write_text("secret")

m = fm.FileManager(data, cache_ttl=0)

section("kind detection")
check("jpg is image", fm.kind_of("A.JPG") == "image")
check("ogg is audio", fm.kind_of("x.ogg") == "audio")
check("mp4 is video", fm.kind_of("x.mp4") == "video")
check("pdf is document", fm.kind_of("x.pdf") == "document")
check("no extension is document", fm.kind_of("README") == "document")

section("usage and scope")
u = m.usage()
check("received counts 4 files", u["roots"]["received"]["files"] == 4)
check("received bytes", u["roots"]["received"]["bytes"] == 6500)
check("sent counts 1 file", u["roots"]["sent"]["files"] == 1)
check("deletable bytes = media only", u["deletable_bytes"] == 7200)
check("system bytes cover db/session/plugins/.env? (storages only)",
      u["system_bytes"] >= 9999 + 8888 + 77)
check("system does not include the media roots", u["system_bytes"] < 9999 + 8888 + 77 + 7200)

section("listing: folders, sorting, filters, paging")
top = m.list_entries("received")
check("top level has 2 phone folders", [r["type"] for r in top["items"]] == ["dir", "dir"])
check("folder size aggregates", sorted(r["size"] for r in top["items"]) == [1300, 5200])
heavy = m.list_entries("received", flat=True, sort="size", order="desc")
check("flat + heaviest first", [r["name"] for r in heavy["items"]][:2] == ["c.mp4", "a.jpg"])
light = m.list_entries("received", flat=True, sort="size", order="asc")
check("lightest first", light["items"][0]["name"] == "d.pdf")
only_video = m.list_entries("received", flat=True, kind="video")
check("filter by kind", [r["name"] for r in only_video["items"]] == ["c.mp4"])
sub = m.list_entries("received", path="5511999990002")
check("sub folder lists its dates", sorted(r["name"] for r in sub["items"]) == ["2026-10-02", "2026-10-03"])
paged = m.list_entries("received", flat=True, limit=3)
check("paging reports next_offset", paged["next_offset"] == 3 and len(paged["items"]) == 3)
last = m.list_entries("received", flat=True, limit=3, offset=3)
check("last page has no next_offset", last["next_offset"] is None and len(last["items"]) == 1)
known = {"statics/media/5511999990001/2026-10-01/a.jpg"}
orph = m.list_entries("received", flat=True, known_paths=known, orphans_only=True)
check("orphans are files without a message", len(orph["items"]) == 3 and all(r["orphan"] for r in orph["items"]))
check("file rows expose a url", heavy["items"][0]["url"].startswith("/statics/media/"))
check("no row leaks an absolute path", all(str(tmp) not in str(r) for r in heavy["items"]))

section("security: forged and hostile ids")
check("garbage id", refused(m, "!!!not base64!!!"))
check("empty id", refused(m, ""))
check("unknown root", refused(m, forge("storages", "whatsbot.db")))
check("root folder itself", refused(m, forge("received", "")))
check("dotdot to the database", refused(m, forge("received", "../../storages/whatsbot.db")))
check("dotdot in the middle", refused(m, forge("received", "5511999990001/../../../.env")))
check("absolute path", refused(m, forge("received", "/etc/passwd")))
check("absolute path to outside file", refused(m, forge("received", str(secret))))
check("backslash traversal", refused(m, forge("received", "..\\..\\storages\\whatsbot.db")))
check("nul byte", refused(m, forge("received", "a\x00.jpg")))
check("missing file", refused(m, forge("received", "nope.jpg")))
check("sibling dir with same prefix is not the root",
      refused(m, forge("received", "../media_evil/x")))
ok_id = forge("received", "5511999990001/2026-10-01/a.jpg")
check("a valid id resolves", not refused(m, ok_id))

section("security: symlinks")
link_file = media / "5511999990001" / "link.jpg"
link_dir = media / "linkdir"
try:
    os.symlink(secret, link_file)
    os.symlink(storages, link_dir)
    have_symlinks = True
except (OSError, NotImplementedError):
    have_symlinks = False
    print("  (symlinks unavailable on this platform; skipped)")
if have_symlinks:
    check("symlink to a file is refused", refused(m, forge("received", "5511999990001/link.jpg")))
    check("symlink to a directory is refused", refused(m, forge("received", "linkdir")))
    check("file behind a symlinked dir is refused",
          refused(m, forge("received", "linkdir/whatsbot.db")))
    listed = m.list_entries("received", flat=True, kind="")
    names = {r["name"] for r in listed["items"]}
    check("symlinks are not listed", "link.jpg" not in names and "whatsbot.db" not in names)
    res = m.delete([forge("received", "5511999990001/link.jpg"), forge("received", "linkdir")])
    check("deleting a symlink id removes nothing", res.files == 0 and res.failed == 2)
    check("the secret file is intact", secret.exists())
    check("the database is intact", (storages / "whatsbot.db").exists())
    os.unlink(link_file)
    os.unlink(link_dir)

section("delete")
res = m.delete([ok_id])
check("delete one file", res.files == 1 and res.bytes == 1000 and res.failed == 0)
check("file is gone", not (media / "5511999990001" / "2026-10-01" / "a.jpg").exists())
check("reports the db media_path", res.removed_paths == ["statics/media/5511999990001/2026-10-01/a.jpg"])
res = m.delete([ok_id])
check("deleting twice does not blow up (counts as failed id)", res.files == 0)

res = m.delete([forge("received", "5511999990002")])
check("delete a folder removes its files", res.files == 2 and res.bytes == 5200)
check("folder is gone too", not (media / "5511999990002").exists())
check("sibling folder untouched", (media / "5511999990001" / "2026-10-02" / "b.ogg").exists())
check("media root still exists", media.is_dir())

res = m.delete([forge("sent", "1.png")])
check("delete from the sent root", res.files == 1 and not (operator / "1.png").exists())

before = {p for p in (storages).rglob("*")}
res = m.delete([forge("received", "../../storages"), forge("received", "..")])
check("hostile ids delete nothing", res.files == 0 and res.failed == 2)
check("storages untouched", {p for p in (storages).rglob("*")} == before)
check(".env untouched", (data / ".env").exists())
try:
    m.delete([ok_id] * (fm.MAX_DELETE_IDS + 1))
    check("too many ids is refused", False)
except fm.FileAccessError:
    check("too many ids is refused", True)

section("cache is invalidated by delete")
u2 = m.usage()
check("usage reflects deletions", u2["roots"]["received"]["files"] == 1)

section("media_policy: matrix")
d = mp.default_autodownload()
check("default keeps audio", d["audio"] == {"individual": True, "group": True})
check("default drops image/video/document",
      all(d[k] == {"individual": False, "group": False} for k in ("image", "video", "document")))
check("sticker follows image", mp.policy_kind("sticker") == "image")
check("location has no file", mp.policy_kind("location") is None)
check("audio kept in groups by default", mp.should_keep(None, "audio", True))
check("image dropped by default", not mp.should_keep(None, "image", False))
check("sticker dropped by default", not mp.should_keep(None, "sticker", True))
check("location always kept", mp.should_keep(None, "location", True))
custom = {"image": {"individual": True, "group": False}}
check("custom: image individual kept", mp.should_keep(custom, "image", False))
check("custom: image group dropped", not mp.should_keep(custom, "image", True))
check("custom merges over defaults", mp.should_keep(custom, "audio", False))
check("junk input falls back to defaults",
      mp.normalize("lixo") == d and mp.normalize({"image": "x", "zzz": 1}) == d)
check("non-bool values are ignored", mp.normalize({"image": {"individual": "yes"}}) == d)
n = mp.normalize({"image": {"individual": True}})
n["image"]["group"] = True
check("normalize returns a copy", mp.default_autodownload()["image"]["group"] is False)

section("media_policy: discard guard")
media.mkdir(parents=True, exist_ok=True)
inside = media / "p" / "x.jpg"
write(inside)
check("file inside statics/media is accepted",
      mp.is_discardable_path("statics/media/p/x.jpg", media, data) == inside.resolve())
check("absolute path inside is accepted",
      mp.is_discardable_path(str(inside), media, data) == inside.resolve())
check("path outside media is refused",
      mp.is_discardable_path(str(secret), media, data) is None)
check("dotdot is refused",
      mp.is_discardable_path("statics/media/../../storages/whatsbot.db", media, data) is None)
check("operator folder is refused",
      mp.is_discardable_path("statics/operator/1.png", media, data) is None)
check("geo path is refused", mp.is_discardable_path("geo:1,2", media, data) is None)
check("empty path is refused", mp.is_discardable_path("", media, data) is None)
check("missing file is refused", mp.is_discardable_path("statics/media/nope.jpg", media, data) is None)
check("the folder itself is refused", mp.is_discardable_path("statics/media", media, data) is None)

print(f"\n{'='*60}\n  {passed} passed, {failed} failed\n{'='*60}")
sys.exit(1 if failed else 0)
