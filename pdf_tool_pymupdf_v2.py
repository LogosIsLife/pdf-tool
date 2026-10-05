"""
title: PDF Form Filler (PyMuPDF)
author: darlene
version: 1.3.0
license: AGPL-3.0
description: Inspect and fill the form fields (AcroForm) of a PDF attached to the chat, return the filled PDF as a downloadable attachment, and show a page to the model as an image.
"""

# Paste this whole file into Open WebUI: Admin > Tools > + > paste > Save.
# PyMuPDF must be provided by the open-webui systemd unit (--with pymupdf); a
# `requirements:` line is deliberately absent because that environment has
# no pip and the install step would fail.
# Then enable the tool for the model (Admin > Models > model > Tools) or
# toggle it in the chat's "+" menu.
#
# LICENSE NOTE: PyMuPDF is AGPL-3.0 (or commercial, from Artifex). Running it
# behind a hosted service can oblige you to offer the source to its users.
# pdf_tool_v4.py is the same tool on pypdf (BSD) if that is a problem.
#
# How it works: Open WebUI keeps the original uploaded PDF in its uploads
# folder even though Docling converts a copy to Markdown for retrieval. The
# tool receives the chat's attachments as __files__, opens the original with
# PyMuPDF, and reads or writes the real form fields. The filled copy is saved
# through Open WebUI's own file store so it appears as a chat attachment.

import base64
import io
import json
import os
import re
import uuid
from datetime import date
from typing import Any, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------
# Pure PyMuPDF helpers (no Open WebUI imports, so they can be tested standalone)
# --------------------------------------------------------------------------


def _mu():
    """The PyMuPDF module, under its current name or its older one."""
    try:
        import pymupdf

        return pymupdf
    except ImportError:
        import fitz

        return fitz


def _clean_label(text: str, limit: int = 90) -> str:
    """Collapse fill-in underscores and whitespace out of a printed label."""
    text = re.sub(r"_+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" :,.-“”\"'")
    return text[:limit]


def _as_bool(value: Any, default: bool = False) -> bool:
    """Strict truthiness for tool arguments.

    The model may send a real bool, a number, or the strings "true"/"false".
    Anything unset or unrecognised falls back to default. Plain bool() is not
    safe here: bool("false") is True, which is how a filled form once got
    flattened without anyone asking for it.
    """
    if value is None:
        return default
    if isinstance(value, (bool, int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes"):
            return True
        if v in ("false", "0", "no", ""):
            return False
    return default


def _open_document(source):
    """Open a PDF, turning library errors into messages a user can act on."""
    mu = _mu()
    try:
        if isinstance(source, (bytes, bytearray)):
            doc = mu.open(stream=bytes(source), filetype="pdf")
        else:
            doc = mu.open(source)
        if not doc.is_pdf:
            raise ValueError("This file is not a PDF.")
        if doc.needs_pass and not doc.authenticate(""):
            raise ValueError(
                "This PDF is password-protected. Remove the password (open it and save an unprotected copy) and attach it again."
            )
        doc.page_count  # forces the page tree to load, so damage shows up here
        doc.pdf_catalog()
        return doc
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(
            f"This PDF could not be read; the file appears to be damaged ({type(e).__name__}: {e})."
        )


# ---- low-level object access ------------------------------------------------
#
# PyMuPDF's widget API edits the boxes that sit on a page. The form's own
# structure (the field tree) is only reachable through these xref calls.

_REF = re.compile(r"(\d+)\s+0\s+R")
_NUM = re.compile(r"-?\d*\.?\d+")
_HEX = re.compile(r"#([0-9A-Fa-f]{2})")


def _decode_name(name: str) -> str:
    """'is#20an#20organization' -> 'is an organization' (PDF names escape spaces)."""
    return _HEX.sub(lambda m: chr(int(m.group(1), 16)), str(name))


def _pdf_name(name: str) -> str:
    """A PDF name ready to be written, with its slash and escapes."""
    text = _decode_name(str(name).lstrip("/"))
    out = []
    for ch in text:
        if ch.isalnum() and ord(ch) < 128 or ch in "-_.+*":
            out.append(ch)
        else:
            out.append("".join(f"#{b:02X}" for b in ch.encode("utf-8")))
    return "/" + "".join(out)


def _get(doc, xref: int, key: str) -> tuple[str, str]:
    try:
        return doc.xref_get_key(xref, key)
    except Exception:
        return ("null", "null")


def _refs(text: str) -> list[int]:
    return [int(n) for n in _REF.findall(text or "")]


def _read_array(doc, xref: int, key: str):
    """The object numbers held in an array, whether stored inline or on its own.

    Returns (items, where) or (None, None). `where` is handed to _write_array.
    """
    kind, value = _get(doc, xref, key)
    if kind == "array":
        return _refs(value), ("key", xref, key)
    if kind == "xref":
        target = _refs(value)
        if not target:
            return None, None
        try:
            body = doc.xref_object(target[0], compressed=True)
        except Exception:
            return None, None
        if body.lstrip().startswith("["):
            return _refs(body), ("object", target[0], "")
    return None, None


def _write_array(doc, where, items: list[int]) -> None:
    text = "[" + " ".join(f"{i} 0 R" for i in items) + "]"
    if where[0] == "object":
        doc.update_object(where[1], text)
    else:
        doc.xref_set_key(where[1], where[2], text)


def _inherited(doc, xref: int, key: str) -> tuple[str, str]:
    """A key's value on this object or the nearest parent that has it."""
    node, hops = xref, 0
    while node and hops < 16:
        kind, value = _get(doc, node, key)
        if kind != "null":
            return kind, value
        pk, pv = _get(doc, node, "Parent")
        node = _refs(pv)[0] if pk == "xref" and _refs(pv) else 0
        hops += 1
    return ("null", "null")


def _rect_of(doc, xref: int):
    kind, value = _get(doc, xref, "Rect")
    if kind != "array":
        return None
    nums = [float(n) for n in _NUM.findall(value)]
    if len(nums) != 4:
        return None
    return (
        min(nums[0], nums[2]),
        min(nums[1], nums[3]),
        max(nums[0], nums[2]),
        max(nums[1], nums[3]),
    )


def _page_widget_xrefs(doc) -> list[tuple[int, int]]:
    """(page number, object number) of every form box listed on a page."""
    out = []
    for pno in range(doc.page_count):
        items, _ = _read_array(doc, doc.page_xref(pno), "Annots")
        for x in items or []:
            if _get(doc, x, "Subtype")[1] == "/Widget":
                out.append((pno, x))
    return out


def _form_location(doc, create: bool = False):
    """Where the form dictionary lives: (object number, key prefix) or None."""
    cat = doc.pdf_catalog()
    kind, value = _get(doc, cat, "AcroForm")
    if kind == "xref" and _refs(value):
        return _refs(value)[0], ""
    if kind == "dict":
        return cat, "AcroForm/"
    if not create:
        return None
    new = doc.get_new_xref()
    doc.update_object(new, "<</Fields[]>>")
    doc.xref_set_key(cat, "AcroForm", f"{new} 0 R")
    return new, ""


def _relink_detached_widgets(doc) -> int:
    """Make the field tree point at the boxes that are actually on the pages.

    Apple's Preview saves a form with two copies of every box: one listed on
    the page (/Annots) and another listed in the form's field tree
    (/AcroForm /Fields or a parent's /Kids). Filling updates the page copy,
    while Preview and field readers look at the tree copy, so a fill looks
    empty in Preview and reads back with the old values. Swap each stale tree
    copy for the page box with the same name and position. Also builds the
    form dictionary when boxes exist without one (PDFs that were merged, split
    or re-saved by a tool that copied the pages but not the form).
    Returns the number of boxes relinked.
    """
    page_widgets = [x for _, x in _page_widget_xrefs(doc)]
    if not page_widgets:
        return 0
    named = [
        x
        for x in page_widgets
        if _get(doc, x, "T")[0] != "null" or _get(doc, x, "Parent")[0] == "xref"
    ]
    if not named:
        return 0
    form = _form_location(doc, create=True)
    form_xref, prefix = form
    if _get(doc, form_xref, prefix + "Fields")[0] == "null":
        doc.xref_set_key(form_xref, prefix + "Fields", "[]")

    arrays: dict[tuple, list[int]] = {}
    where_of: dict[tuple, Any] = {}
    slots: list[tuple[tuple, int, int]] = []
    seen: set[int] = set()

    def load(holder: int, key: str):
        ident = (holder, key)
        if ident not in arrays:
            items, where = _read_array(doc, holder, key)
            if items is None:
                return None
            arrays[ident] = items
            where_of[ident] = where
        return ident

    def walk(holder: int, key: str):
        ident = load(holder, key)
        if ident is None:
            return
        for idx, x in enumerate(arrays[ident]):
            if x in seen:
                continue
            seen.add(x)
            slots.append((ident, idx, x))
            if _get(doc, x, "Kids")[0] in ("array", "xref"):
                walk(x, "Kids")

    walk(form_xref, prefix + "Fields")
    fields_ident = (form_xref, prefix + "Fields")
    in_tree = {x for _, _, x in slots}
    on_page = set(page_widgets)
    changed: set[tuple] = set()
    used: set[tuple] = set()

    def close(a, b, tol=2.0):
        return (
            a is not None
            and b is not None
            and all(abs(p - q) <= tol for p, q in zip(a, b))
        )

    relinked = 0
    for wx in page_widgets:
        if wx in in_tree:
            continue
        wrect = _rect_of(doc, wx)
        pk, pv = _get(doc, wx, "Parent")
        if pk == "xref" and _refs(pv):
            parent = _refs(pv)[0]
            ident = load(parent, "Kids")
            if ident is None:
                doc.xref_set_key(parent, "Kids", "[]")
                ident = load(parent, "Kids")
            if ident is None:
                continue
            cands = [
                (i, k)
                for i, k in enumerate(arrays[ident])
                if k not in on_page and (ident, i) not in used
            ]
            match = next(
                (c for c in cands if close(_rect_of(doc, c[1]), wrect)), None
            ) or (cands[0] if cands else None)
            if match is None:
                arrays[ident].append(wx)
            else:
                arrays[ident][match[0]] = wx
                used.add((ident, match[0]))
            changed.add(ident)
            relinked += 1
            continue
        name = _get(doc, wx, "T")
        if name[0] == "null":
            continue
        cands = [
            (ident, i, x)
            for ident, i, x in slots
            if x not in on_page
            and (ident, i) not in used
            and _get(doc, x, "T") == name
            and _get(doc, x, "Kids")[0] == "null"
            and _get(doc, x, "Parent")[0] == "null"
        ]
        match = next((c for c in cands if close(_rect_of(doc, c[2]), wrect)), None) or (
            cands[0] if cands else None
        )
        if match is None:
            arrays[fields_ident].append(wx)
            changed.add(fields_ident)
        else:
            arrays[match[0]][match[1]] = wx
            used.add((match[0], match[1]))
            changed.add(match[0])
        relinked += 1

    # Every box must be reachable from /Fields through its topmost ancestor.
    for wx in page_widgets:
        top, hops = wx, 0
        while hops < 16:
            pk, pv = _get(doc, top, "Parent")
            if pk != "xref" or not _refs(pv):
                break
            top = _refs(pv)[0]
            hops += 1
        if top not in arrays[fields_ident] and _get(doc, top, "T")[0] != "null":
            arrays[fields_ident].append(top)
            changed.add(fields_ident)
            relinked += 1

    for ident in changed:
        _write_array(doc, where_of[ident], arrays[ident])
    return relinked


def _prepared(source):
    """Open a PDF with its form structure repaired and ready for the widget API."""
    doc = _open_document(source)
    try:
        if _relink_detached_widgets(doc):
            # Reload so PyMuPDF rebuilds its view of the form from the repaired tree.
            data = doc.tobytes(garbage=0)
            doc.close()
            doc = _open_document(data)
    except ValueError:
        raise
    except Exception:
        pass
    return doc


# ---- field listing -----------------------------------------------------------


def _kind(widget) -> str:
    mu = _mu()
    t = widget.field_type
    if t == mu.PDF_WIDGET_TYPE_TEXT:
        return "text"
    if t in (mu.PDF_WIDGET_TYPE_CHECKBOX, mu.PDF_WIDGET_TYPE_RADIOBUTTON):
        return "checkbox_or_radio"
    if t in (mu.PDF_WIDGET_TYPE_COMBOBOX, mu.PDF_WIDGET_TYPE_LISTBOX):
        return "choice"
    if t == mu.PDF_WIDGET_TYPE_SIGNATURE:
        return "signature"
    if t == mu.PDF_WIDGET_TYPE_BUTTON:
        return "push_button"
    return "unknown"


def _on_state(widget) -> Optional[str]:
    """The option name a checkbox or radio box switches to, with its slash."""
    try:
        state = widget.on_state()
    except Exception:
        state = None
    if not state or state is True or str(state) == "Off":
        return None
    return "/" + _decode_name(str(state).lstrip("/"))


def _choices(widget) -> list[tuple[str, str]]:
    """(stored value, shown text) for each option of a dropdown or list."""
    out = []
    for c in widget.choice_values or []:
        if isinstance(c, (list, tuple)) and len(c) >= 2:
            out.append((str(c[0]), str(c[1])))
        else:
            out.append((str(c), str(c)))
    return out


def _widget_label(page_words: list, body_h: float, rect) -> dict[str, str]:
    """The text printed beside, under, or after one box.

    left  - words on the box's own line, ending just before it ("Period: ___")
    below - a smaller-font caption under a signature-style line
            ("Assignor Entity Name"), used only when there is no left label
    right - text after the box: primary for a checkbox/radio square
            ("[ ] a. The right to..."), a fallback for other boxes
    above - a caption printed over the box, used for a checkbox/radio square
            that has no other label ("Denied" over its square)
    A box inside a table gets its column heading from _table_labels instead.
    """

    def phrases(words, gap=6.0):
        words = sorted(words, key=lambda w: w[0])
        out, cur = [], []
        for w in words:
            if cur and w[0] - cur[-1][2] > gap:
                out.append(cur)
                cur = []
            cur.append(w)
        if cur:
            out.append(cur)
        return [" ".join(w[4] for w in ph) for ph in out]

    x1, top, x2, bottom = rect.x0, rect.y0, rect.x1, rect.y1
    h = bottom - top
    mid = (top + bottom) / 2
    on_line = [w for w in page_words if abs((w[1] + w[3]) / 2 - mid) <= h * 0.6]
    # A label may run up to the box or a little into it ("Sponsor:|___").
    left = phrases(
        [w for w in on_line if w[2] <= x1 + 3 and w[0] < x1 - 1 and w[0] >= x1 - 300]
    )
    right = phrases([w for w in on_line if w[0] >= x2 + 1 and w[0] <= x2 + 300])
    below = phrases(
        [
            w
            for w in page_words
            if bottom + 0.5 <= w[1] <= bottom + 13
            and x1 - 6 <= w[0] <= x2 - 6
            and (w[3] - w[1]) <= body_h * 0.85
        ]
    )
    over = [
        w for w in page_words if w[3] <= top + 1 and w[2] >= x1 - 6 and w[0] <= x2 + 6
    ]
    above: list[str] = []
    edge, reach = top, 16.0
    for _ in range(3):
        # The nearest line, then the lines stacked right on it ("Spouse/" over "Partner").
        line = [w for w in over if edge - reach <= w[3] <= edge + 1]
        if not line:
            break
        near = max(w[3] for w in line)
        line = [w for w in line if near - w[3] <= 3]
        above = phrases(line) + above
        edge, reach = min(w[1] for w in line), 4.0
        over = [w for w in over if w[3] <= edge + 1 and w not in line]
    entry: dict[str, str] = {}
    if left:
        entry["label_left"] = _clean_label(left[-1])
    if below:
        entry["label_below"] = _clean_label(" ".join(below))
    if right:
        entry["label_right"] = _clean_label(right[0])
    if above:
        entry["label_above"] = _clean_label(" ".join(above))
    return entry


def _table_labels(page, widgets) -> dict[int, str]:
    """For each box inside a table, its column heading and row: {xref: "Self, row 2"}.

    Only a fallback for a box with no text beside it: a ruled frame around a
    block of boxes also passes for a table, and its heading is not a label.
    """
    mu = _mu()
    try:
        if hasattr(mu, "no_recommend_layout"):
            mu.no_recommend_layout()
        tables = page.find_tables().tables
    except Exception:
        return {}
    out: dict[int, str] = {}
    for table in tables:
        try:
            names = [
                _clean_label(str(n or "").replace("/\n", "/"))
                for n in table.header.names
            ]
            rows = list(table.rows)
            body = rows if table.header.external else rows[1:]
        except Exception:
            continue
        for number, row in enumerate(body, start=1):
            for column, cell in enumerate(row.cells):
                if cell is None or column >= len(names) or not names[column]:
                    continue
                x0, y0, x1, y1 = cell
                for w in widgets:
                    r = w.rect
                    if x0 <= (r.x0 + r.x1) / 2 <= x1 and y0 <= (r.y0 + r.y1) / 2 <= y1:
                        out[w.xref] = names[column] + (
                            f", row {number}" if len(body) > 1 else ""
                        )
    return out


def _printed_words(page, widgets) -> list:
    """The words printed on the page, without the values typed into its boxes.

    The page text includes what the boxes show, and a value ("Austin") would
    pass for the label of the box next to it.
    """
    mu = _mu()
    typed = (
        mu.PDF_WIDGET_TYPE_TEXT,
        mu.PDF_WIDGET_TYPE_COMBOBOX,
        mu.PDF_WIDGET_TYPE_LISTBOX,
    )
    filled = [
        w.rect
        for w in widgets
        if w.field_type in typed and w.field_value not in (None, "", [])
    ]
    out = []
    for w in page.get_text("words"):
        if set(w[4]) <= set("_.-"):
            continue
        x, y = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
        if any(r.x0 <= x <= r.x1 and r.y0 <= y <= r.y1 for r in filled):
            continue
        out.append(w)
    return out


def _collect(doc) -> dict[str, list[dict[str, Any]]]:
    """Every box of every field, in page order, with what is needed to list it."""
    out: dict[str, list[dict[str, Any]]] = {}
    for page in doc:
        try:
            widgets = list(page.widgets())
        except Exception:
            widgets = []
        if not widgets:
            continue
        try:
            words = _printed_words(page, widgets)
        except Exception:
            words = []
        heights = sorted(w[3] - w[1] for w in words) or [10.0]
        body_h = heights[len(heights) // 2]
        in_table: Optional[dict[int, str]] = None
        for w in widgets:
            name = w.field_name
            if not name:
                continue
            info: dict[str, Any] = {
                "page": page.number + 1,
                "kind": _kind(w),
                "value": w.field_value,
                "xref": w.xref,
            }
            try:
                info.update(_widget_label(words, body_h, w.rect))
            except Exception:
                pass
            if not any(
                info.get(k) for k in ("label_left", "label_below", "label_right")
            ):
                if in_table is None:
                    in_table = _table_labels(page, widgets)
                if in_table.get(w.xref):
                    info["label_table"] = in_table[w.xref]
            tip = _inherited(doc, w.xref, "TU")
            if tip[0] == "string" and _clean_label(tip[1]):
                info["tooltip"] = _clean_label(tip[1])
            if info["kind"] == "checkbox_or_radio":
                info["on_value"] = _on_state(w)
                info["radio"] = w.field_type == _mu().PDF_WIDGET_TYPE_RADIOBUTTON
            elif info["kind"] == "choice":
                info["choices"] = _choices(w)
            out.setdefault(str(name), []).append(info)
    return out


def inspect_pdf_fields(path: str) -> dict[str, Any]:
    """Return the form fields of a PDF: name, type, printed label, value, options."""
    doc = _prepared(path)
    try:
        boxes = _collect(doc)
        pages = doc.page_count
    finally:
        doc.close()

    def _label(b):
        return (
            b.get("label_left")
            or b.get("label_below")
            or b.get("label_right")
            or b.get("label_table")
            or ""
        )

    def _box_label(b):
        # A checkbox or radio square: its text follows it; "A." before it is only a marker.
        return (
            b.get("label_right")
            or b.get("label_left")
            or b.get("label_below")
            or b.get("label_table")
            or b.get("label_above")
            or ""
        )

    out = []
    for name, group in boxes.items():
        kind = group[0]["kind"]
        if kind == "push_button":
            # Push buttons ("Print Form", "Reset Form") hold no value.
            out.append({"name": name, "type": "push_button", "fillable": False})
            continue
        entry: dict[str, Any] = {"name": name, "type": kind, "page": group[0]["page"]}
        if kind == "checkbox_or_radio":
            states = []
            for b in group:
                if b.get("on_value") and b["on_value"] not in states:
                    states.append(b["on_value"])
            current = next(
                (
                    b["on_value"]
                    for b in group
                    if b.get("on_value")
                    and "/" + str(b["value"]).lstrip("/") == b["on_value"]
                ),
                "/Off",
            )
            if len(group) == 1:
                if _box_label(group[0]):
                    entry["label"] = _box_label(group[0])
                entry["options"] = states + ["/Off"]
            else:
                # One field with several boxes: a radio group. List each option.
                entry["options"] = [
                    {
                        "value": b.get("on_value"),
                        "label": _box_label(b),
                    }
                    for b in group
                ]
            tip = group[0].get("tooltip")
            if tip and tip != name and tip != entry.get("label"):
                entry["tooltip"] = tip
            entry["value"] = current
        else:
            labels: list[str] = []
            for b in group:
                if _label(b) and _label(b) not in labels:
                    labels.append(_label(b))
            if labels:
                entry["label"] = labels[0]
            tip = group[0].get("tooltip")
            if tip and tip != name and tip not in labels:
                entry["tooltip"] = tip
            if len(group) > 1 and len(labels) > 1:
                entry["shared_by_boxes"] = labels
                entry["warning"] = (
                    "This single field appears in several boxes on the form; one value is shown in all of them."
                )
            value = group[0]["value"]
            if value not in (None, "", []):
                entry["value"] = str(value)
            if kind == "choice":
                entry["options"] = [shown for _, shown in group[0].get("choices", [])]
        out.append(entry)
    return {
        "pages": pages,
        "field_count": len(out),
        "fields": out,
        "note": (
            "Use 'label' (the text printed on the form next to or under the box) and 'tooltip' (the form's own "
            "description of the box) to decide what each field is for; fill by 'name', copied exactly. "
            "Leave out a field the form does not require or that does not apply; never write filler such as "
            "'None' or 'N/A' into it."
        ),
    }


# ---- page images -------------------------------------------------------------


def _show_stored_drawings(doc) -> None:
    """Make the page image show the drawings that are in the file.

    When a form asks viewers to redraw its boxes (NeedAppearances), MuPDF
    draws every value again at the form's declared font size, and a value
    that was fitted into its box comes out cut off. The file's own drawings
    are used when every value has one. Changes the open copy only.
    """
    try:
        form = _form_location(doc, create=False)
        if form is None or _get(doc, form[0], form[1] + "NeedAppearances")[1] != "true":
            return
        mu = _mu()
        drawn = (
            mu.PDF_WIDGET_TYPE_TEXT,
            mu.PDF_WIDGET_TYPE_COMBOBOX,
            mu.PDF_WIDGET_TYPE_LISTBOX,
        )
        for page in doc:
            for w in page.widgets():
                if (
                    w.field_type in drawn
                    and w.field_value not in (None, "", [])
                    and not _has_drawn_text(doc, w.xref)
                ):
                    return
        doc.xref_set_key(form[0], form[1] + "NeedAppearances", "false")
    except Exception:
        pass


def render_pdf_page(
    path: str,
    page: int = 1,
    dpi: int = 110,
    max_kb: int = 3000,
    max_side: int = 2000,
) -> tuple[bytes, str, dict[str, Any]]:
    """Draw one page, with its filled boxes, as an image.

    Returns (image bytes, mime type, details). PNG is used when it fits in
    `max_kb`; otherwise JPEG, and then a lower resolution, so the image stays
    within what model providers accept.
    """
    mu = _mu()
    doc = _open_document(path)
    try:
        count = doc.page_count
        try:
            number = int(page)
        except Exception:
            raise ValueError(f"page must be a whole number between 1 and {count}.")
        if number < 1 or number > count:
            raise ValueError(
                f"This PDF has {count} page(s); page {number} does not exist."
            )
        _show_stored_drawings(doc)
        pg = doc[number - 1]
        dpi = max(40, min(int(dpi or 110), 300))
        longest = max(pg.rect.width, pg.rect.height) / 72.0  # inches
        if longest * dpi > max_side:
            dpi = max(40, int(max_side / longest))

        limit = max(100, int(max_kb)) * 1024
        attempt = dpi
        data, mime = b"", "image/png"
        while True:
            pix = pg.get_pixmap(dpi=attempt, alpha=False, annots=True)
            data, mime = pix.tobytes("png"), "image/png"
            if len(data) > limit:
                data, mime = pix.tobytes("jpeg", jpg_quality=80), "image/jpeg"
            if len(data) <= limit or attempt <= 40:
                break
            attempt = max(40, int(attempt * 0.8))
        details = {
            "page": number,
            "pages": count,
            "dpi": attempt,
            "width": pix.width,
            "height": pix.height,
            "bytes": len(data),
            "format": mime.split("/")[1],
        }
        return data, mime, details
    finally:
        try:
            doc.close()
        except Exception:
            pass


# ---- filling -------------------------------------------------------------------

_TRUE_WORDS = {"true", "yes", "y", "on", "x", "1", "checked", "check", "selected"}
_FALSE_WORDS = {"false", "no", "n", "off", "0", "unchecked", "uncheck", "none", ""}
# Text a model writes into a box it has nothing to say in, compared after _key_norm.
_FILLER = {"none", "na", "notapplicable", "nil", "null", "blank"}


def _key_norm(text: Any) -> str:
    """'Street Address of registered agent:' -> 'streetaddressofregisteredagent'."""
    return re.sub(r"[^a-z0-9]+", "", _decode_name(str(text)).lower())


def _resolve_names(boxes: dict[str, list[dict[str, Any]]], values: dict[str, Any]):
    """Match the names the model sent to the names the form uses.

    A name is accepted when it is exact, when it differs from one field's
    name only in letter case, spacing or punctuation, or, failing that, when
    it is the printed label or tooltip of exactly one field. A name that fits
    several fields is not guessed at.
    Returns (mapped, remapped, unknown, ambiguous): mapped is
    {form name: value}, remapped is {name sent: form name}.
    """
    by_name: dict[str, list[str]] = {}
    by_label: dict[str, list[str]] = {}
    for name, group in boxes.items():
        by_name.setdefault(_key_norm(name), []).append(name)
        seen = set()
        for b in group:
            for key in ("tooltip", "label_left", "label_below", "label_right"):
                norm = _key_norm(b.get(key) or "")
                # Shorter than this is a stray word beside the box ("OR", "TX").
                if len(norm) >= 4 and norm not in seen:
                    seen.add(norm)
                    by_label.setdefault(norm, []).append(name)

    mapped: dict[str, Any] = {}
    remapped: dict[str, str] = {}
    unknown: list[str] = []
    ambiguous: list[dict[str, Any]] = []
    loose = []
    for k, v in values.items():
        if k in boxes:
            mapped[k] = v
        else:
            loose.append((k, v))
    for k, v in loose:
        norm = _key_norm(k)
        # A name that is off by a space or a capital was copied from the
        # listing; it outranks a label, which several fields may share.
        found = (by_name.get(norm) or by_label.get(norm) or []) if norm else []
        if not found:
            unknown.append(k)
        elif len(found) > 1:
            ambiguous.append(
                {
                    "sent": k,
                    "reason": "fits several fields; use one exact name",
                    "candidates": [
                        {"name": n, "page": boxes[n][0]["page"]} for n in found
                    ],
                }
            )
        elif found[0] in mapped:
            ambiguous.append(
                {
                    "sent": k,
                    "reason": "another name in the same call already filled this field",
                    "candidates": [
                        {"name": found[0], "page": boxes[found[0]][0]["page"]}
                    ],
                }
            )
        else:
            mapped[found[0]] = v
            remapped[k] = found[0]
    return mapped, remapped, unknown, ambiguous


def _normalize_values(
    boxes: dict[str, list[dict[str, Any]]], clean: dict[str, str]
) -> list[dict[str, Any]]:
    """Turn loose answers into the exact option the form uses.

    Checkbox and radio: accepts the option with or without its leading slash,
    in any letter case, and plain words such as yes/true/on or no/false/off for
    a checkbox. Dropdown and list: accepts the shown text or the stored value in
    any letter case. Signature fields and push buttons cannot be filled.
    Values that match nothing are removed from `clean` and returned, so a box
    is never reported as filled while it stays unchanged.
    """
    rejected: list[dict[str, Any]] = []
    for name in list(clean):
        group = boxes.get(name) or []
        if not group:
            continue
        kind = group[0]["kind"]
        raw = clean[name].strip()
        bare = raw.lstrip("/").strip().lower()
        if kind == "checkbox_or_radio":
            options = []
            for b in group:
                if b.get("on_value") and b["on_value"] not in options:
                    options.append(b["on_value"])
            if not options:
                rejected.append(
                    {
                        "field": name,
                        "value": raw,
                        "reason": "this box has no checked state",
                    }
                )
                del clean[name]
                continue
            match = next(
                (o for o in options if o.lstrip("/").strip().lower() == bare), None
            )
            if match is not None:
                clean[name] = match
            elif bare in _FALSE_WORDS:
                clean[name] = "/Off"
            elif bare in _TRUE_WORDS and len(options) == 1:
                clean[name] = options[0]
            else:
                rejected.append(
                    {"field": name, "value": raw, "valid_options": options + ["/Off"]}
                )
                del clean[name]
        elif kind == "choice":
            choices = group[0].get("choices") or []
            match = next(
                (
                    shown
                    for stored, shown in choices
                    if raw.lower() in (shown.strip().lower(), stored.strip().lower())
                ),
                None,
            )
            if match is not None:
                clean[name] = match
            elif choices and raw:
                rejected.append(
                    {
                        "field": name,
                        "value": raw,
                        "valid_options": [shown for _, shown in choices],
                    }
                )
                del clean[name]
        elif kind in ("signature", "push_button", "unknown"):
            rejected.append(
                {
                    "field": name,
                    "value": raw,
                    "reason": f"a {kind.replace('_', ' ')} cannot be filled with text",
                }
            )
            del clean[name]
    return rejected


def _fitted_size(widget, value: str) -> Optional[float]:
    """A font size at which the value fits its box, or None when no change is needed.

    Some forms declare a font taller than the box (LibreOffice: 11 pt text in an
    8 pt box) or receive a value longer than the box is wide. Drawn at the
    declared size the text is cut off. An explicit size is computed because
    automatic sizing turns negative, and mirrors the text, in very small boxes.
    """
    import math

    mu = _mu()
    try:
        declared = float(widget.text_fontsize or 0)
    except Exception:
        return None
    width, height = abs(widget.rect.width), abs(widget.rect.height)
    if width <= 0 or height <= 0:
        return None
    lines = value.splitlines() or [""]
    longest = max(len(line) for line in lines)
    multiline = bool(int(widget.field_flags or 0) & mu.PDF_TX_FIELD_IS_MULTILINE)
    floor = 2.0
    ceiling = max(floor, height - 1.5)

    def fits(size: float) -> bool:
        if size > ceiling:
            return False
        per_line = max(1.0, (width - 4) / (size * 0.5))
        if not multiline:
            try:
                # Helvetica is as wide as any font a form is likely to use.
                return (
                    max(mu.get_text_length(line, "helv", size) for line in lines)
                    <= width - 4
                )
            except Exception:
                return longest <= per_line
        needed = sum(max(1, math.ceil(len(line) / per_line)) for line in lines)
        if needed == 1:
            return True  # one line: only the height limit above applies
        return needed * size * 1.15 <= max(height - 2, size)

    if declared > 0:
        if fits(declared):
            return None
        size = declared
    else:
        # Automatic: safe in a normal box, not in a tiny one.
        if height >= 8:
            return None
        size = ceiling
    while size > floor and not fits(size):
        size -= 0.5
    return round(max(floor, size), 2)


def _clear_value(doc, widget) -> None:
    """Empty a text or choice box: no stored value, nothing drawn.

    PyMuPDF skips an empty value, which would leave the old one in place.
    """
    holder = widget.xref
    for _ in range(20):
        # A box without a name of its own leaves the value to its parent.
        kind, value = _get(doc, holder, "Parent")
        if _get(doc, holder, "T")[0] != "null" or kind != "xref" or not _refs(value):
            break
        holder = _refs(value)[0]
    for xref in {holder, widget.xref}:
        for key in ("V", "RV", "I"):
            if _get(doc, xref, key)[0] != "null":
                doc.xref_set_key(
                    xref, key, "()" if key == "V" and xref == holder else "null"
                )
    if _get(doc, holder, "V")[0] == "null":
        doc.xref_set_key(holder, "V", "()")
    kind, value = _get(doc, widget.xref, "AP/N")
    if kind == "xref" and _refs(value):
        doc.update_stream(_refs(value)[0], b"/Tx BMC\nEMC\n")


def _draw_value(doc, widget, value) -> None:
    """Set a text or choice value and draw it, keeping the form's own font setting."""
    mu = _mu()
    if value in (None, "", []):
        _clear_value(doc, widget)
        return
    size = None
    if widget.field_type == mu.PDF_WIDGET_TYPE_TEXT and value:
        size = _fitted_size(widget, str(value))
    before = _get(doc, widget.xref, "DA")
    if size is not None:
        widget.text_fontsize = size
    widget.field_value = value
    widget.update()
    if size is not None:
        # The smaller size was for this drawing only.
        try:
            if before[0] == "string":
                doc.xref_set_key(widget.xref, "DA", mu.get_pdf_str(before[1]))
            elif before[0] == "null":
                doc.xref_set_key(widget.xref, "DA", "null")
        except Exception:
            pass


def _own_drawing(doc, widget) -> Optional[str]:
    """The option name, as the file spells it, when the form draws this box itself.

    Returns None when the form has no drawing for the checked state (LaTeX
    writes an empty placeholder), in which case PyMuPDF has to draw one.
    """
    try:
        raw = widget.on_state()
    except Exception:
        raw = None
    if not raw or raw is True or str(raw) == "Off":
        return None
    raw = str(raw).lstrip("/")
    kind, value = _get(doc, widget.xref, "AP/N")
    text = value
    if kind == "xref" and _refs(value):
        try:
            text = doc.xref_object(_refs(value)[0], compressed=True)
        except Exception:
            return None
    elif kind != "dict":
        return None
    if re.search(r"/" + re.escape(raw) + r"\s*\d+\s+0\s+R", text):
        return "/" + raw
    return None


def _switch_box(doc, widget, on: bool) -> None:
    """Check or uncheck one box.

    The form's own drawings are kept: only the box's state is changed, so a
    form that marks its choices with a tick keeps its tick. PyMuPDF would
    redraw the box in its own style (a round dot for radio buttons).
    """
    spelled = _own_drawing(doc, widget)
    if spelled is None:
        widget.field_value = bool(on)
        widget.update()
        return
    state = spelled if on else "/Off"
    doc.xref_set_key(widget.xref, "AS", state)
    # A box that is its own field carries the value itself; a box inside a
    # group leaves the value to the group (see _fix_button_values).
    if (
        _get(doc, widget.xref, "T")[0] != "null"
        or _get(doc, widget.xref, "Parent")[0] != "xref"
    ):
        doc.xref_set_key(widget.xref, "V", state)
    elif _get(doc, widget.xref, "V")[0] != "null":
        doc.xref_set_key(widget.xref, "V", state)


def _set_name_value(doc, xref: int, state: str) -> None:
    """Store a checkbox/radio value as a PDF name."""
    doc.xref_set_key(xref, "V", _pdf_name(state))


def _fix_button_values(doc, touched: set[str]) -> int:
    """Store the value of checkbox and radio fields as a PDF name.

    Libraries write the group's value as a text string, e.g. (B), while each
    box's state /AS is the name /B. Apple's Preview compares the box against
    the field value, finds no match, and draws the box empty even though the
    option is selected. Acrobat, Chrome and Ghostscript only look at /AS, so
    the defect is invisible there. Returns the number of values corrected.
    """
    fixed = 0
    groups: dict[int, list[int]] = {}
    for _, wx in _page_widget_xrefs(doc):
        if _inherited(doc, wx, "FT")[1] != "/Btn":
            continue
        flags = _inherited(doc, wx, "Ff")
        try:
            if int(float(flags[1])) & (1 << 16):
                continue  # push button
        except Exception:
            pass
        pk, pv = _get(doc, wx, "Parent")
        holder = wx
        if _get(doc, wx, "T")[0] == "null" and pk == "xref" and _refs(pv):
            holder = _refs(pv)[0]
        groups.setdefault(holder, []).append(wx)
    for holder, kids in groups.items():
        kind, value = _get(doc, holder, "V")
        states = [_get(doc, k, "AS") for k in kids]
        on = next((v for t, v in states if t == "name" and v != "/Off"), None)
        if kind == "name" and (
            on is None or _decode_name(value) == _decode_name(on) or holder in kids
        ):
            continue
        if kind == "null" and on is None:
            continue
        _set_name_value(doc, holder, on or "/Off")
        fixed += 1
    return fixed


def _has_drawn_text(doc, xref: int) -> bool:
    """Whether a box's drawing contains any text."""
    kind, value = _get(doc, xref, "AP/N")
    body = b""
    if kind == "xref" and _refs(value):
        try:
            body = doc.xref_stream(_refs(value)[0]) or b""
        except Exception:
            body = b""
    return b"Tj" in body or b"TJ" in body


def _refresh_empty_appearances(doc) -> int:
    """Draw values that the form stores but never drew.

    Some forms keep a value in a field and leave its drawing empty, trusting
    the viewer to fill it in. A page image and a flattened copy show only
    drawings, so such a value would be missing. Returns the number of boxes
    redrawn.
    """
    mu = _mu()
    redrawn = 0
    for page in doc:
        for w in page.widgets():
            try:
                if w.field_type not in (
                    mu.PDF_WIDGET_TYPE_TEXT,
                    mu.PDF_WIDGET_TYPE_COMBOBOX,
                    mu.PDF_WIDGET_TYPE_LISTBOX,
                ):
                    continue
                if w.field_value in (None, "", []):
                    continue
                if _has_drawn_text(doc, w.xref):
                    continue
                _draw_value(doc, w, w.field_value)
                redrawn += 1
            except Exception:
                continue
    return redrawn


def _drop_stacked_duplicates(doc) -> int:
    """Remove a box that sits exactly on top of another box of the same field.

    Both would be drawn when flattening, which prints the text twice and makes
    it look bold. Returns the number of boxes removed.
    """
    removed = 0
    for page in doc:
        kept: list[tuple[str, Any]] = []
        extra = []
        for w in page.widgets():
            r = w.rect
            if any(
                n == w.field_name and all(abs(a - b) <= 3 for a, b in zip(k, r))
                for n, k in kept
            ):
                extra.append(w)
            else:
                kept.append((w.field_name, tuple(r)))
        for w in extra:
            try:
                page.delete_widget(w)
                removed += 1
            except Exception:
                continue
    return removed


def _read_back(
    data: bytes, wanted: dict[str, str], empty: Optional[dict] = None
) -> list[dict[str, Any]]:
    """Open the written file again and list every value that is not there.

    A text value counts only when it is stored and drawn: a page image shows
    drawings, not stored values. Pass a dict as `empty` to receive the text
    and choice fields that hold no value, as {"page 2": [names]}.
    """

    def text(v) -> str:
        if isinstance(v, (list, tuple)):
            v = ", ".join(str(x) for x in v)
        return (
            str("" if v is None else v)
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .strip()
        )

    def state(v) -> str:
        return "/" + _decode_name(str(v or "Off")).lstrip("/")

    doc = _prepared(data)
    try:
        after = _collect(doc)
        if empty is not None:
            for name, group in after.items():
                if group[0]["kind"] in ("text", "choice") and not text(
                    group[0]["value"]
                ):
                    empty.setdefault(f"page {group[0]['page']}", []).append(name)
        missing: list[dict[str, Any]] = []
        for name, want in wanted.items():
            group = after.get(name) or []
            if not group:
                missing.append(
                    {
                        "field": name,
                        "wanted": want,
                        "got": None,
                        "reason": "field not found after writing",
                    }
                )
                continue
            kind = group[0]["kind"]
            if kind == "checkbox_or_radio":
                got = next(
                    (
                        b["on_value"]
                        for b in group
                        if b.get("on_value") and state(b["value"]) == b["on_value"]
                    ),
                    "/Off",
                )
                if state(got) != state(want):
                    missing.append(
                        {
                            "field": name,
                            "wanted": want,
                            "got": got,
                            "page": group[0]["page"],
                        }
                    )
                continue
            got = text(group[0]["value"])
            accepted = {text(want)}
            if kind == "choice":
                accepted |= {
                    text(stored)
                    for stored, shown in group[0].get("choices") or []
                    if text(shown) == text(want)
                }
            if got not in accepted:
                missing.append(
                    {
                        "field": name,
                        "wanted": want,
                        "got": got,
                        "page": group[0]["page"],
                    }
                )
            elif text(want):
                blank = [
                    b["page"] for b in group if not _has_drawn_text(doc, b["xref"])
                ]
                if blank:
                    missing.append(
                        {
                            "field": name,
                            "wanted": want,
                            "got": got,
                            "page": blank[0],
                            "reason": "value stored but not drawn on the page",
                        }
                    )
        return missing
    finally:
        try:
            doc.close()
        except Exception:
            pass


def fill_pdf_fields(
    path: str,
    values: dict[str, Any],
    flatten: bool = False,
    rejected: Optional[list] = None,
    report: Optional[dict] = None,
) -> tuple[bytes, list[str]]:
    """Fill fields and return (pdf_bytes, unknown_field_names).

    Pass a list as `rejected` to receive values that matched none of a field's
    options; those fields are left as they were. Pass a dict as `report` to
    receive what was written and whether it reads back: filled, remapped,
    blank, replaced, filler, ambiguous, did_not_stick, still_empty,
    pages_touched.
    """
    mu = _mu()
    doc = _prepared(path)
    try:
        boxes = _collect(doc)
        if not boxes:
            raise ValueError(
                "This PDF has no fillable fields (a flat, scanned, or already flattened form), so there is nothing to fill."
            )
        mapped, remapped, unknown, ambiguous = _resolve_names(boxes, values)
        clean: dict[str, str] = {}
        for k, v in mapped.items():
            if isinstance(v, bool):
                clean[k] = "true" if v else "false"
            else:
                clean[k] = "" if v is None else str(v)
        bad = _normalize_values(boxes, clean)
        if rejected is not None:
            rejected.extend(bad)

        had = {k: str(boxes[k][0]["value"] or "").strip() for k in clean}
        buttons = (mu.PDF_WIDGET_TYPE_CHECKBOX, mu.PDF_WIDGET_TYPE_RADIOBUTTON)
        for page in doc:
            widgets = [w for w in page.widgets() if w.field_name in clean]
            # Switch boxes off before switching one on: turning a sibling off
            # afterwards would reset the whole group.
            widgets.sort(
                key=lambda w: (
                    1
                    if (w.field_type in buttons and _on_state(w) == clean[w.field_name])
                    else 0
                )
            )
            for w in widgets:
                value = clean[w.field_name]
                if w.field_type in buttons:
                    state = _on_state(w)
                    _switch_box(doc, w, bool(state and state == value))
                else:
                    _draw_value(doc, w, value)

        _fix_button_values(doc, set(clean))
        _refresh_empty_appearances(doc)
        if flatten:
            # Flattening removes the fields, so the check reads the copy made just before.
            check = doc.tobytes(garbage=0)
            _drop_stacked_duplicates(doc)
            doc.bake(annots=False, widgets=True)
            data = doc.tobytes(garbage=3, deflate=True)
        else:
            form = _form_location(doc, create=False)
            if form is not None:
                doc.xref_set_key(form[0], form[1] + "NeedAppearances", "true")
            data = check = doc.tobytes(garbage=3, deflate=True)

        if report is not None:
            empty: dict[str, list[str]] = {}
            try:
                did_not_stick = _read_back(check, clean, empty)
            except Exception as e:
                did_not_stick = [
                    {
                        "field": k,
                        "wanted": v,
                        "got": None,
                        "reason": f"could not be read back ({type(e).__name__})",
                    }
                    for k, v in clean.items()
                ]
            failed = {d["field"] for d in did_not_stick}
            typed = [
                k
                for k in clean
                if k not in failed and boxes[k][0]["kind"] in ("text", "choice")
            ]
            blank = [k for k in typed if not clean[k].strip()]
            # A box left blank on purpose is not one that was forgotten.
            empty = {
                p: [n for n in names if n not in blank] for p, names in empty.items()
            }
            empty = {p: names for p, names in empty.items() if names}
            report.update(
                {
                    "filled": [k for k in clean if k not in failed],
                    "blank": blank,
                    "replaced": {
                        k: had[k]
                        for k in typed
                        if had[k] and had[k] != clean[k].strip()
                    },
                    "filler": [
                        k
                        for k in typed
                        if boxes[k][0]["kind"] == "text"
                        and _key_norm(clean[k]) in _FILLER
                    ],
                    "remapped": remapped,
                    "ambiguous": ambiguous,
                    "did_not_stick": did_not_stick,
                    "still_empty": empty,
                    "pages_touched": sorted(
                        {b["page"] for k in clean for b in boxes[k]}
                    ),
                }
            )
        return data, unknown
    finally:
        try:
            doc.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Open WebUI tool
# --------------------------------------------------------------------------


class Tools:
    class Valves(BaseModel):
        flatten_by_default: bool = Field(
            default=False,
            description="Flatten filled forms so values become fixed page content (no longer editable).",
        )
        base_url: str = Field(
            default="",
            description=(
                "Public URL of this Open WebUI, e.g. https://drcurbside.ai, used to build full download "
                "links. Leave empty to use the WebUI URL from Admin > Settings > General, or the request host."
            ),
        )

        render_dpi: int = Field(
            default=110,
            description="Resolution of page images sent to the model by render_page (40 to 300). Higher is sharper and costs more.",
        )
        render_max_kb: int = Field(
            default=3000,
            description="Largest page image, in kilobytes, sent to the model. Larger pages are sent as JPEG or at lower resolution.",
        )

        review_pages: int = Field(
            default=3,
            description=(
                "How many filled pages fill_form shows the model for checking (0 to 4). Only pages that were "
                "written to are shown. Needs Open WebUI 0.11.4 or later; older versions ask the model to call render_page."
            ),
        )

    def __init__(self):
        self.valves = self.Valves()

    # ---- internals -------------------------------------------------------

    @staticmethod
    def _attached_pdfs(files: Optional[list]) -> list[dict]:
        pdfs = []
        for f in files or []:
            if not isinstance(f, dict):
                continue
            name = f.get("name") or (f.get("file") or {}).get("filename") or ""
            ctype = f.get("content_type") or (f.get("file") or {}).get("meta", {}).get(
                "content_type", ""
            )
            if name.lower().endswith(".pdf") or ctype == "application/pdf":
                pdfs.append(
                    {"id": f.get("id") or (f.get("file") or {}).get("id"), "name": name}
                )
        return pdfs

    async def _resolve_path(self, file_id: str, user_id: str) -> tuple[str, str]:
        from open_webui.models.files import Files
        from open_webui.storage.provider import Storage

        rec = await Files.get_file_by_id(file_id)
        if rec is None:
            raise ValueError(f"No file with id {file_id}")
        if rec.user_id != user_id:
            raise ValueError("That file belongs to another user")
        path = Storage.get_file(rec.path)
        if not os.path.isfile(path):
            raise ValueError(f"File missing on disk: {path}")
        return path, rec.filename

    async def _pick_file(
        self, file_id: str, files: Optional[list], user_id: str
    ) -> tuple[str, str, str]:
        if file_id:
            path, name = await self._resolve_path(file_id, user_id)
            return file_id, path, name
        pdfs = self._attached_pdfs(files)
        if len(pdfs) == 1:
            path, name = await self._resolve_path(pdfs[0]["id"], user_id)
            return pdfs[0]["id"], path, name
        if not pdfs:
            raise ValueError("No PDF is attached to this chat. Attach the form first.")
        raise ValueError(
            "Several PDFs are attached; pass file_id. Attached: "
            + ", ".join(f"{p['name']} (id {p['id']})" for p in pdfs)
        )

    @staticmethod
    def _host_shows_images_in_results() -> bool:
        """Whether Open WebUI hands images inside a JSON result to the model.

        From 0.11.4 a result may be a dict and every value that is a
        data:image string reaches the model as a picture. Before that the
        whole result had to be one image, and a dict would arrive as text.
        """
        try:
            from open_webui.utils import middleware

            return callable(getattr(middleware, "extract_base64_images", None))
        except Exception:
            return False

    # ---- tools exposed to the model -------------------------------------

    async def list_form_fields(
        self,
        file_id: str = "",
        __user__: Optional[dict] = None,
        __files__: Optional[list] = None,
    ) -> str:
        """
        List the fillable form fields of a PDF attached to the chat. Call this
        before fill_form so you know the exact field names.
        :param file_id: Id of the attached PDF. Leave empty when only one PDF is attached.
        :return: JSON with the file name and its fields (name, type, current value, options).
        """
        try:
            fid, path, name = await self._pick_file(
                file_id, __files__, (__user__ or {}).get("id")
            )
            info = inspect_pdf_fields(path)
            info.update(
                {"file_id": fid, "file_name": name, "today": date.today().isoformat()}
            )
            if info["field_count"] == 0:
                info["note"] = (
                    "This PDF has no fillable fields (flat or scanned form). "
                    "Filling it needs a text overlay, which this tool does not do."
                )
            return json.dumps(info, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def render_page(
        self,
        page: int = 1,
        file_id: str = "",
        show_in_chat: Optional[bool] = None,
        __user__: Optional[dict] = None,
        __files__: Optional[list] = None,
        __event_emitter__=None,
    ) -> str:
        """
        Look at one page of a PDF as an image. Use it to check that a filled form
        looks right, or to read a form that has no fillable fields (a flat or
        scanned form). Needs a model that can read images. Returns one page per
        call; call again for another page.
        :param page: Page number, starting at 1.
        :param file_id: Id of the PDF. Leave empty when only one PDF is attached. To look at a form you just filled, pass the file_id that fill_form returned.
        :param show_in_chat: Leave unset. Only set true when the user asks to see the page image themselves.
        :return: The page as an image.
        """
        try:
            user_id = (__user__ or {}).get("id")
            fid, path, name = await self._pick_file(file_id, __files__, user_id)
            data, mime, details = render_pdf_page(
                path,
                page=page,
                dpi=self.valves.render_dpi,
                max_kb=self.valves.render_max_kb,
            )
        except Exception as e:
            return json.dumps({"error": str(e)})

        if _as_bool(show_in_chat, default=False):
            try:
                from open_webui.models.files import FileForm, Files
                from open_webui.storage.provider import Storage

                ext = "jpg" if mime == "image/jpeg" else "png"
                base = os.path.splitext(os.path.basename(name))[0]
                image_name = f"{base}-page{details['page']}.{ext}"
                new_id = str(uuid.uuid4())
                _, stored_path = Storage.upload_file(
                    io.BytesIO(data),
                    f"{new_id}_{image_name}",
                    {"OpenWebUI-User-Id": user_id or ""},
                )
                await Files.insert_new_file(
                    user_id,
                    FileForm(
                        id=new_id,
                        filename=image_name,
                        path=stored_path,
                        data={},
                        meta={
                            "name": image_name,
                            "content_type": mime,
                            "size": len(data),
                            "source_file_id": fid,
                        },
                    ),
                )
                if __event_emitter__:
                    await __event_emitter__(
                        {
                            "type": "files",
                            "data": {
                                "files": [
                                    {
                                        "type": "image",
                                        "url": f"/api/v1/files/{new_id}/content",
                                    }
                                ]
                            },
                        }
                    )
            except Exception:
                pass  # showing the image to the user is optional; the model still gets it

        # Open WebUI hands a result that starts with "data:image/" to the model
        # as a picture. The whole return value has to be the image.
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")

    async def fill_form(
        self,
        values: str,
        file_id: str = "",
        output_name: str = "",
        flatten: Optional[bool] = None,
        __user__: Optional[dict] = None,
        __files__: Optional[list] = None,
        __event_emitter__=None,
        __request__=None,
    ):
        """
        Fill the form fields of an attached PDF and return the filled PDF as a
        chat attachment. Use the exact field names from list_form_fields.
        The form is complete only when status is "ok". When status is
        "partial", some values were NOT written: read the lists at the top of
        the result, call list_form_fields, and call fill_form again with the
        returned file_id and only the missing fields. Do not ask the user
        about a field this tool already flagged. still_empty lists the fields
        that hold no value: fill the ones the request covers. Leave out a
        field the form does not require or that does not apply; never write
        filler such as "None" or "N/A" into it. To empty a field that holds
        a value, send "" for it. Before telling the user the
        form is complete, look at every page in pages_touched: either in
        page_images of this result, or with render_page. If a value is missing
        on the image, call fill_form again.
        :param values: JSON object mapping field names to values, e.g. {"Name": "Jane Doe", "Date": "09/16/2026"}. Include every field you want filled in ONE call. For a checkbox use true or false; for a radio group use the option value from list_form_fields (for example "/A").
        :param file_id: Id of the attached PDF. Leave empty when only one PDF is attached.
        :param output_name: File name for the filled copy. Defaults to "<original>-filled.pdf".
        :param flatten: Leave unset. Only set true when the user explicitly asks for a flattened or non-editable PDF.
        :return: A summary: status, what was not written and why, the pages to check, and the full download URL of the filled PDF. When the user asks for a link, give them download_link_markdown verbatim.
        """
        from open_webui.models.files import FileForm, Files
        from open_webui.storage.provider import Storage

        try:
            user_id = (__user__ or {}).get("id")
            try:
                parsed = json.loads(values) if isinstance(values, str) else dict(values)
            except json.JSONDecodeError as e:
                return json.dumps({"error": f"values is not valid JSON: {e}"})
            if not isinstance(parsed, dict) or not parsed:
                return json.dumps({"error": "values must be a non-empty JSON object"})

            fid, path, orig_name = await self._pick_file(file_id, __files__, user_id)
            do_flatten = _as_bool(flatten, default=self.valves.flatten_by_default)
            rejected: list = []
            report: dict = {}
            pdf_bytes, unknown = fill_pdf_fields(
                path, parsed, flatten=do_flatten, rejected=rejected, report=report
            )
            ambiguous = report.get("ambiguous") or []
            did_not_stick = report.get("did_not_stick") or []
            filled = report.get("filled") or []

            problems: dict[str, Any] = {}
            if unknown:
                problems["ignored_unknown_fields"] = unknown
            if ambiguous:
                problems["ambiguous_fields"] = ambiguous
            if rejected:
                problems["not_set_invalid_option"] = rejected
            if did_not_stick:
                problems["did_not_stick"] = did_not_stick

            if not filled:
                # Nothing was written: an unchanged copy in the chat would pass for a filled form.
                return json.dumps(
                    {
                        "status": "failed",
                        **problems,
                        "next": (
                            "Nothing was filled and no file was made. Call list_form_fields, then fill_form "
                            "again with the exact names. Do not ask the user."
                        ),
                    },
                    indent=2,
                )

            base = os.path.splitext(os.path.basename(orig_name))[0]
            out_name = output_name.strip() or f"{base}-filled.pdf"
            if not out_name.lower().endswith(".pdf"):
                out_name += ".pdf"

            new_id = str(uuid.uuid4())
            _, stored_path = Storage.upload_file(
                io.BytesIO(pdf_bytes),
                f"{new_id}_{out_name}",
                {"OpenWebUI-User-Id": user_id or ""},
            )
            rec = await Files.insert_new_file(
                user_id,
                FileForm(
                    id=new_id,
                    filename=out_name,
                    path=stored_path,
                    data={},
                    meta={
                        "name": out_name,
                        "content_type": "application/pdf",
                        "size": len(pdf_bytes),
                        "source_file_id": fid,
                    },
                ),
            )
            url = f"/api/v1/files/{new_id}/content"
            base = (self.valves.base_url or "").strip().rstrip("/")
            if not base and __request__ is not None:
                try:
                    base = (
                        str(
                            getattr(__request__.app.state.config, "WEBUI_URL", "") or ""
                        )
                    ).rstrip("/")
                except Exception:
                    base = ""
                if not base or base.startswith("http://localhost"):
                    try:
                        base = str(__request__.base_url).rstrip("/")
                    except Exception:
                        pass
            full_url = f"{base}{url}" if base else url

            # Index the filled copy like a normal upload so later questions in
            # the chat can read it (otherwise retrieval logs a missing collection).
            indexed = False
            if __request__ is not None and user_id:
                try:
                    from open_webui.internal.db import get_async_db_context
                    from open_webui.models.users import Users
                    from open_webui.routers.retrieval import (
                        ProcessFileForm,
                        process_file,
                    )

                    user_obj = await Users.get_user_by_id(user_id)
                    async with get_async_db_context() as db:
                        await process_file(
                            __request__,
                            ProcessFileForm(file_id=new_id),
                            user=user_obj,
                            db=db,
                        )
                    indexed = True
                except Exception:
                    indexed = False

            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "files",
                        "data": {
                            "files": [
                                {
                                    "type": "file",
                                    "id": new_id,
                                    "name": out_name,
                                    "url": url,
                                    "content_type": "application/pdf",
                                    "size": len(pdf_bytes),
                                }
                            ]
                        },
                    }
                )

            pages = report.get("pages_touched") or []
            limit = max(0, min(int(self.valves.review_pages or 0), 4))
            images: dict[str, str] = {}
            if limit and self._host_shows_images_in_results():
                for number in pages[:limit]:
                    try:
                        data, mime, _ = render_pdf_page(
                            pdf_bytes,
                            page=number,
                            dpi=self.valves.render_dpi,
                            max_kb=self.valves.render_max_kb,
                        )
                        images[f"page_{number}"] = (
                            f"data:{mime};base64,"
                            + base64.b64encode(data).decode("ascii")
                        )
                    except Exception:
                        continue
            unseen = [n for n in pages if f"page_{n}" not in images]

            steps = []
            if problems:
                steps.append(
                    "Some values were NOT written. Call list_form_fields, then fill_form again with "
                    f'file_id "{new_id}", the exact names, and only the fields listed above. Do not ask the user.'
                )
            if images:
                steps.append(
                    "Look at page_images and compare every value with what was asked. "
                    "If one is missing or cut off, call fill_form again."
                )
            filler = report.get("filler") or []
            if filler:
                steps.append(
                    "The values in filler_values look like filler. Unless the user asked for that text, call "
                    f'fill_form again with file_id "{new_id}" and "" for those fields to empty them.'
                )
            still_empty = report.get("still_empty") or {}
            if still_empty:
                steps.append(
                    "The fields in still_empty hold no value. If the request covers one of them, call fill_form "
                    f'again with file_id "{new_id}" and only those fields; leave the rest empty. Do not ask the user.'
                )
            if unseen:
                steps.append(
                    f'Call render_page with file_id "{new_id}" for page(s) {", ".join(str(n) for n in unseen)} '
                    "and check the values before telling the user the form is complete."
                )

            result: dict[str, Any] = {"status": "partial" if problems else "ok"}
            result.update(problems)
            result["next"] = " ".join(steps)
            result.update(
                {
                    "file_id": new_id,
                    "file_name": out_name,
                    "download_url": full_url,
                    "download_link_markdown": f"[{out_name}]({full_url})",
                    "filled_fields": filled,
                    "pages_touched": pages,
                    "flattened": do_flatten,
                    "indexed_for_search": indexed,
                }
            )
            if report.get("remapped"):
                result["remapped_fields"] = report["remapped"]
            if still_empty:
                result["still_empty"] = still_empty
            if report.get("blank"):
                # An empty value counts as filled; say so, or the box looks like a failed write.
                result["left_blank_as_sent"] = report["blank"]
            if report.get("replaced"):
                # What the form held before, so a value written over can be told to the user.
                result["replaced_values"] = report["replaced"]
            if filler:
                result["filler_values"] = filler
            if images:
                # Returned as a dict: Open WebUI takes the pictures out for the
                # model and turns the rest into JSON text.
                result["page_images"] = images
                return result
            return json.dumps(result, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)})
