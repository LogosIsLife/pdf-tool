"""End-to-end and edge-case tests, with stand-ins for the Open WebUI modules.

Usage: python tests/e2e.py <project folder> <output folder> [tool file]

The tool file is pdf_tool.py in the project folder unless given; an absolute
path works too. The sample PDFs are read from the project folder.
"""

import sys, io, os, json, types, asyncio, importlib.util, shutil
from pypdf import PdfReader
import pdfplumber

PROJ, OUT = sys.argv[1], sys.argv[2]
TOOL = sys.argv[3] if len(sys.argv) > 3 else "pdf_tool.py"
os.makedirs(OUT, exist_ok=True)

# ---- stand-in Open WebUI modules -------------------------------------------------
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

spec = importlib.util.spec_from_file_location("tool", os.path.join(PROJ, TOOL))
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)
T = tool.Tools()
T.valves.base_url = "https://example.test"
USER = {"id": "u1"}
results = []


def ok(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS" if cond else "FAIL"), "|", name, "|", detail)


def attach(fname, fid):
    STORE[fid] = Rec(id=fid, user_id="u1", filename=fname, path=f"{PROJ}/{fname}")
    return {"id": fid, "name": fname, "content_type": "application/pdf"}


events = []


async def emit(e):
    events.append(e)


run = lambda c: asyncio.run(c)

# 1 list + fill through the tool methods
f1 = attach("CME Disclosure Form Template.pdf", "f-disc")
listing = json.loads(run(T.list_form_fields(__user__=USER, __files__=[f1])))
ok(
    "list_form_fields returns fields",
    listing.get("field_count") == 23,
    f"field_count={listing.get('field_count')}",
)
vals = {
    "Text4": "Dr. Jane Doe",
    "Text5": "Sepsis Update",
    "Text6": "10/15/2026",
    "Check Box1": "/Yes",
    "Check Box7": "/Yes",
    "Nope": "x",
}
out = json.loads(
    run(
        T.fill_form(
            json.dumps(vals), __user__=USER, __files__=[f1], __event_emitter__=emit
        )
    )
)
ok(
    "fill with an unknown name is partial, not ok",
    out.get("status") == "partial" and out.get("file_id"),
    str({k: out.get(k) for k in ("status", "error", "file_name", "flattened")}),
)
ok(
    "problems come before the file details",
    list(out)[:3] == ["status", "ignored_unknown_fields", "next"],
    str(list(out)[:4]),
)
ok(
    "unknown field reported",
    out.get("ignored_unknown_fields") == ["Nope"],
    str(out.get("ignored_unknown_fields")),
)
ok(
    "full download url built",
    str(out.get("download_url", "")).startswith("https://example.test/api/v1/files/"),
    str(out.get("download_url")),
)
ok("file event emitted", bool(events) and events[-1]["type"] == "files")
if out.get("file_id"):
    rec = STORE[out["file_id"]]
    r = PdfReader(rec.path)
    f = r.get_fields()
    ok(
        "stored file has values",
        f["Text4"].get("/V") == "Dr. Jane Doe"
        and str(f["Check Box1"].get("/V")) == "/Yes",
        f"{f['Text4'].get('/V')!r} {f['Check Box1'].get('/V')!r}",
    )
    ok(
        "untouched field stays empty",
        f["Check Box2"].get("/V") in (None, "/Off")
        and not f["Text4"].get("/V") is None,
        repr(f["Check Box2"].get("/V")),
    )
    ok("still editable", len(f) == 23)

# 2 flatten argument forms
for arg, want in [
    (None, False),
    ("false", False),
    (False, False),
    ("true", True),
    (True, True),
]:
    o = json.loads(
        run(
            T.fill_form(
                json.dumps({"Text4": "A"}), flatten=arg, __user__=USER, __files__=[f1]
            )
        )
    )
    ok(
        f"flatten={arg!r} -> flattened {want}",
        o.get("flattened") == want,
        f"got {o.get('flattened')} {o.get('error', '')}",
    )

# 3 other user's file, no pdf, several pdfs, bad json, flat pdf
STORE["f-other"] = Rec(
    id="f-other",
    user_id="someone-else",
    filename="x.pdf",
    path=f"{PROJ}/205_boc_test.pdf",
)
o = json.loads(
    run(T.fill_form('{"a":"b"}', file_id="f-other", __user__=USER, __files__=[]))
)
ok(
    "another user's file refused",
    "another user" in o.get("error", ""),
    o.get("error", ""),
)
o = json.loads(run(T.fill_form('{"a":"b"}', __user__=USER, __files__=[])))
ok("no PDF attached -> clear error", "No PDF" in o.get("error", ""), o.get("error", ""))
f2 = attach("205_boc_test.pdf", "f-205")
o = json.loads(run(T.fill_form('{"a":"b"}', __user__=USER, __files__=[f1, f2])))
ok(
    "several PDFs -> asks for file_id",
    "Several PDFs" in o.get("error", ""),
    o.get("error", "")[:70],
)
o = json.loads(run(T.fill_form("{bad", __user__=USER, __files__=[f1])))
ok(
    "bad JSON -> clear error",
    "not valid JSON" in o.get("error", ""),
    o.get("error", "")[:60],
)
f3 = attach("205_boc_test-filled.pdf", "f-flat")
o = json.loads(run(T.fill_form('{"a":"b"}', __user__=USER, __files__=[f3])))
ok("flat PDF -> readable error", "error" in o, o.get("error", "")[:110])
o = json.loads(run(T.list_form_fields(__user__=USER, __files__=[f3])))
ok(
    "flat PDF listing explains",
    o.get("field_count") == 0 and "no fillable" in o.get("note", ""),
    o.get("note", "")[:60],
)

# 4 values already in the form survive a partial fill, editable and flattened
src = f"{PROJ}/CME Application Form 2026.pdf"
pdf, _ = tool.fill_pdf_fields(src, {"Text1": "New Title"}, flatten=False)
f = PdfReader(io.BytesIO(pdf)).get_fields()
ok(
    "partial fill keeps existing value (editable)",
    f["Text7"].get("/V") == "lijlkjkl" and f["Text1"].get("/V") == "New Title",
    f"Text7={f['Text7'].get('/V')!r} Text1={f['Text1'].get('/V')!r}",
)
pdf, _ = tool.fill_pdf_fields(src, {"Text1": "New Title"}, flatten=True)
open(f"{OUT}/partial_flat.pdf", "wb").write(pdf)
txt = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
ok(
    "partial fill keeps existing value (flattened)",
    "lijlkjkl" in txt and "New Title" in txt,
    f"existing shown={'lijlkjkl' in txt} new shown={'New Title' in txt}",
)

# 5 special characters and long text
src = f"{PROJ}/CME Disclosure Form Template.pdf"
special = {
    "Text4": "José O'Brien (MD)",
    "Text5": "Heart & Lung: 50% / A\\B",
    "Text6": "Zoë — “quoted” ½",
    "Name of Companys and RelationshipRow1": "A very long company name that certainly cannot fit inside this narrow table cell at all",
}
for flat in (False, True):
    try:
        pdf, _ = tool.fill_pdf_fields(src, special, flatten=flat)
        open(f"{OUT}/special_{'flat' if flat else 'edit'}.pdf", "wb").write(pdf)
        if not flat:
            f = PdfReader(io.BytesIO(pdf)).get_fields()
            bad = {k: f[k].get("/V") for k, v in special.items() if f[k].get("/V") != v}
            ok("special characters stored exactly", not bad, str(bad)[:150])
        else:
            t = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
            ok(
                "special characters drawn when flattened",
                "O'Brien (MD)" in t and "Heart & Lung: 50% / A\\B" in t,
                repr(
                    [
                        l
                        for l in t.splitlines()
                        if "Brien" in l or "Heart" in l or "Zo" in l
                    ][:3]
                ),
            )
    except Exception as e:
        ok(f"special characters flatten={flat}", False, repr(e)[:160])

# 6 radio: choose each option in turn; checkbox: turn on then off again
src = f"{PROJ}/00-985-filled.pdf"
for choice in ("/A", "/B"):
    pdf, _ = tool.fill_pdf_fields(src, {"AOrB": choice}, flatten=False)
    r = PdfReader(io.BytesIO(pdf))
    states = [
        str(a.get_object().get("/AS"))
        for a in r.pages[0]["/Annots"]
        if "/Parent" in a.get_object() and a.get_object()["/Parent"].get("/T") == "AOrB"
    ]
    ok(
        f"radio {choice}: exactly that option on",
        states.count(choice) == 1 and states.count("/Off") == 1,
        str(states),
    )
src = f"{PROJ}/205_boc_test.pdf"
pdf, _ = tool.fill_pdf_fields(src, {"registered": "/is an organization"}, flatten=False)
open(f"{OUT}/cb_on.pdf", "wb").write(pdf)
pdf2, _ = tool.fill_pdf_fields(
    f"{OUT}/cb_on.pdf", {"registered": "/Off"}, flatten=False
)
f = PdfReader(io.BytesIO(pdf2)).get_fields()
ok(
    "checkbox can be turned off again",
    str(f["registered"].get("/V")) == "/Off",
    repr(f["registered"].get("/V")),
)
import inspect

HAS_REJ = "rejected" in inspect.signature(tool.fill_pdf_fields).parameters
for loose in ("Yes", True, "true", "is an organization", "/IS AN ORGANIZATION"):
    pdf3, _ = tool.fill_pdf_fields(src, {"registered": loose}, flatten=False)
    f = PdfReader(io.BytesIO(pdf3)).get_fields()
    ok(
        f"checkbox accepts {loose!r}",
        str(f["registered"].get("/V")) == "/is an organization",
        f"stored {f['registered'].get('/V')!r}",
    )
pdf3, _ = tool.fill_pdf_fields(src, {"registered": "no"}, flatten=False)
f = PdfReader(io.BytesIO(pdf3)).get_fields()
ok(
    "checkbox 'no' leaves it off",
    str(f["registered"].get("/V")) in ("/Off", "None"),
    repr(f["registered"].get("/V")),
)
if HAS_REJ:
    rej = []
    pdf3, _ = tool.fill_pdf_fields(
        f"{PROJ}/00-985-filled.pdf",
        {"AOrB": "banana", "Name": "Kept"},
        flatten=False,
        rejected=rej,
    )
    f = PdfReader(io.BytesIO(pdf3)).get_fields()
    ok(
        "radio with a bad option is reported, not applied",
        len(rej) == 1
        and str(f["AOrB"].get("/V")) == "/A"
        and f["Name"].get("/V") == "Kept",
        f"rejected={rej} AOrB={f['AOrB'].get('/V')!r}",
    )
    for loose, want in (("b", "/B"), ("A", "/A"), ("/b", "/B")):
        pdf3, _ = tool.fill_pdf_fields(
            f"{PROJ}/00-985-filled.pdf", {"AOrB": loose}, flatten=False
        )
        f = PdfReader(io.BytesIO(pdf3)).get_fields()
        ok(
            f"radio accepts {loose!r}",
            str(f["AOrB"].get("/V")) == want,
            repr(f["AOrB"].get("/V")),
        )
    o = json.loads(
        run(
            T.fill_form(
                json.dumps({"Check Box1": "maybe", "Text4": "Z"}),
                __user__=USER,
                __files__=[f1],
            )
        )
    )
    ok(
        "fill_form reports the unset checkbox",
        o.get("filled_fields") == ["Text4"]
        and o.get("not_set_invalid_option", [{}])[0].get("field") == "Check Box1",
        str({k: o.get(k) for k in ("filled_fields", "not_set_invalid_option")})[:200],
    )

# 7 refill of the tool's own output (fill in two steps)
src = f"{PROJ}/CME Disclosure Form Template.pdf"
a, _ = tool.fill_pdf_fields(src, {"Text4": "Step One"}, flatten=False)
open(f"{OUT}/step1.pdf", "wb").write(a)
b, _ = tool.fill_pdf_fields(f"{OUT}/step1.pdf", {"Text5": "Step Two"}, flatten=False)
f = PdfReader(io.BytesIO(b)).get_fields()
ok(
    "second fill on the first output keeps both",
    f["Text4"].get("/V") == "Step One" and f["Text5"].get("/V") == "Step Two",
    f"{f['Text4'].get('/V')!r} {f['Text5'].get('/V')!r}",
)

# 8 listing quality on awkward fields
if hasattr(tool, "inspect_pdf_fields"):
    L = {
        e["name"]: e
        for e in tool.inspect_pdf_fields(f"{PROJ}/205_boc_test.pdf")["fields"]
    }
    print("   listing Print Form  :", json.dumps(L.get("Print Form"))[:150])
    L = {
        e["name"]: e
        for e in tool.inspect_pdf_fields(f"{PROJ}/CME Application Form 2026.pdf")[
            "fields"
        ]
    }
    print("   listing Text7       :", json.dumps(L.get("Text7"))[:200])
    I = tool.inspect_pdf_fields(f"{PROJ}/CME Broadcast Consent Template.pdf")
    L = {e["name"]: e for e in I["fields"]}
    print("   listing split       :", json.dumps(I.get("split_fields"))[:150])
    print("   listing Text1_1     :", json.dumps(L.get("Text1_1"))[:330])
    L = {
        e["name"]: e
        for e in tool.inspect_pdf_fields(f"{PROJ}/00-985-filled copy.pdf")["fields"]
    }
    print("   listing AOrB (copy) :", json.dumps(L.get("AOrB"))[:260])
# 9 names that differ in case or punctuation, labels, and the result contract
src = f"{PROJ}/205_boc_test.pdf"
f5 = attach("205_boc_test.pdf", "f-205b")
sent = {
    "Street Address of registered agent:": "1 Main St",
    "Zip Code of registered agent": "78701",
    "City of registered agent:": "Austin",
}
rep_ = {}
pdf, unk = tool.fill_pdf_fields(src, sent, report=rep_)
f = PdfReader(io.BytesIO(pdf)).get_fields()
ok(
    "names differing in case or punctuation are matched",
    not unk
    and f["Street address of registered agent:"].get("/V") == "1 Main St"
    and f["Zip code of registered agent:"].get("/V") == "78701",
    f"unknown={unk} remapped={rep_.get('remapped')}",
)
ok(
    "remapped names are reported",
    rep_.get("remapped")
    == {
        "Street Address of registered agent:": "Street address of registered agent:",
        "Zip Code of registered agent": "Zip code of registered agent:",
    },
    str(rep_.get("remapped")),
)
ok(
    "pages written to are reported",
    rep_.get("pages_touched") == [1] and not rep_.get("did_not_stick"),
    f"{rep_.get('pages_touched')} {rep_.get('did_not_stick')}",
)
rep_ = {}
pdf, unk = tool.fill_pdf_fields(
    src,
    {"Mailing Address": "9 Elm St", "Middle Initial of Governing Person:": "A"},
    report=rep_,
)
f = PdfReader(io.BytesIO(pdf)).get_fields()
ok(
    "a tooltip that fits one field is matched",
    f["Initial Mailing Address"].get("/V") == "9 Elm St"
    and rep_["pages_touched"] == [1, 2],
    f"{f['Initial Mailing Address'].get('/V')!r} {rep_.get('remapped')}",
)
ok(
    "a name off by one space wins over a tooltip three fields share",
    f["Middle Initial  of Governing Person:"].get("/V") == "A"
    and not rep_.get("ambiguous"),
    str(rep_.get("ambiguous"))[:160],
)
box = lambda page, tip: [{"page": page, "kind": "text", "value": "", "tooltip": tip}]
m, r, u, amb = tool._resolve_names(
    {"a1": box(1, "Home City"), "a2": box(2, "Home City"), "a3": box(2, "Zip")},
    {"home city": "Waco", "ZIP": "1", "zzz": "2"},
)
ok(
    "a name that fits several fields is not guessed",
    not m
    and u == ["ZIP", "zzz"]
    and amb[0].get("sent") == "home city"
    and [c["name"] for c in amb[0]["candidates"]] == ["a1", "a2"],
    f"{m} {u} {amb}"[:200],
)
o = run(T.fill_form(json.dumps(sent), __user__=USER, __files__=[f5]))
o = json.loads(o) if isinstance(o, str) else o
ok(
    "fill_form is ok when every name resolves",
    o.get("status") == "ok"
    and o.get("pages_touched") == [1]
    and "render_page" in o.get("next", ""),
    str({k: o.get(k) for k in ("status", "pages_touched", "next")})[:200],
)
before = len(STORE)
o = json.loads(
    run(T.fill_form(json.dumps({"Nope": "x"}), __user__=USER, __files__=[f5]))
)
ok(
    "nothing filled -> failed, and no file made",
    o.get("status") == "failed" and "file_id" not in o and len(STORE) == before,
    str(o)[:160],
)
o = json.loads(
    run(
        T.fill_form(
            json.dumps({"Text4": "Z", "Check Box1": "maybe"}),
            __user__=USER,
            __files__=[f1],
        )
    )
)
ok(
    "an invalid option makes the fill partial",
    o.get("status") == "partial" and list(o)[1] == "not_set_invalid_option",
    str(list(o)[:3]),
)
o = json.loads(
    run(
        T.fill_form(
            json.dumps(
                {
                    "Initial Mailing Address": "",
                    "City of Initial Mailing Address": "Austin",
                }
            ),
            __user__=USER,
            __files__=[f5],
        )
    )
)
ok(
    "a value sent empty is named, not passed off as filled text",
    o.get("status") == "ok"
    and o.get("left_blank_as_sent") == ["Initial Mailing Address"],
    str(o.get("left_blank_as_sent")),
)
first = {
    "City of Initial Mailing Address": "Austin",
    "State of Initial Mailing Address": "TX",
    "Zip Code of Initial Mailing Address": "78711",
}
o = json.loads(run(T.fill_form(json.dumps(first), __user__=USER, __files__=[f5])))
ok(
    "a field the model left out is listed as still empty",
    "Initial Mailing Address" in o.get("still_empty", {}).get("page 2", [])
    and "City of Initial Mailing Address" not in str(o.get("still_empty"))
    and "still_empty" in o.get("next", ""),
    str(o.get("still_empty", {}).get("page 2"))[:120],
)
ok(
    "push buttons and checkboxes are not listed as empty",
    not {"Print Form", "registered", "document"}
    & {n for v in o.get("still_empty", {}).values() for n in v},
)

# 10 a value that is stored but not drawn is caught, and drawn on the next fill
import pymupdf

d = pymupdf.open(src)
for pg in d:
    for w in pg.widgets():
        if w.field_name == "Initial Mailing Address":
            d.xref_set_key(w.xref, "V", "(9 Elm St)")
d.save(f"{OUT}/undrawn.pdf")
d.close()
miss = tool._read_back(
    open(f"{OUT}/undrawn.pdf", "rb").read(), {"Initial Mailing Address": "9 Elm St"}
)
ok(
    "stored but undrawn value is reported",
    len(miss) == 1 and "not drawn" in miss[0].get("reason", ""),
    str(miss),
)
miss = tool._read_back(
    open(src, "rb").read(), {"City:": "Waco", "registered": "/is an organization"}
)
ok(
    "values that are absent are reported",
    [m["field"] for m in miss] == ["City:", "registered"],
    str(miss)[:200],
)
rep_ = {}
pdf, _ = tool.fill_pdf_fields(
    f"{OUT}/undrawn.pdf", {"City of Initial Mailing Address": "Waco"}, report=rep_
)
ok(
    "every fill draws values the form stored but never drew",
    not tool._read_back(
        pdf,
        {
            "Initial Mailing Address": "9 Elm St",
            "City of Initial Mailing Address": "Waco",
        },
    ),
)
long = "1234 Long Example Street, Suite 500"
pdf, _ = tool.fill_pdf_fields(src, {"Initial Mailing Address": long})
d = pymupdf.open(stream=pdf, filetype="pdf")
w = next(w for w in d[1].widgets() if w.field_name == "Initial Mailing Address")
d2 = pymupdf.open(
    stream=tool.fill_pdf_fields(src, {"Initial Mailing Address": long}, flatten=True)[
        0
    ],
    filetype="pdf",
)
ok(
    "a long value is drawn whole inside its box",
    long in d2[1].get_text("text", clip=w.rect + (-2, -2, 2, 2)),
    repr(d2[1].get_text("text", clip=w.rect + (-2, -2, 2, 2))),
)
open(f"{OUT}/long.pdf", "wb").write(pdf)
import base64


def dark(data, rect, dpi):
    """Rightmost column with ink inside rect, in points from the left edge of the box."""
    pm = pymupdf.Pixmap(data)
    k = dpi / 72
    cols = []
    for x in range(int(rect.x0 * k), int(rect.x1 * k)):
        if any(
            sum(pm.pixel(x, y)[:3]) < 300
            for y in range(int(rect.y0 * k) + 2, int(rect.y1 * k) - 2)
        ):
            cols.append(x)
    return (max(cols) / k - rect.x0) if cols else 0


data, mime, det = tool.render_pdf_page(f"{OUT}/long.pdf", page=2)
end = dark(data, w.rect, det["dpi"])
need = pymupdf.get_text_length(long, "tiro", 8.5)
ok(
    "the page image shows the long value whole, as the file draws it",
    need - 6 <= end <= w.rect.width - 2,
    f"ink ends at {end:.0f} pt, text is {need:.0f} pt, box is {w.rect.width:.0f} pt",
)

# 11 page images in the result, where Open WebUI takes them (0.11.4 on)
ok(
    "no page images on an older Open WebUI",
    isinstance(run(T.fill_form(json.dumps(sent), __user__=USER, __files__=[f5])), str),
)
sys.modules["open_webui.utils"] = types.ModuleType("open_webui.utils")
mw = types.ModuleType("open_webui.utils.middleware")
mw.extract_base64_images = lambda value, files: value
sys.modules["open_webui.utils.middleware"] = mw
sys.modules["open_webui.utils"].middleware = mw
both = dict(sent, **{"Initial Mailing Address": "9 Elm St", "Date:": "09/28/2026"})
o = run(T.fill_form(json.dumps(both), __user__=USER, __files__=[f5]))
ok(
    "touched pages come back as images",
    isinstance(o, dict)
    and sorted(o.get("page_images", {})) == ["page_1", "page_2", "page_3"]
    and all(
        v.startswith("data:image/") and " " not in v for v in o["page_images"].values()
    ),
    str(o.get("pages_touched") if isinstance(o, dict) else o[:80]),
)
T.valves.review_pages = 1
o = run(T.fill_form(json.dumps(both), __user__=USER, __files__=[f5]))
ok(
    "pages over the limit are left to render_page",
    list(o.get("page_images", {})) == ["page_1"]
    and "page(s) 2, 3" in o.get("next", ""),
    o.get("next", "")[-150:],
)
T.valves.review_pages = 0
ok(
    "review_pages=0 sends no images",
    isinstance(run(T.fill_form(json.dumps(both), __user__=USER, __files__=[f5])), str),
)
T.valves.review_pages = 3

# 12 emptying a box, filler, values written over, and what the listing says
T.valves.review_pages = 0  # results as text
src = f"{PROJ}/205_boc_test.pdf"
held = {
    "City:": "Zzyzx",
    "Supplemental Provisions/Information:": "None.",
    "Organization Name:": "N/A",
    "Last Name of Governing Person:": "Nance",
}
rep_ = {}
pdf, _ = tool.fill_pdf_fields(src, held, report=rep_)
open(f"{OUT}/held.pdf", "wb").write(pdf)
ok(
    "filler is named, a real value is not",
    rep_.get("filler")
    == ["Supplemental Provisions/Information:", "Organization Name:"],
    str(rep_.get("filler")),
)
for flat in (False, True):
    rep_ = {}
    pdf, _ = tool.fill_pdf_fields(
        f"{OUT}/held.pdf",
        {"Supplemental Provisions/Information:": "", "City:": ""},
        flatten=flat,
        report=rep_,
    )
    d = pymupdf.open(stream=pdf, filetype="pdf")
    shown = d[0].get_text() + d[1].get_text()
    ok(
        f"an empty value empties the box (flatten={flat})",
        not rep_["did_not_stick"]
        and "None." not in shown
        and "Zzyzx" not in shown
        and "Nance" in shown,
        f"{rep_['did_not_stick']} None.={'None.' in shown} Zzyzx={'Zzyzx' in shown}",
    )
    if not flat:
        f = PdfReader(io.BytesIO(pdf)).get_fields()
        ok(
            "the emptied box holds no value, the others keep theirs",
            not f["City:"].get("/V")
            and not f["Supplemental Provisions/Information:"].get("/V")
            and f["Last Name of Governing Person:"].get("/V") == "Nance",
            f"{f['City:'].get('/V')!r} {f['Last Name of Governing Person:'].get('/V')!r}",
        )
        ok(
            "an emptied box is left blank as sent, not still empty",
            rep_["blank"] == ["Supplemental Provisions/Information:", "City:"]
            and not {"City:", "Supplemental Provisions/Information:"}
            & {n for v in rep_["still_empty"].values() for n in v},
            str(rep_["blank"]),
        )
        ok(
            "the value that was there is reported",
            rep_["replaced"]
            == {"Supplemental Provisions/Information:": "None.", "City:": "Zzyzx"},
            str(rep_["replaced"]),
        )
        open(f"{OUT}/emptied.pdf", "wb").write(pdf)
rep_ = {}
pdf, _ = tool.fill_pdf_fields(f"{OUT}/emptied.pdf", {"City:": "Waco"}, report=rep_)
ok(
    "an emptied box can be filled again",
    not rep_["did_not_stick"]
    and PdfReader(io.BytesIO(pdf)).get_fields()["City:"].get("/V") == "Waco",
    str(rep_["did_not_stick"]),
)
f6 = attach("CME Application Form 2026.pdf", "f-app")
o = json.loads(
    run(
        T.fill_form(
            json.dumps(
                {"Text7": "Review the guidelines", "Text8": "N/A", "Text13": ""}
            ),
            __user__=USER,
            __files__=[f6],
        )
    )
)
ok(
    "fill_form names the value written over and the filler",
    o.get("status") == "ok"
    and o.get("replaced_values") == {"Text7": "lijlkjkl"}
    and o.get("filler_values") == ["Text8"]
    and "filler_values" in o.get("next", ""),
    str({k: o.get(k) for k in ("status", "replaced_values", "filler_values")}),
)
ok(
    "only boxes left out are still empty",
    "Text13" not in o.get("still_empty", {}).get("page 1", [])
    and "Text14" in o.get("still_empty", {}).get("page 1", []),
    str(o.get("still_empty")),
)
o = json.loads(
    run(
        T.fill_form(
            json.dumps(
                {f"Text{n}": "x" for n in range(1, 13)}
                | {f"Text{n}": "" for n in range(13, 17)}
            ),
            __user__=USER,
            __files__=[f6],
        )
    )
)
ok(
    "nothing still empty -> no still_empty step",
    "still_empty" not in o and "still_empty" not in o.get("next", ""),
    o.get("next", "")[:120],
)
listing = json.loads(run(T.list_form_fields(__user__=USER, __files__=[f6])))
L = {e["name"]: e for e in listing["fields"]}
want = {
    "Text1": "Title of Course",
    "Text2": "Name of Sponsor",
    "Text6": "Target Audience",
    "Text12": "Name of Instructor(s)",
    "Check Box17": "Denied",
    "Check Box18": "Approved",
}
ok(
    "a label that runs up to its box is whole; a caption over a checkbox is its label",
    {k: L[k].get("label") for k in want} == want,
    str({k: L[k].get("label") for k in want}),
)
import datetime

ok(
    "the listing gives the date",
    listing.get("today") == datetime.date.today().isoformat(),
    str(listing.get("today")),
)
L = {
    e["name"]: e for e in tool.inspect_pdf_fields(f"{PROJ}/205_boc_test.pdf")["fields"]
}
ok(
    "a checkbox is labelled by the text after it, and has its tooltip",
    L["document"]["label"].startswith("This document becomes effective")
    and L["registered"]
    .get("tooltip", "")
    .startswith("If the initial registered agent"),
    f"{L['document'].get('label')!r} {L['registered'].get('tooltip')!r}"[:160],
)
blank_l = {e["name"]: e.get("label") for e in tool.inspect_pdf_fields(src)["fields"]}
held_l = {
    e["name"]: e.get("label")
    for e in tool.inspect_pdf_fields(f"{OUT}/held.pdf")["fields"]
}
ok(
    "labels are the same on a filled copy",
    held_l == blank_l,
    str({k: (blank_l[k], v) for k, v in held_l.items() if blank_l[k] != v})[:200],
)
L = {
    e["name"]: e.get("label")
    for e in tool.inspect_pdf_fields(f"{PROJ}/CME Disclosure Form Template.pdf")[
        "fields"
    ]
}
want = {
    "Check Box12": "Self, row 1",
    "Check Box13": "Self, row 2",
    "Check Box14": "Spouse/Partner, row 1",
    "Check Box15": "Spouse/Partner, row 2",
    "Nature of Financial RelationshipRow2": "Nature of Financial Relationship, row 2",
    "Check Box1": "Live",
    "Check Box7": "Presenter",
    "Text4": "Name",
}
ok(
    "a box in a table is labelled by its column heading and row",
    {k: L.get(k) for k in want} == want,
    str({k: L.get(k) for k in want if L.get(k) != want[k]}),
)
words = [
    (10, 0, 40, 10, "Spouse/"),
    (10, 11, 40, 21, "Partner"),
    (10, -30, 60, -20, "Unrelated"),
    (200, 11, 230, 21, "Other"),
]
lab = tool._widget_label(words, 10.0, pymupdf.Rect(15, 25, 33, 43))
ok(
    "a caption of two lines over a box is read whole",
    lab.get("label_above") == "Spouse/ Partner",
    str(lab),
)
T.valves.review_pages = 3

# 13 a field drawn in several differently labelled boxes is split, one field per box
T.valves.review_pages = 0  # results as text
src = f"{PROJ}/CME Broadcast Consent Template.pdf"
fb = attach("CME Broadcast Consent Template.pdf", "f-bc")
I = tool.inspect_pdf_fields(src)
L = {e["name"]: e for e in I["fields"]}
new_names = ["Text1_1", "Text1_2", "Text1_3", "Text1_4"]
ok(
    "the listing splits a shared field into one per box",
    I.get("split_fields") == {"Text1": new_names}
    and "Text1" not in L
    and all(L.get(n, {}).get("split_from") == "Text1" for n in new_names)
    and I["field_count"] == 5,
    f"{I.get('split_fields')} count={I['field_count']}",
)
want = {
    "Text1_1": "Topic of presentation",
    "Text1_2": "Date of presentation",
    "Text1_3": "Presenter Printed Name",
    "Text1_4": "Date Signed",
}
ok(
    "each split box is numbered top to bottom and keeps its own label",
    {n: L.get(n, {}).get("label") for n in want} == want,
    str({n: L.get(n, {}).get("label") for n in want}),
)
ok(
    "the listing's note explains the split",
    "split_fields" in I.get("note", "") and "split_from" in I.get("note", ""),
    I.get("note", "")[-120:],
)
I2 = tool.inspect_pdf_fields(f"{PROJ}/CME Application Form 2026.pdf")
ok(
    "boxes that share one label stay one field",
    "split_fields" not in I2
    and {e["name"] for e in I2["fields"]} >= {"Text7", "Text8", "Text9", "Text10"},
    str(I2.get("split_fields")),
)
rep_ = {}
pdf, unk = tool.fill_pdf_fields(src, {"Text1": "x"}, report=rep_)
ok(
    "the old name of a split field is unknown and the split is reported",
    unk == ["Text1"]
    and rep_["filled"] == []
    and rep_["split_fields"] == {"Text1": new_names},
    f"unknown={unk} filled={rep_['filled']} split={rep_['split_fields']}",
)
o = json.loads(
    run(T.fill_form(json.dumps({"Text1": "x"}), __user__=USER, __files__=[fb]))
)
ok(
    "fill_form with only the old name fails and points at the new names",
    o.get("status") == "failed"
    and "file_id" not in o
    and o.get("split_fields") == {"Text1": new_names}
    and "split_fields" in o.get("next", "")
    and "list_form_fields" in o.get("next", ""),
    str(o)[:200],
)
o = json.loads(
    run(
        T.fill_form(
            json.dumps({"Text1": "x", "Text1_3": "Dr. Doe"}),
            __user__=USER,
            __files__=[fb],
        )
    )
)
ok(
    "the old name beside a new one makes the fill partial, with the split first",
    o.get("status") == "partial"
    and list(o)[:4] == ["status", "ignored_unknown_fields", "split_fields", "next"]
    and o.get("filled_fields") == ["Text1_3"]
    and o.get("next", "").startswith("The fields in split_fields"),
    str(list(o)[:4]),
)
each = {
    "Text1_1": "Sepsis Update",
    "Text1_2": "10/15/2026",
    "Text1_3": "Dr. Jane Doe",
    "Text1_4": "10/04/2026",
}
rep_ = {}
pdf, unk = tool.fill_pdf_fields(src, each, report=rep_)
open(f"{OUT}/split_edit.pdf", "wb").write(pdf)
f = PdfReader(io.BytesIO(pdf)).get_fields()
ok(
    "every split box holds only its own value",
    not unk
    and {k: f[k].get("/V") for k in each} == each
    and "Text1" not in f
    and not rep_["did_not_stick"]
    and rep_["filled"] == list(each),
    f"{ {k: f.get(k, {}).get('/V') for k in each} } stuck={rep_['did_not_stick']}",
)
d = pymupdf.open(stream=pdf, filetype="pdf")
boxes = {w.field_name: w.rect for w in d[0].widgets() if w.field_name in each}
d2 = pymupdf.open(
    stream=tool.fill_pdf_fields(src, each, flatten=True)[0], filetype="pdf"
)
shown = {
    n: d2[0].get_text("text", clip=r + (-2, -2, 2, 2)).strip() for n, r in boxes.items()
}
ok(
    "flattened, each value is drawn in its own box and in no other",
    all(each[n] in shown[n] for n in each)
    and all(each[m] not in shown[n] for n in each for m in each if m != n),
    str(shown),
)
rep_ = {}
pdf2, unk = tool.fill_pdf_fields(
    f"{OUT}/split_edit.pdf", {"Text1_2": "11/01/2026"}, report=rep_
)
f = PdfReader(io.BytesIO(pdf2)).get_fields()
ok(
    "a copy that was split is not split again, and keeps its names",
    not unk
    and not rep_["split_fields"]
    and f["Text1_2"].get("/V") == "11/01/2026"
    and f["Text1_1"].get("/V") == "Sepsis Update",
    f"split={rep_['split_fields']} {f['Text1_2'].get('/V')!r}",
)
o = json.loads(run(T.fill_form(json.dumps(each), __user__=USER, __files__=[fb])))
ok(
    "fill_form with the new names is ok and reports the split",
    o.get("status") == "ok"
    and o.get("filled_fields") == list(each)
    and o.get("split_fields") == {"Text1": new_names},
    str({k: o.get(k) for k in ("status", "filled_fields", "split_fields")})[:200],
)
listing = json.loads(
    run(T.list_form_fields(file_id=o["file_id"], __user__=USER, __files__=[fb]))
)
ok(
    "the listing of the filled copy shows the new names with their values",
    "split_fields" not in listing
    and {e["name"]: e.get("value") for e in listing["fields"] if e["name"] in each}
    == each,
    str({e["name"]: e.get("value") for e in listing["fields"]})[:200],
)

# 14 text is shrunk to fit, never below the floor, and a value that still does not fit is reported
W = lambda size, rect, flags=0: types.SimpleNamespace(
    text_fontsize=size, rect=pymupdf.Rect(*rect), field_flags=flags
)
ok(
    "_fitted_size leaves a value alone when the form's size is fine",
    tool._fitted_size(W(11.0, (0, 0, 200, 20)), "hello") == (None, True),
    str(tool._fitted_size(W(11.0, (0, 0, 200, 20)), "hello")),
)
size, fits = tool._fitted_size(W(11.0, (0, 0, 100, 20)), "a fairly long value here")
ok(
    "_fitted_size shrinks a long value until it fits",
    fits and size is not None and 6.0 <= size < 11.0,
    f"{size} fits={fits}",
)
ok(
    "_fitted_size stops at the floor and says the value does not fit",
    tool._fitted_size(W(11.0, (0, 0, 100, 20)), "x" * 400) == (6.0, False)
    and tool._fitted_size(W(11.0, (0, 0, 100, 20)), "x" * 400, floor=9.0)
    == (9.0, False),
    str(tool._fitted_size(W(11.0, (0, 0, 100, 20)), "x" * 400)),
)
ML = pymupdf.PDF_TX_FIELD_IS_MULTILINE
two = (
    "Review the guidelines for sepsis management and discuss the evidence base "
    "for early antibiotics and fluids"
)
size, fits = tool._fitted_size(W(12.0, (0, 0, 367, 22), ML), two)
ok(
    "_fitted_size wraps a multiline value and shrinks it so every line fits the height",
    fits and size is not None and 6.0 <= size < 12.0,
    f"{size} fits={fits}",
)
ok(
    "_fitted_size shrinks a declared size taller than its box",
    tool._fitted_size(W(11.0, (0, 0, 100, 8)), "abc")[1]
    and tool._fitted_size(W(11.0, (0, 0, 100, 8)), "abc")[0] < 8,
    str(tool._fitted_size(W(11.0, (0, 0, 100, 8)), "abc")),
)
src = f"{PROJ}/CME Application Form 2026.pdf"
rep_ = {}
pdf, _ = tool.fill_pdf_fields(src, {"Text7": two}, report=rep_)
d = pymupdf.open(stream=pdf, filetype="pdf")
w = next(w for w in d[0].widgets() if w.field_name == "Text7")
ok(
    "a two-line value is shrunk, fits, and the size is kept in the box's own DA",
    rep_["shrunk"].get("Text7")
    and 6.0 <= rep_["shrunk"]["Text7"] < 12.0
    and not rep_["cut_off"]
    and not rep_["did_not_stick"]
    and w.text_fontsize == rep_["shrunk"]["Text7"]
    and f"{rep_['shrunk']['Text7']:g} Tf" in tool._get(d, w.xref, "DA")[1],
    f"shrunk={rep_['shrunk']} cut_off={rep_['cut_off']} DA={tool._get(d, w.xref, 'DA')[1]!r}",
)
d2 = pymupdf.open(
    stream=tool.fill_pdf_fields(src, {"Text7": two}, flatten=True)[0], filetype="pdf"
)
ok(
    "flattened, the whole two-line value sits inside its box",
    " ".join(d2[0].get_text("text", clip=w.rect + (-2, -2, 2, 2)).split()) == two,
    repr(d2[0].get_text("text", clip=w.rect + (-2, -2, 2, 2)))[:120],
)
huge = " ".join(f"word{i}" for i in range(120))
rep_ = {}
pdf, _ = tool.fill_pdf_fields(src, {"Text7": huge}, report=rep_)
d = pymupdf.open(stream=pdf, filetype="pdf")
w = next(w for w in d[0].widgets() if w.field_name == "Text7")
ok(
    "a value that cannot fit is drawn at the floor and reported as cut off",
    rep_["shrunk"] == {"Text7": 6.0}
    and rep_["cut_off"] == ["Text7"]
    and "Text7" in rep_["filled"]
    and not rep_["did_not_stick"]
    and w.text_fontsize == 6.0,
    f"shrunk={rep_['shrunk']} cut_off={rep_['cut_off']} fs={w.text_fontsize}",
)
rep_ = {}
tool.fill_pdf_fields(src, {"Text7": huge}, report=rep_, min_font_size=9.0)
ok(
    "min_font_size is the floor",
    rep_["shrunk"] == {"Text7": 9.0} and rep_["cut_off"] == ["Text7"],
    f"shrunk={rep_['shrunk']} cut_off={rep_['cut_off']}",
)
rep_ = {}
tool.fill_pdf_fields(src, {"Text7": two}, report=rep_, min_font_size=9.0)
ok(
    "a value that needs less than the floor is drawn at the floor and reported",
    rep_["shrunk"] == {"Text7": 9.0} and rep_["cut_off"] == ["Text7"],
    f"shrunk={rep_['shrunk']} cut_off={rep_['cut_off']}",
)


def ink_below(src_path, name, value, flatten=True, dpi=144):
    """Pixels that change below the box, and inside it, between the blank form and a filled copy."""
    import numpy as np

    blank = pymupdf.open(src_path)
    pg, w = next((p, w) for p in blank for w in p.widgets() if w.field_name == name)
    filled = pymupdf.open(
        stream=tool.fill_pdf_fields(src_path, {name: value}, flatten=flatten)[0],
        filetype="pdf",
    )
    k = dpi / 72
    a, b = pg.get_pixmap(dpi=dpi), filled[pg.number].get_pixmap(dpi=dpi)
    A = np.frombuffer(a.samples, dtype=np.uint8).reshape(a.h, a.w, a.n).astype(int)
    B = np.frombuffer(b.samples, dtype=np.uint8).reshape(b.h, b.w, b.n).astype(int)
    xs = slice(int(w.rect.x0 * k), int(w.rect.x1 * k))
    below = (slice(int((w.rect.y1 + 1) * k), int((w.rect.y1 + 30) * k)), xs)
    inside = (slice(int((w.rect.y0 + 1) * k), int((w.rect.y1 - 1) * k)), xs)
    diff = lambda s: int((np.abs(A[s] - B[s]).sum(axis=2) > 60).sum())
    return diff(below), diff(inside)


src = f"{PROJ}/205_boc_test.pdf"
name = "Supplemental Provisions/Information:"
below, inside = ink_below(src, name, " ".join(f"word{i}" for i in range(60)))
ok(
    "wrapped text that fits is drawn inside its box and nothing below it",
    below == 0 and inside > 1000,
    f"below={below} inside={inside}",
)
below, inside = ink_below(src, name, " ".join(f"word{i}" for i in range(900)))
ok(
    "wrapped text that is cut off still stops at the bottom of its box",
    below == 0 and inside > 1000,
    f"below={below} inside={inside}",
)
below, inside = ink_below(src, name, " ".join(f"word{i}" for i in range(900)), False)
ok(
    "the same holds for the editable copy",
    below == 0 and inside > 1000,
    f"below={below} inside={inside}",
)
o = json.loads(
    run(
        T.fill_form(
            json.dumps({"Text7": huge, "Text1": "A title"}),
            __user__=USER,
            __files__=[f6],
        )
    )
)
ok(
    "fill_form reports a cut-off value as a problem, first, and still makes the file",
    o.get("status") == "partial"
    and list(o)[:3] == ["status", "text_cut_off", "next"]
    and "shorten" in o.get("text_cut_off", {}).get("Text7", "")
    and "6.0 pt" in o.get("text_cut_off", {}).get("Text7", "")
    and o.get("file_id")
    and o.get("filled_fields") == ["Text7", "Text1"]
    and o.get("small_text_pt") == {"Text7": 6.0},
    str({k: o.get(k) for k in ("status", "text_cut_off", "small_text_pt")})[:200],
)
ok(
    "the next step says to shorten it and not to call the form complete",
    o.get("next", "").startswith("The values in text_cut_off")
    and "Shorten them" in o.get("next", "")
    and "Do not tell the user the form is complete" in o.get("next", "")
    and "Some values were NOT written" not in o.get("next", ""),
    o.get("next", "")[:160],
)
T.valves.min_font_size = 9
o = json.loads(
    run(T.fill_form(json.dumps({"Text7": huge}), __user__=USER, __files__=[f6]))
)
T.valves.min_font_size = 6.0
ok(
    "the min_font_size valve is used",
    o.get("small_text_pt") == {"Text7": 9.0}
    and "9.0 pt" in o.get("text_cut_off", {}).get("Text7", ""),
    str({k: o.get(k) for k in ("small_text_pt", "text_cut_off")})[:160],
)
o = json.loads(
    run(T.fill_form(json.dumps({"Text7": two}), __user__=USER, __files__=[f6]))
)
ok(
    "a shrunk value that fits is ok, with its size in small_text_pt",
    o.get("status") == "ok"
    and "text_cut_off" not in o
    and 6.0 <= o.get("small_text_pt", {}).get("Text7", 0) < 12.0,
    str({k: o.get(k) for k in ("status", "small_text_pt")}),
)

# 15 flatten_form: a filled copy is flattened without resending its values
o = json.loads(run(T.fill_form("{}", __user__=USER, __files__=[f5])))
ok(
    "fill_form with no values and no flatten -> error that names flatten_form",
    "non-empty" in o.get("error", "") and "flatten_form" in o.get("error", ""),
    o.get("error", "")[:120],
)
o = json.loads(run(T.fill_form("", __user__=USER, __files__=[f5])))
ok(
    "fill_form with an empty string is the same error",
    "flatten_form" in o.get("error", ""),
)
sent = {"City:": "Waco", "Street address of registered agent:": "1 Main St"}
first = json.loads(run(T.fill_form(json.dumps(sent), __user__=USER, __files__=[f5])))
before = len(STORE)
o = json.loads(
    run(T.flatten_form(file_id=first["file_id"], __user__=USER, __files__=[f5]))
)
ok(
    "flatten_form is ok, flattened, and names the copy -flattened",
    o.get("status") == "ok"
    and o.get("flattened") is True
    and o.get("file_name") == "205_boc_test-filled-flattened.pdf"
    and o.get("file_id")
    and len(STORE) == before + 1,
    str({k: o.get(k) for k in ("status", "flattened", "file_name", "error")}),
)
ok(
    "flatten_form writes no values and touches no page",
    o.get("filled_fields") == [] and o.get("pages_touched") == [],
    str({k: o.get(k) for k in ("filled_fields", "pages_touched")}),
)
if o.get("file_id"):
    r = PdfReader(STORE[o["file_id"]].path)
    txt = r.pages[0].extract_text()
    nw = sum(
        1
        for p in r.pages
        for a in (p.get("/Annots") or [])
        if a.get_object().get("/Subtype") == "/Widget"
    )
    ok(
        "the flattened copy keeps the values as page content and has no fields left",
        not r.get_fields()
        and nw == 0
        and "/AcroForm" not in r.trailer["/Root"]
        and "Waco" in txt
        and "1 Main St" in txt,
        f"fields={r.get_fields()} widgets={nw} Waco={'Waco' in txt}",
    )
ok(
    "the empty fields of a flattened copy point back at the copy before flattening",
    o.get("still_empty")
    and "City:" not in str(o.get("still_empty"))
    and "cannot be filled" in o.get("next", "")
    and f'file_id "{first["file_id"]}"' in o.get("next", "")
    and "flatten=true" in o.get("next", ""),
    o.get("next", "")[:200],
)
o = json.loads(run(T.fill_form("{}", flatten=True, __user__=USER, __files__=[f5])))
ok(
    "fill_form with {} and flatten=true flattens the attached PDF as it is",
    o.get("status") == "ok"
    and o.get("flattened") is True
    and o.get("file_name") == "205_boc_test-flattened.pdf"
    and o.get("filled_fields") == [],
    str({k: o.get(k) for k in ("status", "file_name", "error")}),
)
o = json.loads(
    run(
        T.flatten_form(
            file_id=first["file_id"], output_name="done", __user__=USER, __files__=[f5]
        )
    )
)
ok(
    "flatten_form takes an output name",
    o.get("file_name") == "done.pdf",
    o.get("file_name"),
)
o = json.loads(
    run(
        T.fill_form(
            json.dumps({"Text1": "A title"}),
            flatten=True,
            __user__=USER,
            __files__=[f6],
        )
    )
)
ok(
    "fill with flatten=true names the copy -filled and sends empties back to the original",
    o.get("status") == "ok"
    and o.get("file_name") == "CME Application Form 2026-filled.pdf"
    and o.get("filled_fields") == ["Text1"]
    and 'file_id "f-app"' in o.get("next", "")
    and "the copy before flattening" in o.get("next", ""),
    o.get("next", "")[:200],
)
o = json.loads(run(T.flatten_form(file_id="f-other", __user__=USER, __files__=[])))
ok("flatten_form refuses another user's file", "another user" in o.get("error", ""))
o = json.loads(run(T.flatten_form(__user__=USER, __files__=[f1, f5])))
ok(
    "flatten_form with several PDFs asks for file_id",
    "Several PDFs" in o.get("error", ""),
)
o = json.loads(run(T.flatten_form(__user__=USER, __files__=[f3])))
ok(
    "flatten_form on a flat PDF -> readable error",
    "no fillable fields" in o.get("error", ""),
)

# 16 the listing tells the model what it cannot fill
listing = json.loads(run(T.list_form_fields(__user__=USER, __files__=[f5])))
ok(
    "the listing says a printed line with no field cannot be filled by the tool",
    "cannot be filled by this tool" in listing.get("note", "")
    and "completed by hand" in listing.get("note", "")
    and "split_fields" not in listing.get("note", ""),
    listing.get("note", "")[-200:],
)
T.valves.review_pages = 3

n = sum(1 for r in results if r[1])
print(f"\n{TOOL}: {n}/{len(results)} passed")
[print("   FAILED:", r[0], "|", r[2]) for r in results if not r[1]]
