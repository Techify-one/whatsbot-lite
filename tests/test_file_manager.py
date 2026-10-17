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
for _name in ("a.jfif", "a.JFIF", "a.jpe", "a.pjpeg", "a.avif", "a.tif", "a.tiff"):
    check(f"{_name} is image", fm.kind_of(_name) == "image")

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

section("media_policy: file from the Baixar button goes to the media root")
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 40

def listing(directory: Path) -> list[str]:
    return sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*"))

dl = tmp / "dl_app" / "statics" / "media"
# What GOWA's on-demand download produces: <phone>/<date>/<file>, no extension.
gowa_file = dl / "556492827555" / "2026-10-16" / "1792154699-b73e510d"
gowa_file.parent.mkdir(parents=True)
gowa_file.write_bytes(JPG)
(dl / "556492827555" / "2026-10-15").mkdir()  # left behind by an earlier attempt
final = mp.settle_download(gowa_file.resolve(), dl)
check("moved to the media root with the extension its content calls for",
      final == dl.resolve() / "1792154699-b73e510d.jpg" and final.read_bytes() == JPG)
check("the phone/date folders it came from are gone, empty siblings included",
      listing(dl) == ["1792154699-b73e510d.jpg"])

doc = dl / "5511" / "2026-10-16" / "report.pdf"
write(doc, 30)
final = mp.settle_download(doc.resolve(), dl)
check("a name that already has an extension is kept",
      final == dl.resolve() / "report.pdf" and final.exists())
check("...and its folder is cleaned up", not (dl / "5511").exists())

blob_in = dl / "5511" / "2026-10-16" / "opaque"
blob_in.parent.mkdir(parents=True)
blob_in.write_bytes(bytes(range(256)) * 4)
final = mp.settle_download(blob_in.resolve(), dl)
check("unknown content still moves to the root, name untouched",
      final == dl.resolve() / "opaque" and final.exists())

# Never overwrite a file that is already in the root.
(dl / "clash.jpg").write_bytes(b"original")
clash = dl / "5511" / "2026-10-16" / "clash"
clash.parent.mkdir(parents=True)
clash.write_bytes(JPG)
final = mp.settle_download(clash.resolve(), dl)
check("an existing file in the root is never overwritten",
      (dl / "clash.jpg").read_bytes() == b"original" and final != dl.resolve() / "clash.jpg"
      and final.read_bytes() == JPG and final.suffix == ".jpg")

# A folder that still holds something else must survive.
other = dl / "5599" / "2026-10-14" / "keep_me.jpg"
write(other, 5)
moved = dl / "5599" / "2026-10-16" / "newone"
moved.parent.mkdir(parents=True)
moved.write_bytes(JPG)
final = mp.settle_download(moved.resolve(), dl)
check("a folder that still has files is kept intact",
      other.exists() and not (dl / "5599" / "2026-10-16").exists() and final.parent == dl.resolve())

already = dl / "already.jpg"
already.write_bytes(JPG)
check("a file already in the root stays and the root is untouched",
      mp.settle_download(already.resolve(), dl) == already.resolve() and already.exists() and dl.is_dir())

check("extension_for knows the common media types",
      fm.extension_for("image/jpeg") == ".jpg" and fm.extension_for("audio/ogg") == ".ogg"
      and fm.extension_for("video/mp4") == ".mp4" and fm.extension_for("application/pdf") == ".pdf")
check("extension_for returns nothing it cannot vouch for",
      fm.extension_for(None) == "" and fm.extension_for("text/plain") == ""
      and fm.extension_for("application/x-unknown") == "")

section("viewer: what is shown inline")
pv = tmp / "preview"
def blob(name: str, data: bytes) -> Path:
    path = pv / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 40
PDF = b"%PDF-1.4\n1 0 obj"
check("jfif is jpeg", fm.preview_mime(blob("a.jfif", JPEG)) == "image/jpeg")
check("pjpeg is jpeg (the OS table misses it)", fm.preview_mime(blob("a.pjpeg", JPEG)) == "image/jpeg")
check("extension is case-insensitive", fm.preview_mime(blob("A.JFIF", JPEG)) == "image/jpeg")
check("pdf", fm.preview_mime(blob("a.pdf", PDF)) == "application/pdf")
check("opus is ogg audio", fm.preview_mime(blob("a.opus", b"OggS")) == "audio/ogg")
check("mp4", fm.preview_mime(blob("a.mp4", b"\x00\x00\x00\x18ftypmp42")) == "video/mp4")
check("txt/csv/json/log are text",
      all(fm.preview_mime(blob(f"a.{e}", b"hi")) == "text/plain" for e in ("txt", "csv", "json", "log", "md")))
check("a file without extension is recognised by content (jpeg)", fm.preview_mime(blob("semext1", JPEG)) == "image/jpeg")
check("a file without extension is recognised by content (pdf)", fm.preview_mime(blob("semext2", PDF)) == "application/pdf")
check("a wrong extension does not hide a real format", fm.preview_mime(blob("a.bin", b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)) == "image/png")
check("heic by content", fm.preview_mime(blob("semext3", b"\x00\x00\x00\x18ftypheic")) == "image/heic")
check("plain text without extension", fm.preview_mime(blob("semext4", "olá, mundo\n".encode())) == "text/plain")
check("opaque binary is download-only", fm.preview_mime(blob("semext5", bytes(range(256)) * 4)) is None)
check("docx/xlsx/zip are download-only",
      all(fm.preview_mime(blob(f"a.{e}", b"PK\x03\x04")) is None for e in ("docx", "xlsx", "zip")))
check("empty file without extension is download-only", fm.preview_mime(blob("semext6", b"")) is None)
# Safety: active content is never inline, even when it carries a harmless-looking name or content.
check("html is never inline", fm.preview_mime(blob("a.html", b"<script>alert(1)</script>")) is None)
check("htm/xhtml are never inline",
      all(fm.preview_mime(blob(f"a.{e}", b"<b>x</b>")) is None for e in ("htm", "xhtml")))
check("svg is never inline", fm.preview_mime(blob("a.svg", b"<svg><script>1</script></svg>")) is None)
check("svg is never inline in uppercase", fm.preview_mime(blob("A.SVG", b"<svg/>")) is None)
check("a symlink-free missing file does not raise", fm.preview_mime(pv / "does-not-exist") is None)

section("sender file names")
check("a plain name is kept", fm.clean_name("Teste.mp4") == "Teste.mp4")
check("accents and spaces are kept", fm.clean_name("Relatório final (v2).pdf") == "Relatório final (v2).pdf")
check("a path is cut to its last part", fm.clean_name("../../etc/passwd") == "passwd")
check("backslash paths too", fm.clean_name("..\\..\\evil.exe") == "evil.exe")
check("characters Windows refuses are dropped", fm.clean_name('a<b>c:d"e|f?g*h.txt') == "abcdefgh.txt")
check("control characters and newlines are dropped", fm.clean_name("a\x00b\nc\x1f.txt") == "abc.txt")
check("trailing dots and spaces are dropped (Windows)", fm.clean_name("  nome.  ") == "nome")
check("a dotfile name survives", fm.clean_name(".gitignore") == ".gitignore")
check("only dots/slashes leaves nothing", fm.clean_name("..") == "" and fm.clean_name("/") == "" and fm.clean_name("   ") == "")
check("not a string leaves nothing", fm.clean_name(None) == "" and fm.clean_name(5) == "")
_long = fm.clean_name("x" * 500 + ".mp4")
check("a long name is capped and keeps its extension",
      len(_long) <= fm.MAX_NAME_LEN and _long.endswith(".mp4"))
check("clean_name is idempotent", all(fm.clean_name(fm.clean_name(n)) == fm.clean_name(n)
      for n in ("a<b>.txt", "../x.y", "  z. ", "Relatório.pdf", "x" * 500 + ".mp4")))
check("app-made upload names are not treated as the user's",
      fm.upload_name("voice.ogg") == "" and fm.upload_name("image.png") == "" and fm.upload_name("blob") == "")
check("a real upload name is kept", fm.upload_name("foto da obra.jpg") == "foto da obra.jpg")
check("final_name uses the sender's name", fm.final_name("1792-uuid.mp4", "Teste.mp4") == "Teste.mp4")
check("final_name falls back to the disk name", fm.final_name("1792-uuid.jpg", None) == "1792-uuid.jpg"
      and fm.final_name("1792-uuid.jpg", "  ") == "1792-uuid.jpg")
check("a name without extension borrows the disk one", fm.final_name("1792-uuid.pdf", "Relatorio") == "Relatorio.pdf")
check("final_name never lets a path through", fm.final_name("x.pdf", "../../a.pdf") == "a.pdf")
check("name from a received document text", fm.name_from_content("[Documento recebido: Teste.pdf]") == "Teste.pdf")
check("name from a sent document with a caption",
      fm.name_from_content("[Documento enviado: Orçamento 2026.xlsx]\nsegue") == "Orçamento 2026.xlsx")
check("a name that contains a bracket", fm.name_from_content("[Documento recebido: a [1].pdf]\nlegenda") == "a [1].pdf")
check("the generic placeholder is not a name", fm.name_from_content("[Documento recebido: documento]") == "")
check("other text has no name", fm.name_from_content("[Áudio recebido]") == "" and fm.name_from_content("") == "")
check("a caption that looks like the tag is not read", fm.name_from_content("oi\n[Documento recebido: x.pdf]") == "")

section("listing with sender names")
# Own files: the sections above delete part of the shared sample tree.
named_root = media / "named"
write(named_root / "u1.jpg", 40)
write(named_root / "u2.mp4", 50)
write(named_root / "u3.pdf", 60)
write(named_root / "u4.ogg", 70)
names = {
    "statics/media/named/u1.jpg": "Foto da obra.jpg",
    "statics/media/named/u3.pdf": "Contrato.pdf",
}
def resolver(paths):
    return {p: names[p] for p in paths if p in names}
named = m.list_entries("received", path="named", flat=True, name_resolver=resolver)
by_rel = {r["rel"]: r for r in named["items"]}
check("a known name replaces the disk name", by_rel["named/u1.jpg"]["name"] == "Foto da obra.jpg")
check("a file without a known name keeps its disk name", by_rel["named/u2.mp4"]["name"] == "u2.mp4")
check("the id still points at the real file", not refused(m, by_rel["named/u1.jpg"]["id"])
      and base64.urlsafe_b64decode(by_rel["named/u1.jpg"]["id"] + "==").decode().endswith("u1.jpg"))
check("the on-disk relative path is still reported", by_rel["named/u3.pdf"]["rel"] == "named/u3.pdf")
by_name = m.list_entries("received", path="named", flat=True, sort="name", order="asc", name_resolver=resolver)
check("sorting by name uses the sender's names",
      [r["name"] for r in by_name["items"]] == ["Contrato.pdf", "Foto da obra.jpg", "u2.mp4", "u4.ogg"],
      str([r["name"] for r in by_name["items"]]))
paged_names = m.list_entries("received", path="named", flat=True, sort="name", order="asc",
                             limit=2, offset=1, name_resolver=resolver)
check("sorting by name pages over the names, not the disk names",
      [r["name"] for r in paged_names["items"]] == ["Foto da obra.jpg", "u2.mp4"],
      str([r["name"] for r in paged_names["items"]]))
dir_page = m.list_entries("received", name_resolver=resolver)
check("folders are never renamed", all(r["type"] != "dir" or r["name"] == r["rel"] for r in dir_page["items"]))
calls = []
def counting(paths):
    calls.append(len(paths))
    return {}
m.list_entries("received", path="named", flat=True, sort="date", limit=2, name_resolver=counting)
check("another sort resolves only the returned page", calls == [2], str(calls))

section("filters: period, selector, searchable text")
import time as _time
period_root = media / "period"
old_file = period_root / "old.jpg"
new_file = period_root / "new.jpg"
write(old_file, 11)
write(new_file, 22)
DAY = 86400.0
now = _time.time()
os.utime(old_file, (now - 10 * DAY, now - 10 * DAY))
m.invalidate()
def rels(page):
    return sorted(r["rel"] for r in page["items"])
base = dict(path="period", flat=True)
check("no date filter lists both", rels(m.list_entries("received", **base)) == ["period/new.jpg", "period/old.jpg"])
check("date_from keeps files modified on or after it",
      rels(m.list_entries("received", date_from=now - 2 * DAY, **base)) == ["period/new.jpg"])
check("date_to keeps files modified before it (exclusive)",
      rels(m.list_entries("received", date_to=now - 2 * DAY, **base)) == ["period/old.jpg"])
check("date_from is inclusive", rels(m.list_entries("received", date_from=new_file.stat().st_mtime, **base)) == ["period/new.jpg"])
check("date_to is exclusive", rels(m.list_entries("received", date_to=new_file.stat().st_mtime, **base)) == ["period/old.jpg"])
check("a range keeps what is inside it",
      rels(m.list_entries("received", date_from=now - 11 * DAY, date_to=now - 9 * DAY, **base)) == ["period/old.jpg"])
check("an empty range lists nothing", m.list_entries("received", date_from=now + DAY, **base)["total"] == 0)
check("the total size follows the filter",
      m.list_entries("received", date_from=now - 2 * DAY, **base)["total_bytes"] == 22)
tree = m.list_entries("received", date_from=now - 2 * DAY)
check("a folder is counted only with the files that pass the filter",
      next(r for r in tree["items"] if r["name"] == "period")["count"] == 1)

picked = []
def pick_new(entry, db_path):
    picked.append(db_path)
    return {"match": "name"} if entry.rel.endswith("new.jpg") else None
sel = m.list_entries("received", selector=pick_new, **base)
check("the selector drops what it returns None for", rels(sel) == ["period/new.jpg"])
check("the selector gets the media_path form stored in messages", "statics/media/period/new.jpg" in picked)
check("the selector's extra fields land on the row", sel["items"][0].get("match") == "name")
check("an empty dict keeps a file untouched",
      rels(m.list_entries("received", selector=lambda e, p: {}, **base)) == ["period/new.jpg", "period/old.jpg"])
folder_sel = m.list_entries("received", selector=pick_new)
check("the selector also filters the folder view",
      next(r for r in folder_sel["items"] if r["name"] == "period")["count"] == 1)

check("a placeholder alone leaves no searchable text",
      all(fm.searchable_text(t) == "" for t in (
          "[Áudio recebido]", "[Imagem enviada]", "[Vídeo recebido]", "[Sticker]",
          "[Documento recebido: Contrato.pdf]", "[Documento enviado: documento]")))
check("a caption survives", fm.searchable_text("obra da rua A") == "obra da rua A")
check("a transcription loses only its label",
      fm.searchable_text("[Transcrição do áudio]: preciso do boleto") == "preciso do boleto")
check("a description loses its label and the placeholder",
      fm.searchable_text("[Descrição da imagem]: um recibo\n[Imagem recebida]") == "um recibo")
check("a document's text loses the label",
      fm.searchable_text("[Documento recebido: a.pdf]\n[Conteúdo do documento]: cláusula quinta") == "cláusula quinta")
check("empty and missing text are fine", fm.searchable_text("") == "" and fm.searchable_text(None) == "")
check("brackets the app did not write are kept", fm.searchable_text("pedido [urgente] hoje") == "pedido [urgente] hoje")

print(f"\n{'='*60}\n  {passed} passed, {failed} failed\n{'='*60}")
sys.exit(1 if failed else 0)
