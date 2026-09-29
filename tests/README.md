# Tests for the PDF form filler

These scripts grade a tool file without trusting the tool to grade itself.
Values are read back with pypdf, and pages are rendered with Ghostscript and
with Preview's engine (`sips`, macOS only).

Requirements: `uv`, Ghostscript (`gs`), macOS for the Preview-engine checks.

## Full fill and render suite

Fills every field of every PDF in a folder, editable and flattened.

```
uvx --with pymupdf --with pypdf --with pydantic --with pdfplumber --with pillow --with numpy \
  python tests/harness.py <project folder> <output folder> <tool file> [folder of PDFs]
```

Example, run from the project folder:

```
uvx --with pymupdf --with pypdf --with pydantic --with pdfplumber --with pillow --with numpy \
  python tests/harness.py . /tmp/pdf_out pdf_tool_pymupdf_v1.py
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

## Tool functions and edge cases

Runs `list_form_fields` and `fill_form` against stand-ins for the Open WebUI
modules. Storage, download links and search indexing on a real server are not
covered.

```
uvx --with pymupdf --with pypdf --with pydantic --with pdfplumber \
  python tests/e2e.py <project folder> <output folder> <tool file>
```

## Page images for the model

Tests `render_page`, which hands a page to the model as a picture. Only tool
files that have `render_page` (the PyMuPDF build from v2 on).

```
uvx --with pymupdf --with pypdf --with pydantic --with pillow --with numpy --with cryptography \
  python tests/render.py <project folder> <output folder> <tool file> [folder of extra PDFs]
```

Not covered: whether the model on a real server accepts the image. Open WebUI
0.11.1 turns a tool result that starts with `data:image/` into a picture for
the model (`process_tool_result` in `backend/open_webui/utils/middleware.py`).

## Known expected result

`libreoffice-form.pdf` from https://github.com/py-pdf/sample-files reports one
problem in Preview's engine for the box `First Name_2`. That box is blank in
the untouched original too.
