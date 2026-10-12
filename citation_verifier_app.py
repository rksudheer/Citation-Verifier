#!/usr/bin/env python3
"""
Citation Verifier (live) - paste a reference list, watch each reference get checked in real time.

Run:      python citation_verifier_app.py
Then open http://127.0.0.1:8765 (it opens automatically).

Needs only Python 3.9+. No packages to install.

What it checks, per reference:
  1. If there is a DOI or PMID, it looks it up (Crossref / PubMed) and compares the
     title, first author and year with what you wrote.
  2. If the identifier points to a different paper, or there is no identifier, it searches
     Crossref and OpenAlex for the closest real paper.
  3. It checks whether the paper has been retracted (OpenAlex and Crossref).

Verdicts:
  MATCH      title, first author and year agree with a real record
  REVIEW     something differs, or the identifier points to a different paper than the one
             you described. Look at it yourself. This is NOT a finding of fabrication.
  MISMATCH   the identifier is real but belongs to a paper with a different title,
             and no other real paper matches what you wrote
  NOT FOUND  no identifier or search result matches
  ERROR      a lookup failed (network, rate limit)

It checks that a reference EXISTS and is described correctly. It does not check
that the paper supports the sentence you cited it for.
"""

import argparse
import difflib
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CR = "https://api.crossref.org/works"
OA = "https://api.openalex.org/works"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"<>]+", re.I)
PMID_RE = re.compile(r"(?:pmid\W{0,3}|pubmed/)(\d{4,9})", re.I)
STOP = {"the", "and", "of", "in", "on", "for", "with", "an", "to", "by", "from", "at", "as", "or"}
MAX_REFS = 200


# ---------- network ----------

def get(url, email=""):
    """GET json. Returns None on 404. Retries once on 429/5xx."""
    host = urllib.parse.urlparse(url).netloc
    if email and host in ("api.crossref.org", "api.openalex.org"):
        url += ("&" if "?" in url else "?") + "mailto=" + urllib.parse.quote(email)
    ua = "citation-verifier/0.1.0" + (" (mailto:%s)" % email if email else "")
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == 1 and e.code in (429, 500, 502, 503, 504):
                time.sleep(1.5)
                continue
            raise


# ---------- text helpers ----------

def norm(t):
    t = re.sub(r"<[^>]+>", " ", (t or "").lower())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", t)).strip()


def tokens(t):
    return [w for w in norm(t).split() if w not in STOP and len(w) > 1]


def find_doi(ref):
    m = DOI_RE.search(ref)
    return m.group(0).rstrip(".,;:)]}>'\"") if m else None


def find_pmid(ref):
    m = PMID_RE.search(ref)
    return m.group(1) if m else None


def _short(words):
    return "'" + " ".join(words[:6]) + (" ..." if len(words) > 6 else "") + "'"


def title_diff(ref, title):
    """Say which title words differ between the record and the reference."""
    a, b = norm(ref).split(), norm(title).split()
    if not b:
        return None
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    anchors = [i for i, o in enumerate(ops) if o[0] == "equal" and o[2] - o[1] >= 2]
    if not anchors:
        return None
    items = []
    for i, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "equal":
            continue
        mine, theirs = a[i1:i2], b[j1:j2]
        if not ({w for w in mine + theirs if len(w) > 1} - STOP):
            continue  # only small words such as "and" differ
        inside = anchors[0] < i < anchors[-1]
        if inside and theirs and mine:
            items.append("record %s vs yours %s" % (_short(theirs), _short(mine)))
        elif theirs:
            items.append("record has %s that yours lacks" % _short(theirs))
        elif inside and mine:
            items.append("yours has extra %s" % _short(mine))
    return ("title differs: " + "; ".join(items[:3])) if items else None


def author_order_note(ref, authors):
    """Check that the first few authors in the record appear in the same order in the reference."""
    text = norm(ref)
    last, found, bad = -1, [], False
    for name in authors[:6]:
        n = norm(name)
        if not n:
            continue
        occ = [m.start() for m in re.finditer(r"\b%s\b" % re.escape(n), text)]
        if not occ:
            continue
        later = [o for o in occ if o > last]
        if later:
            last = later[0]
            found.append((later[0], name))
        else:
            bad = True
            found.append((occ[0], name))
    if not bad:
        return None
    return "author order differs from the record: record %s; yours %s" % (
        ", ".join(n for _, n in found), ", ".join(n for _, n in sorted(found)))


def compare(ref, rec):
    """Returns (status, title_score, notes)."""
    ref_tokens = set(tokens(ref))
    tt = tokens(rec["title"])
    score = sum(1 for t in tt if t in ref_tokens) / len(tt) if tt else 0.0
    author_ok = bool(rec["first_author"]) and norm(rec["first_author"]) in norm(ref)
    years = {int(y) for y in re.findall(r"\b(19\d{2}|20\d{2})\b", ref)}
    y = rec.get("year")
    ys = rec.get("years") or ([y] if y else [])
    ystat = ("unknown" if not ys else "ok" if any(v in years for v in ys)
             else "off1" if any(abs(v - w) == 1 for v in ys for w in years) else "diff")
    notes = []
    if score < 0.8:
        notes.append("title only %d%% similar to the record" % round(score * 100))
    if not author_ok:
        notes.append("first author '%s' not found in your reference" % rec["first_author"])
    if ystat in ("off1", "diff"):
        notes.append("record year is %s" % "/".join(str(v) for v in ys) + (" (often online vs print year)" if ystat == "off1" else ""))
    for extra in (title_diff(ref, rec["title"]), author_order_note(ref, rec.get("authors") or [])):
        if extra:
            notes.append(extra)
    if score >= 0.8 and author_ok and ystat in ("ok", "unknown"):
        return "MATCH", score, notes
    return ("REVIEW" if score >= 0.5 else "MISMATCH"), score, notes


# ---------- sources ----------

def from_crossref(it):
    au = it.get("author") or []
    years = []
    for k in ("issued", "published-print", "published-online", "published"):
        p = (it.get(k) or {}).get("date-parts") or [[None]]
        if p and p[0] and p[0][0] and int(p[0][0]) not in years:
            years.append(int(p[0][0]))
    year = years[0] if years else None
    flagged = any(u.get("type") in ("retraction", "withdrawal") for u in it.get("updated-by", []))
    return {"title": re.sub(r"<[^>]+>", "", (it.get("title") or [""])[0]),
            "first_author": au[0].get("family", "") if au else "",
            "year": year, "years": years, "journal": (it.get("container-title") or [""])[0],
            "doi": it.get("DOI", ""), "retracted": True if flagged else None, "source": "Crossref",
            "authors": [a.get("family", "") for a in au if a.get("family")]}


def from_openalex(w):
    au = w.get("authorships") or []
    name = ((au[0].get("author") or {}).get("display_name") or "") if au else ""
    src = ((w.get("primary_location") or {}).get("source") or {})
    return {"title": w.get("title") or "", "first_author": name.split()[-1] if name else "",
            "year": w.get("publication_year"), "journal": src.get("display_name") or "",
            "doi": (w.get("doi") or "").replace("https://doi.org/", ""),
            "retracted": w.get("is_retracted"), "source": "OpenAlex",
            "authors": [n.split()[-1] for n in
                        (((x.get("author") or {}).get("display_name") or "") for x in au) if n]}


def from_pubmed(r):
    au = r.get("authors") or []
    m = re.search(r"\d{4}", r.get("pubdate", ""))
    doi = next((a.get("value", "") for a in r.get("articleids", []) if a.get("idtype") == "doi"), "")
    return {"title": (r.get("title") or "").rstrip("."),
            "first_author": au[0]["name"].split()[0] if au else "",
            "year": int(m.group(0)) if m else None, "journal": r.get("source", ""),
            "doi": doi, "retracted": None, "source": "PubMed",
            "authors": [x["name"].split()[0] for x in au if x.get("name")]}


def search(ref, email):
    """Search Crossref and OpenAlex by the reference text. Returns best (record, score)."""
    q = re.sub(r"^\s*(\[\d+\]|\d+[.)])\s*", "", DOI_RE.sub("", ref))[:300]
    cands = []
    try:
        d = get("%s?query.bibliographic=%s&rows=3" % (CR, urllib.parse.quote(q)), email)
        cands += [from_crossref(i) for i in ((d or {}).get("message", {}).get("items") or [])]
    except Exception:
        pass
    try:
        d = get("%s?search=%s&per-page=3" % (OA, urllib.parse.quote(q)), email)
        cands += [from_openalex(w) for w in ((d or {}).get("results") or [])]
    except Exception:
        pass
    best, bs = None, -1
    for c in cands:
        s = compare(ref, c)[1]
        if s > bs:
            best, bs = c, s
    return best, bs


def retraction(rec, email):
    if rec.get("retracted"):
        return True
    if not rec.get("doi"):
        return None
    try:
        w = get("%s/doi:%s?select=is_retracted" % (OA, urllib.parse.quote(rec["doi"], safe="/")), email)
    except Exception:
        return None
    return None if w is None else bool(w.get("is_retracted"))


# ---------- the check ----------

def check(ref, email):
    res = {"ref": ref, "status": "NOT FOUND", "notes": [], "record": None, "candidate": None, "retracted": None}
    try:
        doi = find_doi(ref)
        pmid = None if doi else find_pmid(ref)
        rec, idnote = None, ""

        if doi:
            d = get("%s/%s" % (CR, urllib.parse.quote(doi, safe="/")), email)
            if d:
                rec = from_crossref(d["message"])
            else:
                h = get("https://doi.org/api/handles/" + urllib.parse.quote(doi, safe="/"), email)
                if h and h.get("responseCode") == 1:
                    res.update(status="REVIEW", notes=["DOI exists but is not in Crossref (another registry, e.g. DataCite). Compare by hand."])
                    return res
                idnote = "DOI %s does not exist" % doi
        elif pmid:
            d = get("%sesummary.fcgi?db=pubmed&retmode=json&id=%s" % (EUTILS, pmid), email)
            r = ((d or {}).get("result") or {}).get(pmid)
            if r and not r.get("error"):
                rec = from_pubmed(r)
            else:
                idnote = "PMID %s does not exist" % pmid

        if rec:
            st, _, notes = compare(ref, rec)
            res.update(status=st, notes=notes, record=rec)
            if st == "MISMATCH":
                best, bs = search(ref, email)
                if best and bs >= 0.8:
                    res.update(status="REVIEW", candidate=best,
                               notes=["identifier points to a different paper; your title matches the candidate below"])
                else:
                    res["notes"].insert(0, "identifier is real but belongs to a paper with a different title")
        else:
            best, bs = search(ref, email)
            if best:
                st, _, notes = compare(ref, best)
                if st == "MISMATCH":
                    res["notes"] = [idnote] if idnote else ["no matching record found in Crossref or OpenAlex"]
                else:
                    if idnote:
                        st, notes = "REVIEW", [idnote + "; the closest real paper is below"] + notes
                    res.update(status=st, notes=notes, record=best)
            else:
                res["notes"] = [idnote or "no matching record found"]

        final = res["record"]
        if final and final.get("doi"):
            res["retracted"] = retraction(final, email)
    except Exception as e:
        res.update(status="ERROR", notes=[str(e)])
    return res


# ---------- reading a Word file ----------

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
REF_HEAD = re.compile(r"^\W*(\d+[.)]?\s*)?(references?|reference list|bibliography|literature cited|works cited)\W*$", re.I)
END_HEAD = re.compile(r"^\W*(appendix|appendices|supplementary.*|supporting information|figure legends?|figure captions?|"
                      r"tables?|acknowledg.*|funding|conflicts? of interest|declarations?|author contributions?|"
                      r"data availability.*|abbreviations)\W*$", re.I)


def docx_paragraphs(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if z.getinfo("word/document.xml").file_size > 60000000:
            raise ValueError("document.xml is too large")
        xml = z.read("word/document.xml")
    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        raise ValueError("DTD and entity declarations are not allowed")
    root = ET.fromstring(xml)
    out = []
    for p in root.iter(W + "p"):
        parts = []
        for el in p.iter():
            if el.tag == W + "t":
                parts.append(el.text or "")
            elif el.tag in (W + "tab", W + "br"):
                parts.append(" ")
        t = re.sub(r"\s+", " ", "".join(parts)).strip()
        if t:
            out.append(t)
    return out


def extract_refs(paras):
    """Take the paragraphs after the last 'References' heading. Without a heading,
    fall back to paragraphs that contain a DOI or PMID."""
    heads = [i for i, t in enumerate(paras) if len(t) < 40 and REF_HEAD.match(t)]
    if heads:
        refs = []
        for t in paras[heads[-1] + 1:]:
            if len(t) < 40 and END_HEAD.match(t):
                break
            refs.append(t)
        return [t for t in refs if len(t) >= 25], "the paragraphs after the heading '%s'" % paras[heads[-1]]
    return [t for t in paras if DOI_RE.search(t) or PMID_RE.search(t)], \
        "paragraphs containing a DOI or PMID, because no References heading was found"


# ---------- server ----------

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Citation Verifier</title>
<style>
:root{--bg:#fff;--fg:#1a1a1a;--mut:#666;--bd:#ddd;--card:#fafafa;--ok:#1a7f37;--rev:#9a6700;--bad:#cf222e;--gr:#6e7781}
@media(prefers-color-scheme:dark){:root{--bg:#161616;--fg:#eee;--mut:#999;--bd:#333;--card:#1e1e1e;--ok:#3fb950;--rev:#d29922;--bad:#f85149;--gr:#8b949e}}
body{font:15px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg);max-width:860px;margin:0 auto;padding:20px}
h1{font-size:22px;margin:0 0 4px}p.s{color:var(--mut);margin:0 0 14px}
textarea{width:100%;height:190px;box-sizing:border-box;padding:10px;font:14px/1.4 ui-monospace,monospace;background:var(--card);color:var(--fg);border:1px solid var(--bd);border-radius:6px}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0;align-items:center}
input{padding:8px;border:1px solid var(--bd);border-radius:6px;background:var(--card);color:var(--fg);flex:1;min-width:200px}
button{padding:8px 16px;border:0;border-radius:6px;background:#2563eb;color:#fff;font-size:15px;cursor:pointer}
button.g{background:var(--card);color:var(--fg);border:1px solid var(--bd)}button:disabled{opacity:.5}
#sum{margin:14px 0;font-weight:600}.card{border:1px solid var(--bd);border-left:5px solid var(--gr);border-radius:6px;padding:10px 12px;margin:8px 0;background:var(--card)}
.card.MATCH{border-left-color:var(--ok)}.card.REVIEW{border-left-color:var(--rev)}.card.MISMATCH,.card.NOTFOUND{border-left-color:var(--bad)}
.badge{display:inline-block;font-size:12px;font-weight:700;padding:1px 8px;border-radius:10px;color:#fff;background:var(--gr);margin-right:6px}
input[type=file]{flex:0 0 auto;min-width:0}
.b-MATCH{background:var(--ok)}.b-REVIEW{background:var(--rev)}.b-MISMATCH,.b-NOTFOUND,.b-RET{background:var(--bad)}
.ref{margin:6px 0;word-break:break-word}.note{color:var(--mut);font-size:14px}.rec{font-size:14px;margin-top:4px}.note.flag{color:var(--rev)}
a{color:#2563eb}small{color:var(--mut)}
</style></head><body>
<h1>Citation Verifier</h1>
<p class="s">Paste references (one per line, or blank-line separated). Results appear as each one is checked. Everything runs on your computer; only the references are sent to Crossref, OpenAlex and PubMed.</p>
<div class="bar"><input type="file" id="file" accept=".docx"><span id="fnote" class="note">Or load a Word (.docx) manuscript and its reference list will be filled in below.</span></div>
<textarea id="refs" placeholder="1. Author A, Author B. Title of the paper. Journal. 2021;12:345-56. doi:10.xxxx/xxxxx"></textarea>
<div class="bar"><input id="email" placeholder="Your email (optional, gives faster service from Crossref and OpenAlex)">
<button id="go">Check references</button><button class="g" id="csv" disabled>Download CSV</button></div>
<div id="sum"></div><div id="out"></div>
<small>This checks that a reference exists and is described correctly. It does not check that the paper supports your claim. REVIEW and NOT FOUND mean "look yourself", not "fake".</small>
<script>
const $=id=>document.getElementById(id);let R={};
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const cls=s=>s.replace(' ','');
function split(t){let b=t.split(/\n\s*\n/).map(x=>x.trim()).filter(Boolean);
 if(b.length<=1)b=t.split('\n').map(x=>x.trim()).filter(Boolean);return b.map(x=>x.replace(/\s*\n\s*/g,' '));}
function rec(x,l){if(!x)return'';return `<div class="rec"><b>${l}:</b> ${esc(x.title)} - ${esc(x.first_author)} (${x.year||'?'}), ${esc(x.journal)} `+
 (x.doi?`<a href="https://doi.org/${encodeURI(x.doi)}" target="_blank" rel="noopener">${esc(x.doi)}</a> `:'')+`<small>via ${esc(x.source)}</small></div>`;}
function draw(n,r){const el=$('r'+n);if(!r){return;}el.className='card '+cls(r.status);
 el.innerHTML=`<span class="badge b-${cls(r.status)}">${esc(r.status)}${r.status==='MATCH'&&r.notes.length?' - see note':''}</span>`+(r.retracted?'<span class="badge b-RET">RETRACTED / NOTICE</span>':'')+
 `<small>#${n+1}</small><div class="ref">${esc(r.ref)}</div>`+(r.notes.length?`<div class="note${r.status==='MATCH'?' flag':''}">${r.notes.map(esc).join('; ')}</div>`:'')+
 rec(r.record,'Found')+rec(r.candidate,'Closest real paper');}
function summary(total){const c={};Object.values(R).forEach(r=>{c[r.status]=(c[r.status]||0)+1;if(r.retracted)c.RETRACTED=(c.RETRACTED||0)+1;});
 $('sum').textContent=`${Object.keys(R).length}/${total} checked  `+Object.entries(c).map(([k,v])=>`${k}: ${v}`).join('   ');}
$('file').onchange=async e=>{const f=e.target.files[0];if(!f)return;$('fnote').textContent='Reading '+f.name+'...';
 try{const r=await (await fetch('/api/docx',{method:'POST',body:f})).json();
  if(r.error){$('fnote').textContent=r.error;return;}
  $('refs').value=r.refs.join('\n\n');
  $('fnote').textContent=r.refs.length?`Found ${r.total} references (from ${r.how}). Read through the list, fix anything that was split or joined wrongly, then click Check references.`+(r.total>r.refs.length?` Only the first ${r.refs.length} were loaded.`:''):'No references found. Paste them into the box instead.';
 }catch(x){$('fnote').textContent='Could not read the file: '+x;}};
$('go').onclick=async()=>{const refs=split($('refs').value);if(!refs.length)return;R={};$('csv').disabled=true;$('go').disabled=true;
 $('out').innerHTML=refs.map((r,i)=>`<div class="card" id="r${i}"><small>#${i+1} checking...</small><div class="ref">${esc(r)}</div></div>`).join('');summary(refs.length);
 try{const resp=await fetch('/api/check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({refs,email:$('email').value})});
  const rd=resp.body.getReader(),dec=new TextDecoder();let buf='';
  for(;;){const {done,value}=await rd.read();if(done)break;buf+=dec.decode(value,{stream:true});let i;
   while((i=buf.indexOf('\n'))>=0){const line=buf.slice(0,i);buf=buf.slice(i+1);if(line.trim()){const r=JSON.parse(line);R[r.n]=r;draw(r.n,r);summary(refs.length);}}}
 }catch(e){$('sum').textContent='Connection lost: '+e;}
 $('go').disabled=false;$('csv').disabled=!Object.keys(R).length;};
$('csv').onclick=()=>{const q=v=>'"'+String(v==null?'':v).replace(/"/g,'""')+'"';
 const rows=[['n','status','retracted','notes','your_reference','found_title','found_first_author','found_year','found_doi','source']];
 Object.keys(R).sort((a,b)=>a-b).forEach(k=>{const r=R[k],x=r.record||{};rows.push([+k+1,r.status,r.retracted?'yes':'',r.notes.join('; '),r.ref,x.title,x.first_author,x.year,x.doi,x.source]);});
 const a=document.createElement('a');a.href=URL.createObjectURL(new Blob(['\ufeff'+rows.map(r=>r.map(q).join(',')).join('\n')],{type:'text/csv'}));a.download='citation_check.csv';a.click();};
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    enforce_local = True   # set to False in main() only when the user chooses a non-local --host
    timeout = 120

    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def _allowed(self):
        """Refuse requests whose Host or Origin is not this computer (blocks other websites
        from talking to the local server through the browser)."""
        if not self.enforce_local:
            return True
        host = self.headers.get("Host") or ""
        if (urllib.parse.urlparse("//" + host).hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
            return False
        origin = self.headers.get("Origin")
        return not origin or origin == "http://" + host

    def do_GET(self):
        if not self._allowed():
            return self.send_error(403)
        if self.path in ("/", "/index.html"):
            b = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        else:
            self.send_error(404)

    def docx(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self.send_error(400)
        if n < 0 or n > 25000000:
            return self.send_error(413)
        data = self.rfile.read(n)
        try:
            refs, how = extract_refs(docx_paragraphs(data))
            out = {"refs": refs[:MAX_REFS], "total": len(refs), "how": how}
        except Exception:
            out = {"error": "Could not read this file. Only .docx files work (not .doc or PDF)."}
        b = json.dumps(out).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        if not self._allowed():
            return self.send_error(403)
        if self.path == "/api/docx":
            return self.docx()
        if self.path != "/api/check":
            return self.send_error(404)
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n < 0 or n > 2000000:
                return self.send_error(413)
            body = json.loads(self.rfile.read(n))
        except Exception:
            return self.send_error(400)
        refs = [str(r) for r in body.get("refs", []) if str(r).strip()][:MAX_REFS]
        email = re.sub(r"[^A-Za-z0-9@._+-]", "", str(body.get("email", "")))[:100]
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        ex = ThreadPoolExecutor(max_workers=4)
        try:
            futs = {ex.submit(check, r, email): i for i, r in enumerate(refs)}
            for f in as_completed(futs):
                res = f.result()
                res["n"] = futs[f]
                self.wfile.write((json.dumps(res) + "\n").encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser closed the page
        finally:
            ex.shutdown(wait=False, cancel_futures=True)


def main():
    ap = argparse.ArgumentParser(description="Live citation verifier")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="keep 127.0.0.1 unless you add your own access control")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        Handler.enforce_local = False
        print("WARNING: listening on %s. This tool has no login and no rate limit. Anyone who can reach it can use it." % a.host)
    srv = None
    for port in range(a.port, a.port + 20):   # if the port is busy (e.g. a second copy is open), try the next one
        try:
            srv = ThreadingHTTPServer((a.host, port), Handler)
            break
        except OSError:
            continue
    if srv is None:
        print("Could not find a free port starting at %d. Close any other copy of this program and try again." % a.port)
        if sys.stdin and sys.stdin.isatty():
            input("Press Enter to close this window.")
        raise SystemExit(1)
    url = "http://%s:%d" % (a.host, port)
    print("Citation Verifier is running at " + url)
    print("Your browser should open by itself. Keep this window open while you use it.")
    print("Close this window (or press Ctrl+C) to quit.")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
