# PDF Form Filler for Open WebUI (PyMuPDF)

An [Open WebUI](https://github.com/open-webui/open-webui) tool that lets a model
inspect and fill the form fields (AcroForm) of a PDF attached to the chat,
return the filled PDF as a downloadable attachment, and look at a page as an
image.

The whole tool is one file: `pdf_tool_pymupdf_v2.py`.

## What the model can do

| Function | Purpose |
|---|---|
| `list_form_fields` | List every fillable field: name, type, the label printed next to the box, current value, and valid options. |
| `fill_form` | Fill fields from a JSON object of `{field name: value}`, read every value back, and attach the filled PDF to the chat. Shows the model the pages it wrote to. Optionally flatten it. |
| `render_page` | Hand one page to the model as an image, to check a filled form or read a flat or scanned one. Needs a model that can read images. |

Flat and scanned PDFs have no fields to fill. The tool reports that instead of
drawing text over the page.

## Installation

1. Make PyMuPDF available to the Open WebUI process. The tool has no
   `requirements:` line on purpose, so Open WebUI will not try to install it.
   With a `uvx`-based systemd unit, add `--with pymupdf` to the command.
2. In Open WebUI, go to **Admin > Tools > +**, paste the contents of
   `pdf_tool_pymupdf_v2.py`, and save.
3. Enable the tool for a model (**Admin > Models > model > Tools**) or toggle
   it in the chat's **+** menu.

## Settings (valves)

| Valve | Default | Meaning |
|---|---|---|
| `flatten_by_default` | `false` | Flatten filled forms so values become fixed page content. |
| `base_url` | empty | Public URL of the Open WebUI, used to build full download links. When empty, the WebUI URL from **Admin > Settings > General** is used, then the request host. |
| `render_dpi` | `110` | Resolution of page images sent to the model (40 to 300). |
| `render_max_kb` | `3000` | Largest page image sent to the model, in kilobytes. Larger pages are sent as JPEG or at lower resolution. |
| `review_pages` | `3` | How many filled pages `fill_form` shows the model for checking (0 to 4). Only pages that were written to are shown. Needs Open WebUI 0.11.4 or later. |

## Usage

Attach a PDF form to a chat and ask the model to fill it. A typical exchange
makes three calls:

1. `list_form_fields` to get the exact field names and options.
2. `fill_form` with every value in one call, for example:

   ```json
   {"Name": "Jane Doe", "Date": "09/16/2026", "Agree": true, "Plan": "/A"}
   ```

3. `render_page` with the `file_id` that `fill_form` returned, for any page
   in `pages_touched` that `fill_form` did not already show.

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
the model left blank is not taken for a write that failed.

`pages_touched` lists the pages that were written to. On Open WebUI 0.11.4
or later those pages, up to `review_pages`, reach the model as images in the
same result. On older versions, and for pages over the limit, `next` tells
the model to call `render_page`. Images reach the model only through a
tool's return value; `__event_emitter__` shows files to the user, not to the
model.

## What it handles

- **Forms saved by Apple's Preview**, which keeps two copies of every box. The
  field tree is relinked to the boxes on the pages, so filled values show up
  in Preview and read back correctly.
- **Boxes without a form dictionary**, left behind when a PDF is merged, split
  or re-saved. The form dictionary is rebuilt.
- **Checkbox and radio values** are stored as PDF names, so Preview draws the
  selected box. The form's own check mark drawings are kept.
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
`fill_pdf_fields`, `render_pdf_page`) import nothing from Open WebUI and can
be used on their own:

```python
from pdf_tool_pymupdf_v2 import inspect_pdf_fields, fill_pdf_fields

print(inspect_pdf_fields("form.pdf"))
pdf_bytes, unknown = fill_pdf_fields("form.pdf", {"Name": "Jane Doe"})
open("form-filled.pdf", "wb").write(pdf_bytes)
```

## Tests

The scripts in `tests/` read values back with pypdf and render pages with
Ghostscript and Preview's engine, so the tool does not grade itself. They need
`uv` and Ghostscript (`gs`); the Preview-engine checks need macOS. Sample PDFs
are not included in this repository; the scripts expect them in the project
folder.

```
uvx --with pymupdf --with pypdf --with pydantic --with pdfplumber --with pillow --with numpy \
  python tests/harness.py . /tmp/pdf_out pdf_tool_pymupdf_v2.py
```

See [tests/README.md](tests/README.md) for all three suites and how to read
their output.

## License

AGPL-3.0, as declared in the tool's header.

PyMuPDF is itself AGPL-3.0 (or commercial, from Artifex). Running it behind a
hosted service can oblige you to offer the source to the service's users.
