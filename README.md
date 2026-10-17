# PDF Tool for Open WebUI (PyMuPDF)

An [Open WebUI](https://github.com/open-webui/open-webui) tool that lets a model
work on a PDF attached to the chat:

- **Fill forms:** list a form's fields (AcroForm), fill them, and flatten the
  result if asked.
- **Redact text:** find printed text and delete exactly those characters from
  the file.
- **Check its own work:** after each fill or redaction, the model gets a
  close-up image of every field or area it changed. It can also look at any
  page, or zoom in on one area.

Every filled or redacted copy is attached to the chat as a downloadable PDF.

The whole tool is one file: `pdf_tool.py`.

## What the model can do

| Function | Purpose |
|---|---|
| `list_form_fields` | List every fillable field: name, type, the label printed next to the box, current value, and valid options. Also gives today's date, for date fields. |
| `fill_form` | Fill fields from a JSON object of `{field name: value}`, read every value back, and attach the filled PDF to the chat. Shows the model a close-up of each field it wrote. Optionally flatten it. |
| `render_page` | Hand one page, or a close-up of one field or area, to the model as an image, to check a filled form or read a flat or scanned one. Needs a model that can read images. |
| `flatten_form` | Flatten a copy that was already filled, so its values become fixed page content, without resending them. |
| `find_text` | List every place a piece of text is printed, as whole words, with a hit id, the page, the text as printed and its context on the line. |
| `redact_text` | Delete exactly the characters behind the given hit ids, redraw any neighbouring characters the box clipped, confirm the text is gone, attach the redacted PDF to the chat, and show the model a close-up of each hit. |

Flat and scanned PDFs have no fields to fill. The tool reports that instead of
drawing text over the page.

## Installation

1. Make PyMuPDF available to the Open WebUI process. The tool has no
   `requirements:` line on purpose, so Open WebUI will not try to install it.
   With a `uvx`-based systemd unit, add `--with pymupdf` to the command.
2. In Open WebUI, go to **Admin > Tools > +**, paste the contents of
   `pdf_tool.py`, and save.
3. Enable the tool for a model (**Admin > Models > model > Tools**) or toggle
   it in the chat's **+** menu.

## Settings (valves)

| Valve | Default | Meaning |
|---|---|---|
| `flatten_by_default` | `false` | Flatten filled forms so values become fixed page content. |
| `base_url` | empty | Public URL of the Open WebUI, used to build full download links. When empty, the WebUI URL from **Admin > Settings > General** is used, then the request host. |
| `min_font_size` | `6.0` | Smallest size, in points, a value is shrunk to so it fits its box. A value that does not fit at this size is drawn cut off and reported, so the model can shorten it. |
| `render_dpi` | `110` | Resolution of page images sent to the model (40 to 300). |
| `render_max_kb` | `3000` | Largest page image sent to the model, in kilobytes. Larger pages are sent as JPEG or at lower resolution. |
| `review_crops` | `12` | How many close-ups `fill_form` and `redact_text` show the model for checking (0 to 30): one JPEG per field written or hit removed. Needs Open WebUI 0.11.4 or later. |
| `crop_dpi` | `200` | Resolution of those close-ups (72 to 300). |
| `review_pages` | `0` | How many whole changed pages `fill_form` and `redact_text` also show the model, besides the close-ups (0 to 4). Needs Open WebUI 0.11.4 or later. |

## Usage

Attach a PDF form to a chat and ask the model to fill it. A typical exchange
makes three calls:

1. `list_form_fields` to get the exact field names and options.
2. `fill_form` with every value in one call, for example:

   ```json
   {"Name": "Jane Doe", "Date": "09/16/2026", "Agree": true, "Plan": "/A"}
   ```

3. `render_page` with the `file_id` that `fill_form` returned, and the page
   and `rect` of each entry in `crops_not_shown`, for any field `fill_form`
   did not already show close up.

When several PDFs are attached, pass `file_id` to choose one.

Values are matched loosely. A checkbox accepts `true`/`false`, `yes`/`no`,
`on`/`off`, or its option name with or without the leading slash. A radio
group takes one of its option values. A dropdown takes the shown text or the
stored value, in any letter case. A value that matches no option is left
unset and reported in `not_set_invalid_option` along with the valid options.

Field names are matched exactly first, then by a name that differs only in
letter case, spacing or punctuation, then by the printed label or tooltip
when exactly one field has it. Such matches are reported in
`remapped_fields`. A name that fits several fields is not guessed at.

## Redacting text

Redaction is a two-call flow, like filling:

1. `find_text` with the text as printed, for example `"2 day"`. Every
   occurrence comes back as a hit with an id such as `p1:12:15-20` (page,
   line, character offsets), the page, the text, its rectangle, and the line
   around it with the match in brackets:

   ```json
   {"id": "p1:12:15-20", "page": 1, "text": "2 day",
    "context": "Flare-up lasts [2 day](s) per episode"}
   ```

2. `redact_text` with the ids to remove, for example `["p1:12:15-20"]`.

Matching is on whole words, so `2 day` is not found inside `12 day`, and a
hit is a run of printed characters, not a search string: the redaction box
is the union of those characters' boxes and nothing else on the page is
touched. One occurrence of a value can be removed while another stays.
Letter case matters; runs of spaces do not. `whole_word=false` also matches
inside words, for the rare case where that is what the user means.

The characters are deleted from the file, not covered. The area is left
blank (`fill="white"`) or drawn as a black box (`fill="black"`). PyMuPDF
drops every character whose box touches a redaction box, so a value printed
over a line of underscores would take part of the line with it; the
characters outside the hit that vanish are drawn again at their own
positions and counted in `restored_glyphs`. A hit that sits in a form field
is reported with `in_field`, and the field's stored value is emptied too.

`status` is `ok` only when every hit resolved and the text was confirmed
gone. An id from another file, or from a copy that has since changed, is
listed in `not_found` or `stale` (when the entry carried a `text` to check)
and the status is `partial`. When no hit resolves, nothing is written and the
status is `failed`. The result carries the same `file_id`, download link and
close-ups (`crop_images`, one per hit, listed in `crops`) as `fill_form`. To redact more from the result, call
`find_text` with the new `file_id`.

A scanned or image-only PDF has no text layer, so there is nothing to find;
`find_text` says so.

The helpers work on their own, outside Open WebUI:

```python
from pdf_tool import find_pdf_text, redact_pdf_text

hits = find_pdf_text("form.pdf", "2 day")["hits"]
report = {}
pdf = redact_pdf_text("form.pdf", [h["id"] for h in hits], report=report)
open("form-redacted.pdf", "wb").write(pdf)
```

## What `fill_form` returns

`status` is `ok` only when every name resolved and every value reads back
from the written file. Otherwise it is `partial`, or `failed` when nothing
was written (no file is made). The reasons come first in the result:

| Key | Meaning |
|---|---|
| `ignored_unknown_fields` | Names that fit no field. |
| `ambiguous_fields` | Names that fit several fields, with the candidates and their pages. |
| `not_set_invalid_option` | Values that match none of a field's options, with the valid options. |
| `did_not_stick` | Values that are missing from the written file, or stored but not drawn on the page. |
| `next` | What the model has to do before it may call the form complete. |

`still_empty` lists, page by page, the text and choice fields that hold no
value after the fill, so a field the model left out of its call shows up in
the result instead of on the page image only.

`left_blank_as_sent` names the fields whose value was sent empty, so a box
the model left blank is not taken for a write that failed. Those fields are
not repeated in `still_empty`. Sending an empty value for a field that holds
one empties it: the stored value and its drawing are both removed.

`replaced_values` gives, for each text or choice field that held a different
value before the fill, the value that was there.

`filler_values` names the text fields that were filled with "None", "N/A" or
the like, and `next` tells the model to empty them unless the user asked for
that text.

`pages_touched` lists the pages that were written to. On Open WebUI 0.11.4
or later the model gets a JPEG close-up of each field written, up to
`review_crops`, in `crop_images` of the same result. `crops` says which field
each image shows, with its page and `rect`. A close-up is framed on the
field's boxes, widened to take in the printed label, and drawn at `crop_dpi`,
so small print stays readable. Whole pages are sent as well only when
`review_pages` is above 0. On older versions, and for fields over the limit,
the fields are listed in `crops_not_shown` and `next` tells the model to call
`render_page` with their page and `rect`. Images reach the model only
through a tool's return value; `__event_emitter__` shows files to the user,
not to the model.

## What it handles

- **Forms saved by Apple's Preview**, which keeps two copies of every box. The
  field tree is relinked to the boxes on the pages, so filled values show up
  in Preview and read back correctly.
- **Boxes without a form dictionary**, left behind when a PDF is merged, split
  or re-saved. The form dictionary is rebuilt.
- **Checkbox and radio values** are stored as PDF names, so Preview draws the
  selected box. The form's own check mark drawings are kept.
- **Labels** are read from the printed words only, so a value typed into
  one box is not taken for the label of the box next to it. A checkbox is
  labelled by the text after it, or by the caption over it. A box inside
  a table that has no text beside it is labelled by its column heading and
  row ("Self, row 2").
- **Text that does not fit its box** is drawn at a smaller font size for that
  value only. The form's font setting is left as it was.
- **Values stored but never drawn** are drawn on every fill, so they show in
  page images and in viewers that do not draw them on their own.
- **Page images** show the drawings that are in the file. MuPDF would
  otherwise redraw every value at the form's declared font size and cut off
  a value that was fitted into its box.
- **Flattening** removes boxes stacked on top of each other so text is not
  printed twice.
- **Password-protected and damaged files** return a message the user can act on.
- **File ownership**: a file that belongs to another user is refused.

## How it works

Open WebUI keeps the original uploaded PDF in its uploads folder, even though
a converted copy is used for retrieval. The tool receives the chat's
attachments as `__files__`, opens the original with PyMuPDF, and reads or
writes the real form fields. The filled copy is saved through Open WebUI's own
file store, so it appears as a chat attachment, and is indexed like a normal
upload so later questions in the chat can read it.

The PDF helpers at the top of the file (`inspect_pdf_fields`,
`fill_pdf_fields`, `render_pdf_page`, `render_pdf_region`, `find_pdf_text`, `redact_pdf_text`) import nothing from Open WebUI and can
be used on their own:

```python
from pdf_tool import inspect_pdf_fields, fill_pdf_fields

print(inspect_pdf_fields("form.pdf"))
pdf_bytes, unknown = fill_pdf_fields("form.pdf", {"Name": "Jane Doe"})
open("form-filled.pdf", "wb").write(pdf_bytes)
```

## Tests

The scripts in `tests/` read values back with pypdf and render pages with
Ghostscript and Preview's engine, so the tool does not grade itself. They need
`uv` and Ghostscript (`gs`); the Preview-engine checks need macOS. Sample PDFs
are not included in this repository; the scripts read them from the folder
given as the project folder, and the tool file may be given by absolute path.

```
uvx --with pymupdf --with pypdf --with pydantic --with pdfplumber --with pillow --with numpy \
  python tests/harness.py ~/pdf_maker /tmp/pdf_out $PWD/pdf_tool.py
```

See [tests/README.md](tests/README.md) for all three suites and how to read
their output.

## License

AGPL-3.0, as declared in the tool's header.

PyMuPDF is itself AGPL-3.0 (or commercial, from Artifex). Running it behind a
hosted service can oblige you to offer the source to the service's users.
