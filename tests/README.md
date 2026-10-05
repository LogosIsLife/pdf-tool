# Tests for the PDF form filler

These scripts grade a tool file without trusting the tool to grade itself.
Values are read back with pypdf, and pages are rendered with Ghostscript and
with Preview's engine (`sips`, macOS only).

Requirements: `uv`, Ghostscript (`gs`), macOS for the Preview-engine checks.

## Full fill and render suite

Fills every field of every PDF in a folder, editable and flattened. A field
the tool splits into one field per box (see `split_fields` below) is planned
with a different value in each box.

```
uvx --with pymupdf --with pypdf --with pydantic --with pdfplumber --with pillow --with numpy \
  python tests/harness.py <project folder> <output folder> [tool file] [folder of PDFs]
```

The tool file defaults to `pdf_tool.py` in the project folder and may be an
absolute path. Example, with the sample PDFs in `~/pdf_maker`, run from this
repository:

```
uvx --with pymupdf --with pypdf --with pydantic --with pdfplumber --with pillow --with numpy \
  python tests/harness.py ~/pdf_maker /tmp/pdf_out $PWD/pdf_tool.py
```

A form passes when it prints no `PROBLEM` line. What each line means:

| Line | Meaning |
|---|---|
| tree_values_ok | Values read back from the form's field tree |
| page_values_ok | Values read back from the boxes on the pages |
| button_widgets | Checkbox and radio state, and value stored as a PDF name |
| text_widgets | Text boxes whose drawing contains the value |
| drawn_qz / drawn_gs | Boxes visibly drawn in Preview's engine / Ghostscript |
| gs_whole_document | Pages render the same in a whole-document run as one at a time |
| flat_text_in_box | Flattened text sits inside its original box |
| flat_checks_drawn | Flattened check marks present, and absent where unselected |
| split_fields | Fields the tool split because one field was drawn in several differently labelled boxes; each new field must exist and the old one must be gone |

## Tool functions and edge cases

Runs `list_form_fields`, `fill_form` and `flatten_form` against stand-ins for
the Open WebUI modules, including name matching, the read-back of written
values, the `status` contract and the page images in the result. It also
covers the 1.4.0 behaviour: a field drawn in several differently labelled
boxes is split into one field per box; text is shrunk to fit its box but never
below `min_font_size`, wrapped text stops at the bottom of the box, and a value
that still does not fit is reported in `text_cut_off`; `flatten_form` flattens
a filled copy without resending its values; and the listing tells the model
that a printed line with no field cannot be filled. Storage, download links
and search indexing on a real server are not covered.

```
uvx --with pymupdf --with pypdf --with pydantic --with pdfplumber --with numpy \
  python tests/e2e.py <project folder> <output folder> [tool file]
```

## Page images for the model

Tests `render_page`, which hands a page to the model as a picture.

```
uvx --with pymupdf --with pypdf --with pydantic --with pillow --with numpy --with cryptography \
  python tests/render.py <project folder> <output folder> [tool file] [folder of extra PDFs]
```

Not covered: whether the model on a real server accepts the image. Open WebUI
0.11.1 turns a tool result that starts with `data:image/` into a picture for
the model (`process_tool_result` in `backend/open_webui/utils/middleware.py`).

## Known expected result

`libreoffice-form.pdf` from https://github.com/py-pdf/sample-files reports one
problem in Preview's engine for the box `First Name_2`. That box is blank in
the untouched original too.
