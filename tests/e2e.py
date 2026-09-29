"""End-to-end and edge-case tests, with stand-ins for the Open WebUI modules."""
import sys, io, os, json, types, asyncio, importlib.util, shutil
from pypdf import PdfReader
import pdfplumber
PROJ, OUT, TOOL = sys.argv[1], sys.argv[2], sys.argv[3]

# ---- stand-in Open WebUI modules -------------------------------------------------
STORE = {}
class Rec:
    def __init__(s, **k): s.__dict__.update(k)
class Files:
    @staticmethod
    async def get_file_by_id(fid): return STORE.get(fid)
    @staticmethod
    async def insert_new_file(user_id, form):
        r = Rec(id=form.id, user_id=user_id, filename=form.filename, path=form.path, meta=form.meta); STORE[form.id] = r; return r
class FileForm:
    def __init__(s, **k): s.__dict__.update(k)
class Storage:
    @staticmethod
    def get_file(path): return path
    @staticmethod
    def upload_file(fh, name, tags):
        p = os.path.join(OUT, 'uploads', name); os.makedirs(os.path.dirname(p), exist_ok=True)
        data = fh.read(); open(p, 'wb').write(data); return data, p
for name, attrs in {'open_webui': {}, 'open_webui.models': {}, 'open_webui.models.files': {'Files': Files, 'FileForm': FileForm},
                    'open_webui.storage': {}, 'open_webui.storage.provider': {'Storage': Storage}}.items():
    m = types.ModuleType(name); m.__dict__.update(attrs); sys.modules[name] = m

spec = importlib.util.spec_from_file_location('tool', f'{PROJ}/{TOOL}'); tool = importlib.util.module_from_spec(spec); spec.loader.exec_module(tool)
T = tool.Tools(); T.valves.base_url = 'https://example.test'
USER = {'id': 'u1'}
results = []
def ok(name, cond, detail=''):
    results.append((name, bool(cond), detail)); print(('PASS' if cond else 'FAIL'), '|', name, '|', detail)

def attach(fname, fid):
    STORE[fid] = Rec(id=fid, user_id='u1', filename=fname, path=f'{PROJ}/{fname}')
    return {'id': fid, 'name': fname, 'content_type': 'application/pdf'}
events = []
async def emit(e): events.append(e)
run = lambda c: asyncio.run(c)

# 1 list + fill through the tool methods
f1 = attach('CME Disclosure Form Template.pdf', 'f-disc')
listing = json.loads(run(T.list_form_fields(__user__=USER, __files__=[f1])))
ok('list_form_fields returns fields', listing.get('field_count') == 23, f"field_count={listing.get('field_count')}")
vals = {'Text4': 'Dr. Jane Doe', 'Text5': 'Sepsis Update', 'Text6': '10/15/2026', 'Check Box1': '/Yes', 'Check Box7': '/Yes', 'Nope': 'x'}
out = json.loads(run(T.fill_form(json.dumps(vals), __user__=USER, __files__=[f1], __event_emitter__=emit)))
ok('fill_form status ok', out.get('status') == 'ok', str({k: out.get(k) for k in ('status', 'error', 'file_name', 'flattened')}))
ok('unknown field reported', out.get('ignored_unknown_fields') == ['Nope'], str(out.get('ignored_unknown_fields')))
ok('full download url built', str(out.get('download_url', '')).startswith('https://example.test/api/v1/files/'), str(out.get('download_url')))
ok('file event emitted', bool(events) and events[-1]['type'] == 'files')
if out.get('status') == 'ok':
    rec = STORE[out['file_id']]; r = PdfReader(rec.path); f = r.get_fields()
    ok('stored file has values', f['Text4'].get('/V') == 'Dr. Jane Doe' and str(f['Check Box1'].get('/V')) == '/Yes', f"{f['Text4'].get('/V')!r} {f['Check Box1'].get('/V')!r}")
    ok('untouched field stays empty', f['Check Box2'].get('/V') in (None, '/Off') and not f['Text4'].get('/V') is None, repr(f['Check Box2'].get('/V')))
    ok('still editable', len(f) == 23)

# 2 flatten argument forms
for arg, want in [(None, False), ('false', False), (False, False), ('true', True), (True, True)]:
    o = json.loads(run(T.fill_form(json.dumps({'Text4': 'A'}), flatten=arg, __user__=USER, __files__=[f1])))
    ok(f'flatten={arg!r} -> flattened {want}', o.get('flattened') == want, f"got {o.get('flattened')} {o.get('error','')}")

# 3 other user's file, no pdf, several pdfs, bad json, flat pdf
STORE['f-other'] = Rec(id='f-other', user_id='someone-else', filename='x.pdf', path=f'{PROJ}/205_boc_test.pdf')
o = json.loads(run(T.fill_form('{"a":"b"}', file_id='f-other', __user__=USER, __files__=[]))); ok("another user's file refused", 'another user' in o.get('error', ''), o.get('error', ''))
o = json.loads(run(T.fill_form('{"a":"b"}', __user__=USER, __files__=[]))); ok('no PDF attached -> clear error', 'No PDF' in o.get('error', ''), o.get('error', ''))
f2 = attach('205_boc_test.pdf', 'f-205')
o = json.loads(run(T.fill_form('{"a":"b"}', __user__=USER, __files__=[f1, f2]))); ok('several PDFs -> asks for file_id', 'Several PDFs' in o.get('error', ''), o.get('error', '')[:70])
o = json.loads(run(T.fill_form('{bad', __user__=USER, __files__=[f1]))); ok('bad JSON -> clear error', 'not valid JSON' in o.get('error', ''), o.get('error', '')[:60])
f3 = attach('205_boc_test-filled.pdf', 'f-flat')
o = json.loads(run(T.fill_form('{"a":"b"}', __user__=USER, __files__=[f3]))); ok('flat PDF -> readable error', 'error' in o, o.get('error', '')[:110])
o = json.loads(run(T.list_form_fields(__user__=USER, __files__=[f3]))); ok('flat PDF listing explains', o.get('field_count') == 0 and 'no fillable' in o.get('note', ''), o.get('note', '')[:60])

# 4 values already in the form survive a partial fill, editable and flattened
src = f'{PROJ}/CME Application Form 2026.pdf'
pdf, _ = tool.fill_pdf_fields(src, {'Text1': 'New Title'}, flatten=False); f = PdfReader(io.BytesIO(pdf)).get_fields()
ok('partial fill keeps existing value (editable)', f['Text7'].get('/V') == 'lijlkjkl' and f['Text1'].get('/V') == 'New Title', f"Text7={f['Text7'].get('/V')!r} Text1={f['Text1'].get('/V')!r}")
pdf, _ = tool.fill_pdf_fields(src, {'Text1': 'New Title'}, flatten=True); open(f'{OUT}/partial_flat.pdf', 'wb').write(pdf)
txt = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
ok('partial fill keeps existing value (flattened)', 'lijlkjkl' in txt and 'New Title' in txt, f"existing shown={'lijlkjkl' in txt} new shown={'New Title' in txt}")

# 5 special characters and long text
src = f'{PROJ}/CME Disclosure Form Template.pdf'
special = {'Text4': "José O'Brien (MD)", 'Text5': 'Heart & Lung: 50% / A\\B', 'Text6': 'Zoë — “quoted” ½',
           'Name of Companys and RelationshipRow1': 'A very long company name that certainly cannot fit inside this narrow table cell at all'}
for flat in (False, True):
    try:
        pdf, _ = tool.fill_pdf_fields(src, special, flatten=flat); open(f'{OUT}/special_{"flat" if flat else "edit"}.pdf', 'wb').write(pdf)
        if not flat:
            f = PdfReader(io.BytesIO(pdf)).get_fields(); bad = {k: f[k].get('/V') for k, v in special.items() if f[k].get('/V') != v}
            ok('special characters stored exactly', not bad, str(bad)[:150])
        else:
            t = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
            ok('special characters drawn when flattened', "O'Brien (MD)" in t and 'Heart & Lung: 50% / A\\B' in t, repr([l for l in t.splitlines() if 'Brien' in l or 'Heart' in l or 'Zo' in l][:3]))
    except Exception as e:
        ok(f'special characters flatten={flat}', False, repr(e)[:160])

# 6 radio: choose each option in turn; checkbox: turn on then off again
src = f'{PROJ}/00-985-filled.pdf'
for choice in ('/A', '/B'):
    pdf, _ = tool.fill_pdf_fields(src, {'AOrB': choice}, flatten=False); r = PdfReader(io.BytesIO(pdf))
    states = [str(a.get_object().get('/AS')) for a in r.pages[0]['/Annots'] if '/Parent' in a.get_object() and a.get_object()['/Parent'].get('/T') == 'AOrB']
    ok(f'radio {choice}: exactly that option on', states.count(choice) == 1 and states.count('/Off') == 1, str(states))
src = f'{PROJ}/205_boc_test.pdf'
pdf, _ = tool.fill_pdf_fields(src, {'registered': '/is an organization'}, flatten=False); open(f'{OUT}/cb_on.pdf', 'wb').write(pdf)
pdf2, _ = tool.fill_pdf_fields(f'{OUT}/cb_on.pdf', {'registered': '/Off'}, flatten=False); f = PdfReader(io.BytesIO(pdf2)).get_fields()
ok('checkbox can be turned off again', str(f['registered'].get('/V')) == '/Off', repr(f['registered'].get('/V')))
import inspect
HAS_REJ = 'rejected' in inspect.signature(tool.fill_pdf_fields).parameters
for loose in ('Yes', True, 'true', 'is an organization', '/IS AN ORGANIZATION'):
    pdf3, _ = tool.fill_pdf_fields(src, {'registered': loose}, flatten=False); f = PdfReader(io.BytesIO(pdf3)).get_fields()
    ok(f'checkbox accepts {loose!r}', str(f['registered'].get('/V')) == '/is an organization', f"stored {f['registered'].get('/V')!r}")
pdf3, _ = tool.fill_pdf_fields(src, {'registered': 'no'}, flatten=False); f = PdfReader(io.BytesIO(pdf3)).get_fields()
ok("checkbox 'no' leaves it off", str(f['registered'].get('/V')) in ('/Off', 'None'), repr(f['registered'].get('/V')))
if HAS_REJ:
    rej = []; pdf3, _ = tool.fill_pdf_fields(f'{PROJ}/00-985-filled.pdf', {'AOrB': 'banana', 'Name': 'Kept'}, flatten=False, rejected=rej); f = PdfReader(io.BytesIO(pdf3)).get_fields()
    ok('radio with a bad option is reported, not applied', len(rej) == 1 and str(f['AOrB'].get('/V')) == '/A' and f['Name'].get('/V') == 'Kept', f"rejected={rej} AOrB={f['AOrB'].get('/V')!r}")
    for loose, want in (('b', '/B'), ('A', '/A'), ('/b', '/B')):
        pdf3, _ = tool.fill_pdf_fields(f'{PROJ}/00-985-filled.pdf', {'AOrB': loose}, flatten=False); f = PdfReader(io.BytesIO(pdf3)).get_fields()
        ok(f'radio accepts {loose!r}', str(f['AOrB'].get('/V')) == want, repr(f['AOrB'].get('/V')))
    o = json.loads(run(T.fill_form(json.dumps({'Check Box1': 'maybe', 'Text4': 'Z'}), __user__=USER, __files__=[f1])))
    ok('fill_form reports the unset checkbox', o.get('filled_fields') == ['Text4'] and o.get('not_set_invalid_option', [{}])[0].get('field') == 'Check Box1', str({k: o.get(k) for k in ('filled_fields', 'not_set_invalid_option')})[:200])

# 7 refill of the tool's own output (fill in two steps)
src = f'{PROJ}/CME Disclosure Form Template.pdf'
a, _ = tool.fill_pdf_fields(src, {'Text4': 'Step One'}, flatten=False); open(f'{OUT}/step1.pdf', 'wb').write(a)
b, _ = tool.fill_pdf_fields(f'{OUT}/step1.pdf', {'Text5': 'Step Two'}, flatten=False); f = PdfReader(io.BytesIO(b)).get_fields()
ok('second fill on the first output keeps both', f['Text4'].get('/V') == 'Step One' and f['Text5'].get('/V') == 'Step Two', f"{f['Text4'].get('/V')!r} {f['Text5'].get('/V')!r}")

# 8 listing quality on awkward fields
if hasattr(tool, 'inspect_pdf_fields'):
    L = {e['name']: e for e in tool.inspect_pdf_fields(f'{PROJ}/205_boc_test.pdf')['fields']}
    print('   listing Print Form  :', json.dumps(L.get('Print Form'))[:150])
    L = {e['name']: e for e in tool.inspect_pdf_fields(f'{PROJ}/CME Application Form 2026.pdf')['fields']}
    print('   listing Text7       :', json.dumps(L.get('Text7'))[:200])
    L = {e['name']: e for e in tool.inspect_pdf_fields(f'{PROJ}/CME Broadcast Consent Template.pdf')['fields']}
    print('   listing Text1       :', json.dumps(L.get('Text1'))[:330])
    L = {e['name']: e for e in tool.inspect_pdf_fields(f'{PROJ}/00-985-filled copy.pdf')['fields']}
    print('   listing AOrB (copy) :', json.dumps(L.get('AOrB'))[:260])
n = sum(1 for r in results if r[1]); print(f'\n{TOOL}: {n}/{len(results)} passed'); [print('   FAILED:', r[0], '|', r[2]) for r in results if not r[1]]
