"""Tests for render_page: the page image handed to the model.

Usage: python tests/render.py <project folder> <output folder> <tool file> [extra folder of PDFs]
"""

import sys, io, os, re, json, glob, types, asyncio, base64, importlib.util, logging, warnings

logging.disable(logging.CRITICAL)
warnings.filterwarnings("ignore")
import numpy as np
from PIL import Image
from pypdf import PdfReader

PROJ, OUT, TOOL = sys.argv[1], sys.argv[2], sys.argv[3]
EXTRA = sys.argv[4] if len(sys.argv) > 4 else None
os.makedirs(OUT, exist_ok=True)

# ---- stand-in Open WebUI modules
STORE = {}


class Rec:
    def __init__(s, **k):
        s.__dict__.update(k)


class Files:
    @staticmethod
    async def get_file_by_id(fid):
        return STORE.get(fid)

    @staticmethod
    async def insert_new_file(user_id, form):
        r = Rec(
            id=form.id,
            user_id=user_id,
            filename=form.filename,
            path=form.path,
            meta=form.meta,
        )
        STORE[form.id] = r
        return r


class FileForm:
    def __init__(s, **k):
        s.__dict__.update(k)


class Storage:
    @staticmethod
    def get_file(path):
        return path

    @staticmethod
    def upload_file(fh, name, tags):
        p = os.path.join(OUT, "uploads", name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        data = fh.read()
        open(p, "wb").write(data)
        return data, p


for name, attrs in {
    "open_webui": {},
    "open_webui.models": {},
    "open_webui.models.files": {"Files": Files, "FileForm": FileForm},
    "open_webui.storage": {},
    "open_webui.storage.provider": {"Storage": Storage},
}.items():
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m

spec = importlib.util.spec_from_file_location("tool", f"{PROJ}/{TOOL}")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)
T = tool.Tools()
T.valves.base_url = "https://example.test"
USER = {"id": "u1"}
run = lambda c: asyncio.run(c)
results = []


def ok(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS" if cond else "FAIL"), "|", name, "|", str(detail)[:150])


def attach(path, fid):
    STORE[fid] = Rec(id=fid, user_id="u1", filename=os.path.basename(path), path=path)
    return {
        "id": fid,
        "name": os.path.basename(path),
        "content_type": "application/pdf",
    }


events = []


async def emit(e):
    events.append(e)


# What Open WebUI 0.11.1 requires of a tool result for it to reach the model as an image:
# middleware.process_tool_result: isinstance(str) and startswith('data:image/')
SAFE = re.compile(r"^data:image/(png|jpeg|gif|webp);base64,", re.I)


def decode(result):
    if not (
        isinstance(result, str)
        and result.startswith("data:image/")
        and SAFE.match(result)
    ):
        return None
    return Image.open(io.BytesIO(base64.b64decode(result.split(",", 1)[1])))


# 1. every page of every form renders
forms = sorted(glob.glob(f"{PROJ}/*.pdf")) + (
    sorted(glob.glob(f"{EXTRA}/*.pdf")) if EXTRA else []
)
total = good = 0
biggest = 0
for f in forms:
    n = len(PdfReader(f).pages)
    for pg in range(1, n + 1):
        total += 1
        try:
            data, mime, d = tool.render_pdf_page(f, page=pg)
            im = Image.open(io.BytesIO(data))
            im.load()
            biggest = max(biggest, len(data))
            if (
                im.size == (d["width"], d["height"])
                and max(im.size) <= 2000
                and len(data) <= 3000 * 1024
            ):
                good += 1
            else:
                print("   odd", os.path.basename(f), pg, d)
        except Exception as e:
            print("   error", os.path.basename(f), pg, repr(e)[:100])
ok(
    "every page of every PDF renders within limits",
    good == total,
    f"{good}/{total} pages, largest {biggest // 1024} KB",
)

# 2. through the tool: the result is exactly what Open WebUI turns into a picture
f1 = attach(f"{PROJ}/CME Disclosure Form Template.pdf", "f-disc")
r = run(T.render_page(__user__=USER, __files__=[f1], __event_emitter__=emit))
im = decode(r)
ok("result is a data:image string Open WebUI accepts", im is not None, r[:40])
ok(
    "no text is mixed into the result",
    im is not None and "{" not in r[:30] and "\n" not in r,
)
ok("nothing shown in chat unless asked", len(events) == 0, f"{len(events)} events")
r = run(
    T.render_page(
        page=2, show_in_chat=True, __user__=USER, __files__=[f1], __event_emitter__=emit
    )
)
shown = events[-1]["data"]["files"][0] if events else {}
ok(
    "show_in_chat emits an image to the chat",
    decode(r) is not None
    and shown.get("type") == "image"
    and shown.get("url", "").startswith("/api/v1/files/"),
    shown,
)
r = run(
    T.render_page(
        page=1,
        show_in_chat="false",
        __user__=USER,
        __files__=[f1],
        __event_emitter__=emit,
    )
)
ok("show_in_chat='false' (string) does not show", len(events) == 1)

# 3. the filled values are visible in the image
blank = np.asarray(
    decode(run(T.render_page(__user__=USER, __files__=[f1]))).convert("L"),
    dtype=np.int16,
)
out = json.loads(
    run(
        T.fill_form(
            json.dumps(
                {
                    "Text4": "Dr. Jane Doe",
                    "Text5": "Sepsis Update 2026",
                    "Check Box1": True,
                }
            ),
            __user__=USER,
            __files__=[f1],
        )
    )
)
ok("fill_form still works", out.get("status") == "ok", out.get("error", ""))
filled_img = decode(
    run(T.render_page(file_id=out.get("file_id", ""), __user__=USER, __files__=[f1]))
)
filled = np.asarray(filled_img.convert("L"), dtype=np.int16)
diff = int((np.abs(filled - blank) > 60).sum()) if filled.shape == blank.shape else -1
ok(
    "image of the filled copy shows the new values",
    diff > 300,
    f"{diff} pixels changed",
)
filled_img.save(f"{OUT}/filled_disclosure_p1.png")
f2 = attach(STORE[out["file_id"]].path, out["file_id"])
r = run(T.render_page(__user__=USER, __files__=[f1, f2]))
ok(
    "two PDFs attached and no file_id -> clear error, not an image",
    decode(r) is None and "Several PDFs" in json.loads(r).get("error", ""),
    r[:80],
)

# 4. flattened copy and a form with no fields
b, _ = tool.fill_pdf_fields(
    f"{PROJ}/CME Disclosure Form Template.pdf", {"Text4": "Dr. Jane Doe"}, flatten=True
)
open(f"{OUT}/flat.pdf", "wb").write(b)
data, mime, d = tool.render_pdf_page(f"{OUT}/flat.pdf")
fl = np.asarray(Image.open(io.BytesIO(data)).convert("L"), dtype=np.int16)
ok("flattened copy renders with its values", int((np.abs(fl - blank) > 60).sum()) > 100)
data, mime, d = tool.render_pdf_page(f"{PROJ}/205_boc_test-filled.pdf", page=3)
ok("PDF with no fields still renders (for the model to read)", d["width"] > 500, d)

# 5. bad requests
for bad, want in (
    (0, "does not exist"),
    (99, "does not exist"),
    ("abc", "whole number"),
):
    r = run(T.render_page(page=bad, __user__=USER, __files__=[f1]))
    ok(
        f"page={bad!r} -> clear error",
        decode(r) is None and want in json.loads(r).get("error", ""),
        r[:90],
    )
r = run(T.render_page(__user__=USER, __files__=[]))
ok("no PDF attached -> clear error", "No PDF" in json.loads(r).get("error", ""), r[:70])
STORE["f-other"] = Rec(
    id="f-other",
    user_id="someone-else",
    filename="x.pdf",
    path=f"{PROJ}/205_boc_test.pdf",
)
r = run(T.render_page(file_id="f-other", __user__=USER, __files__=[]))
ok(
    "another user's file refused",
    "another user" in json.loads(r).get("error", ""),
    r[:70],
)
if EXTRA:
    pw = glob.glob(
        f"{os.path.dirname(EXTRA)}/../sample-files/**/libreoffice-writer-password.pdf",
        recursive=True,
    )
    if pw:
        fp = attach(pw[0], "f-pw")
        r = run(T.render_page(__user__=USER, __files__=[fp]))
        ok(
            "password-protected PDF -> clear error",
            "password" in json.loads(r).get("error", ""),
            r[:70],
        )

# 6. size limits and settings
data, mime, d = tool.render_pdf_page(
    f"{PROJ}/205_boc_test.pdf", page=1, dpi=300, max_kb=200
)
ok("small size limit is respected", len(data) <= 200 * 1024, d)
data, mime, d = tool.render_pdf_page(f"{PROJ}/205_boc_test.pdf", page=1, dpi=9999)
ok("resolution is capped", max(d["width"], d["height"]) <= 2000, d)
T.valves.render_dpi = 60
small = decode(run(T.render_page(__user__=USER, __files__=[f1])))
T.valves.render_dpi = 110
ok(
    "render_dpi setting is used",
    small is not None and small.width < blank.shape[1],
    f"{small.width} vs {blank.shape[1]}",
)

# 7. rotated pages come out the way a reader sees them
if EXTRA and os.path.exists(f"{EXTRA}/cropped-rotated-scaled.pdf"):
    sizes = []
    for pg in (1, 2, 3, 4):
        data, mime, d = tool.render_pdf_page(
            f"{EXTRA}/cropped-rotated-scaled.pdf", page=pg
        )
        sizes.append((d["width"], d["height"]))
        Image.open(io.BytesIO(data)).save(f"{OUT}/rotated_p{pg}.png")
    ok(
        "rotated and cropped pages render",
        all(w > 100 and h > 100 for w, h in sizes),
        sizes,
    )

n = sum(1 for r in results if r[1])
print(f"\n{TOOL}: {n}/{len(results)} passed")
for r in results:
    if not r[1]:
        print("   FAILED:", r[0], "|", r[2])
