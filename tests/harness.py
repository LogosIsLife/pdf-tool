"""Fill every field of every form with the tool and verify the result.

Usage: python tests/harness.py <project folder> <output folder> [tool file] [folder of PDFs]

The tool file is pdf_tool.py in the project folder unless given; an absolute
path works too. The PDFs come from the project folder unless a folder is given.
"""

import sys, io, os, json, glob, subprocess, importlib.util, re
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject
import pdfplumber
import numpy as np
from PIL import Image

PROJ, OUT = sys.argv[1], sys.argv[2]
TOOL = sys.argv[3] if len(sys.argv) > 3 else "pdf_tool.py"
os.makedirs(OUT, exist_ok=True)
spec = importlib.util.spec_from_file_location("tool", os.path.join(PROJ, TOOL))
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)
SCALE = 2.0


def widgets(reader):
    out = []
    for pi, page in enumerate(reader.pages):
        for a in page.get("/Annots") or []:
            o = a.get_object()
            if o.get("/Subtype") != "/Widget":
                continue
            par = o["/Parent"].get_object() if "/Parent" in o else None
            name = (
                o.get("/T")
                if o.get("/T") is not None
                else (par.get("/T") if par is not None else None)
            )
            # qualified name
            q, node = [], o
            while node is not None:
                if node.get("/T") is not None:
                    q.append(str(node["/T"]))
                node = node["/Parent"].get_object() if "/Parent" in node else None
            ft = o.get("/FT") or (par.get("/FT") if par is not None else None)
            ff = (
                o.get("/Ff")
                if o.get("/Ff") is not None
                else (par.get("/Ff") if par is not None else 0)
            )
            out.append(
                dict(
                    page=pi,
                    obj=o,
                    parent=par,
                    name=".".join(reversed(q)),
                    ft=str(ft),
                    ff=int(ff or 0),
                    rect=sorted_rect([float(x) for x in o["/Rect"]]),
                )
            )
    return out


def sorted_rect(r):
    return [min(r[0], r[2]), min(r[1], r[3]), max(r[0], r[2]), max(r[1], r[3])]


def plan(path, split):
    """Values for every field, from pypdf's view of the form.

    `split` is the tool's {old name: [new names]} for a text field drawn in
    several differently labelled boxes. Each new name gets its own value, so
    the checks below prove every box shows only what was sent for it.
    """
    r = PdfReader(path)
    vals, skipped = {}, {}
    ws = widgets(r)
    for i, (name, f) in enumerate((r.get_fields() or {}).items()):
        ft = str(f.get("/FT"))
        ff = int(f.get("/Ff") or 0)
        low = name.lower()
        if ft == "/Tx" and name in split:
            w = [x for x in ws if x["name"] == name]
            width = min((x["rect"][2] - x["rect"][0]) for x in w) if w else 100
            ml = f.get("/MaxLen")
            for n, new in enumerate(split[name]):
                v = (
                    f"V{i}{chr(65 + n)}"
                    if width < 60
                    else f"Val{i:02d}{chr(65 + n)} Test"
                )
                vals[new] = v[: int(ml)] if ml else v
        elif ft == "/Tx":
            w = [x for x in ws if x["name"] == name]
            width = min((x["rect"][2] - x["rect"][0]) for x in w) if w else 100
            if "date" in low:
                v = "09/26/2026"
            elif "zip" in low:
                v = "77001"
            elif (
                "initial" in low
                and "mailing" not in low
                and "registered agent is" not in low
            ):
                v = "Q"
            elif "suff" in low:
                v = "Jr"
            elif low.strip().startswith("state") or low.startswith("state "):
                v = "TX"
            elif "country" in low:
                v = "USA"
            elif width < 60:
                v = f"V{i}"
            else:
                v = f"Val{i:02d} Test"
            ml = f.get("/MaxLen")
            if ml:
                v = v[: int(ml)]
            vals[name] = v
        elif ft == "/Btn":
            if ff & (1 << 16):
                skipped[name] = "push button"
                continue
            states = [str(s) for s in (f.get("/_States_") or []) if str(s) != "/Off"]
            if not states:
                for x in ws:
                    if x["name"] == name and "/AP" in x["obj"]:
                        try:
                            states += [
                                str(s)
                                for s in x["obj"]["/AP"]["/N"].keys()
                                if str(s) != "/Off"
                            ]
                        except Exception:
                            pass
            if not states:
                skipped[name] = "no on-state"
                continue
            cur = str(f.get("/V"))
            pick = next(
                (s for s in states if s != cur), states[0]
            )  # prefer a change from the current value
            vals[name] = pick
        elif ft == "/Ch":
            opts = f.get("/Opt") or []
            if opts:
                vals[name] = str(opts[0][1] if isinstance(opts[0], list) else opts[0])
            else:
                skipped[name] = "choice without options"
        else:
            skipped[name] = f"type {ft}"
    return vals, skipped


def strip_annots(src_bytes):
    w = PdfWriter(clone_from=PdfReader(io.BytesIO(src_bytes)))
    for p in w.pages:
        if "/Annots" in p:
            del p["/Annots"]
    if "/AcroForm" in w._root_object:
        del w._root_object["/AcroForm"]
    b = io.BytesIO()
    w.write(b)
    return b.getvalue()


def render_gs(pdf_bytes, tag):
    p = f"{OUT}/{tag}.pdf"
    open(p, "wb").write(pdf_bytes)
    subprocess.run(
        [
            "gs",
            "-q",
            "-dNOPAUSE",
            "-dBATCH",
            "-sDEVICE=png16m",
            f"-r{int(72 * SCALE)}",
            f"-sOutputFile={OUT}/{tag}_gs_p%d.png",
            p,
        ],
        check=True,
        capture_output=True,
    )
    n = len(PdfReader(io.BytesIO(pdf_bytes)).pages)
    return [f"{OUT}/{tag}_gs_p{i + 1}.png" for i in range(n)]


def render_quartz(pdf_bytes, tag):
    """sips only draws page 1, so write one single-page document per page (AcroForm kept)."""
    n = len(PdfReader(io.BytesIO(pdf_bytes)).pages)
    outs = []
    for i in range(n):
        w = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_bytes)))
        for j in reversed(range(n)):
            if j != i:
                w.remove_page(j)
        width = float(w.pages[0].mediabox.width)
        p = f"{OUT}/{tag}_qz_p{i + 1}.pdf"
        w.write(p)
        png = f"{OUT}/{tag}_qz_p{i + 1}.png"
        subprocess.run(
            [
                "sips",
                "-s",
                "format",
                "png",
                "--resampleWidth",
                str(int(width * SCALE)),
                p,
                "--out",
                png,
            ],
            check=True,
            capture_output=True,
        )
        outs.append(png)
    return outs


_GRAY = {}


def gray(path):
    """Grayscale on a white ground; Quartz PNGs are transparent where the page is blank."""
    if path not in _GRAY:
        im = Image.open(path).convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        _GRAY[path] = np.asarray(
            Image.alpha_composite(bg, im).convert("L"), dtype=np.int16
        )
    return _GRAY[path]


def ink(img_a, img_b, rect, page_h, pad=1.0):
    """Count pixels inside rect that differ noticeably between two renders."""
    a = gray(img_a)
    b = gray(img_b)
    h = min(a.shape[0], b.shape[0])
    w = min(a.shape[1], b.shape[1])
    s = a.shape[1] / (612.0 if False else 1)  # placeholder
    sx = a.shape[1] / PAGE_W[0]
    sy = a.shape[0] / page_h
    x1, y1, x2, y2 = rect
    px1, px2 = int(max(0, (x1 - pad) * sx)), int(min(w, (x2 + pad) * sx))
    py1, py2 = (
        int(max(0, (page_h - y2 - pad) * sy)),
        int(min(h, (page_h - y1 + pad) * sy)),
    )
    if px2 <= px1 or py2 <= py1:
        return 0
    return int((np.abs(a[py1:py2, px1:px2] - b[py1:py2, px1:px2]) > 60).sum())


PAGE_W = [612.0]


def check_form(path):
    tag = re.sub(r"[^A-Za-z0-9]+", "_", os.path.splitext(os.path.basename(path))[0])
    res = dict(form=os.path.basename(path), tag=tag, problems=[], notes=[])
    try:
        split = tool.inspect_pdf_fields(path).get("split_fields") or {}
    except Exception:
        split = {}
    if split:
        res["split_fields"] = split
    vals, skipped = plan(path, split)
    res["planned"] = len(vals)
    res["skipped"] = skipped
    if not vals:
        try:
            pdf, unk = tool.fill_pdf_fields(path, {"x": "y"}, flatten=False)
            res["notes"].append(
                f"no fillable fields; fill call returned {len(pdf)} bytes, unknown={unk}"
            )
        except ValueError as e:
            res["notes"].append(f"no fillable fields; tool refused cleanly: {e}")
        except Exception as e:
            res["problems"].append(f"fill on field-less PDF raised {e!r}")
        return res
    json.dump(vals, open(f"{OUT}/{tag}_values.json", "w"), indent=1)

    # ---------- editable fill ----------
    pdf, unk = tool.fill_pdf_fields(path, vals, flatten=False)
    if unk:
        res["problems"].append(f"unknown fields reported: {unk}")
    r2 = PdfReader(io.BytesIO(pdf))
    f2 = r2.get_fields() or {}
    res["fields_after"] = len(f2)
    res["fields_before"] = len(PdfReader(path).get_fields() or {})
    # A split field becomes one field per box, so the count may grow by that much.
    expected = res["fields_before"] + sum(len(v) - 1 for v in split.values())
    if res["fields_after"] != expected:
        res["problems"].append(
            f"field count changed: {res['fields_before']} before, {res['fields_after']} after, expected {expected}"
        )
    for old, news in split.items():
        if old in f2:
            res["problems"].append(f"split field {old!r} still present")
        gone = [n for n in news if n not in f2]
        if gone:
            res["problems"].append(f"split field {old!r}: new fields missing {gone}")
    ws = widgets(r2)
    byname = {}
    for w in ws:
        byname.setdefault(w["name"], []).append(w)
    ok_val = 0
    stale = []
    for k, v in vals.items():
        got = f2.get(k, {}).get("/V")
        if str(got) == v:
            ok_val += 1
        else:
            stale.append(f"{k}={got!r}")
    res["tree_values_ok"] = f"{ok_val}/{len(vals)}"
    if stale:
        res["problems"].append(
            f"FIELD TREE not updated for {len(stale)} field(s), e.g. {stale[:4]}"
        )
    okw = 0
    badw = []
    for k, v in vals.items():
        ww = byname.get(k, [])

        def wv(w):
            o, par = w["obj"], w["parent"]
            return (
                o.get("/V")
                if o.get("/V") is not None
                else (par.get("/V") if par is not None else None)
            )

        if ww and all(
            str(wv(w)) == v
            or (w["ft"] == "/Btn" and str(w["obj"].get("/AS")) in (v, "/Off"))
            for w in ww
        ):
            okw += 1
        else:
            badw.append(k)
    res["page_values_ok"] = f"{okw}/{len(vals)}"
    if badw:
        res["problems"].append(f"on-page value wrong for {badw[:6]}")
    # structural checks per widget
    btn_ok = btn_n = tx_ok = tx_n = 0
    for k, v in vals.items():
        for w in byname.get(k, []):
            o, par = w["obj"], w["parent"]
            if w["ft"] == "/Btn":
                btn_n += 1
                node = (
                    par
                    if (par is not None and par.get("/FT") == "/Btn" and "/T" not in o)
                    else o
                )
                fv = node.get("/V")
                states = [str(s) for s in o["/AP"]["/N"].keys()] if "/AP" in o else []
                want_as = v if v in states else "/Off"
                good = (
                    isinstance(fv, NameObject)
                    and str(fv) == v
                    and str(o.get("/AS")) == want_as
                )
                if good:
                    btn_ok += 1
                else:
                    res["problems"].append(
                        f"button {k!r}: /V {fv!r} ({type(fv).__name__}) /AS {o.get('/AS')} want {v} / {want_as}"
                    )
            elif w["ft"] == "/Tx":
                tx_n += 1
                ap = o.get("/AP")
                data = b""
                try:
                    data = ap["/N"].get_object().get_data()
                except Exception:
                    pass
                if (
                    v.encode("latin1", "ignore") in data
                    or v.encode("utf-16-be") in data
                ):
                    tx_ok += 1
                else:
                    res["problems"].append(
                        f"text {k!r}: appearance stream lacks value ({len(data)} bytes)"
                    )
    res["button_widgets"] = f"{btn_ok}/{btn_n}"
    res["text_widgets"] = f"{tx_ok}/{tx_n}"
    same_on = {}
    for k, v in vals.items():
        on = [
            w
            for w in byname.get(k, [])
            if w["ft"] == "/Btn" and str(w["obj"].get("/AS")) != "/Off"
        ]
        if len(byname.get(k, [])) > 1 and byname[k][0]["ft"] == "/Btn" and len(on) != 1:
            res["problems"].append(f"radio {k!r}: {len(on)} widgets on")
    multi = {k: len(v) for k, v in byname.items() if len(v) > 1 and v[0]["ft"] == "/Tx"}
    if multi:
        res["notes"].append(
            f"text fields shared by several boxes (one value shows in all): {multi}"
        )

    # ---------- renders of editable fill ----------
    base = strip_annots(pdf)
    rend = {}
    for eng, fn in (("qz", render_quartz), ("gs", render_gs)):
        try:
            rend[eng] = (fn(pdf, f"{tag}_edit"), fn(base, f"{tag}_base"))
        except Exception as e:
            res["problems"].append(f"{eng} render failed: {e!r}")
    for eng, (fimgs, bimgs) in rend.items():
        drawn = total = 0
        missing = []
        for k, v in vals.items():
            for w in byname.get(k, []):
                page = r2.pages[w["page"]]
                PAGE_W[0] = float(page.mediabox.width)
                ph = float(page.mediabox.height)
                total += 1
                n = ink(fimgs[w["page"]], bimgs[w["page"]], w["rect"], ph)
                expect_on = not (
                    w["ft"] == "/Btn" and str(w["obj"].get("/AS")) == "/Off"
                )
                if (n >= 12) == expect_on:
                    drawn += 1
                else:
                    missing.append(
                        f"{k} (p{w['page'] + 1}, {n}px, expected {'mark' if expect_on else 'blank'})"
                    )
        res[f"drawn_{eng}"] = f"{drawn}/{total}"
        if missing:
            res["problems"].append(
                f"{eng}: wrong appearance in {len(missing)} box(es): {missing[:12]}"
            )

    # ---------- whole document versus page by page ----------
    # Ghostscript (used by many print systems) renders pages in order and keeps
    # fonts between pages. Output that is fine one page at a time can lose text
    # on later pages. Compare both ways of rendering the same file.
    try:
        whole = render_gs(pdf, f"{tag}_whole")
        bad_pages = []
        for i, wpath in enumerate(whole):
            one = f"{OUT}/{tag}_single_p{i + 1}.png"
            subprocess.run(
                [
                    "gs",
                    "-q",
                    "-dNOPAUSE",
                    "-dBATCH",
                    "-sDEVICE=png16m",
                    f"-r{int(72 * SCALE)}",
                    f"-dFirstPage={i + 1}",
                    f"-dLastPage={i + 1}",
                    f"-sOutputFile={one}",
                    f"{OUT}/{tag}_whole.pdf",
                ],
                check=True,
                capture_output=True,
            )
            a1, b1 = gray(wpath), gray(one)
            diff = int((np.abs(a1 - b1) > 60).sum()) if a1.shape == b1.shape else -1
            if diff != 0:
                bad_pages.append(f"p{i + 1}: {diff}px")
        res["gs_whole_document"] = (
            f"{len(whole) - len(bad_pages)}/{len(whole)} pages match"
        )
        if bad_pages:
            res["problems"].append(
                f"Ghostscript renders pages differently in a whole-document run: {bad_pages}"
            )
    except Exception as e:
        res["problems"].append(f"whole-document check failed: {e!r}")

    # ---------- flattened fill ----------
    fpdf, _ = tool.fill_pdf_fields(path, vals, flatten=True)
    open(f"{OUT}/{tag}_flat.pdf", "wb").write(fpdf)
    rf = PdfReader(io.BytesIO(fpdf))
    nw = sum(
        1
        for p in rf.pages
        for a in (p.get("/Annots") or [])
        if a.get_object().get("/Subtype") == "/Widget"
    )
    if "/AcroForm" in rf.trailer["/Root"] or nw:
        res["problems"].append(f"flatten left AcroForm/widgets ({nw})")
    # Boxes are taken from the editable copy: same places as the original,
    # but under the new names when a field was split.
    srcby = byname
    placed = total = 0
    misplaced = []
    dups = {}
    with pdfplumber.open(io.BytesIO(fpdf)) as pl:
        for k, v in vals.items():
            for w in srcby.get(k, []):
                if w["ft"] != "/Tx":
                    continue
                total += 1
                pg = pl.pages[w["page"]]
                H = float(pg.height)
                x1, y1, x2, y2 = w["rect"]
                box = (
                    max(0, x1 - 2),
                    max(0, H - y2 - 2),
                    min(float(pg.width), x2 + 2),
                    min(H, H - y1 + 2),
                )
                crop = pg.crop(box)
                dup = len(crop.chars) - len(crop.dedupe_chars().chars)
                if dup:
                    dups[k] = dup
                txt = "".join(
                    c["text"]
                    for c in sorted(
                        crop.dedupe_chars().chars,
                        key=lambda c: (round(c["top"]), c["x0"]),
                    )
                    if c["text"] not in "_ "
                )
                if v.replace(" ", "") in txt:
                    placed += 1
                else:
                    misplaced.append(
                        f"{k} (p{w['page'] + 1}) wanted {v!r} found {txt[:40]!r}"
                    )
    res["flat_text_in_box"] = f"{placed}/{total}"
    if dups:
        res["notes"].append(
            f"flatten drew the same text twice on top of itself for: {dups}"
        )
    if misplaced:
        res["problems"].append(
            f"flatten: text not inside its box for {len(misplaced)}: {misplaced[:12]}"
        )
    try:
        fb = render_gs(fpdf, f"{tag}_flat")
        bb = rend["gs"][1] if "gs" in rend else render_gs(base, f"{tag}_base")
        render_quartz(fpdf, f"{tag}_flat")
        d = t = 0
        miss = []
        for k, v in vals.items():
            for w in srcby.get(k, []):
                if w["ft"] != "/Btn":
                    continue
                page = rf.pages[w["page"]]
                PAGE_W[0] = float(page.mediabox.width)
                t += 1
                n = ink(
                    fb[w["page"]], bb[w["page"]], w["rect"], float(page.mediabox.height)
                )
                states = (
                    [str(x) for x in w["obj"]["/AP"]["/N"].keys()]
                    if "/AP" in w["obj"]
                    else []
                )
                expect_on = v in states
                if (n >= 12) == expect_on:
                    d += 1
                else:
                    miss.append(
                        f"{k} (p{w['page'] + 1}, {n}px, expected {'mark' if expect_on else 'blank'})"
                    )
        res["flat_checks_drawn"] = f"{d}/{t}"
        if miss:
            res["problems"].append(f"flatten: wrong check mark state for {miss[:12]}")
    except Exception as e:
        res["problems"].append(f"flat render failed: {e!r}")
    return res


results = []
FORMS = (
    sys.argv[4] if len(sys.argv) > 4 else PROJ
)  # optional: a different folder of PDFs to test
for f in sorted(glob.glob(f"{FORMS}/*.pdf")):
    try:
        results.append(check_form(f))
    except Exception as e:
        import traceback

        results.append(
            dict(
                form=os.path.basename(f),
                problems=[f"harness crashed: {e!r}", traceback.format_exc()[-600:]],
            )
        )
json.dump(results, open(f"{OUT}/results.json", "w"), indent=1, default=str)
for r in results:
    print("=" * 8, r["form"])
    for k in (
        "planned",
        "fields_before",
        "fields_after",
        "tree_values_ok",
        "page_values_ok",
        "button_widgets",
        "text_widgets",
        "drawn_qz",
        "drawn_gs",
        "gs_whole_document",
        "flat_text_in_box",
        "flat_checks_drawn",
        "split_fields",
        "skipped",
    ):
        if k in r:
            print(f"   {k:18} {r[k]}")
    for n in r.get("notes", []):
        print("   NOTE   ", n)
    for p in r.get("problems", []):
        print("   PROBLEM", p)
