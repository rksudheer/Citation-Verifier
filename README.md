# Citation Verifier
Created by Dr. Sudheer Aluru, Oral Microbiology Research Laboratory, Department of Bioclinical Sciences, Faculty of Dentistry, Health Sciences Center, Kuwait University, Kuwait. **This is a personal project and not an official product of Kuwait University**.

A small tool that checks whether the references in a manuscript exist and are described correctly. You paste a reference list (or load a Word file), and each reference is checked live against Crossref, OpenAlex and PubMed. Results appear one by one as they come in.

**Version 0.1.0, an early release.** It works, but it has only been tried on a small number of real reference lists. There is no measured accuracy figure. Please read "What it does not do" before relying on it.

## Download for Windows

No Python needed. Download `CitationVerifier.exe` from the [latest release](../../releases/latest) and double-click it. A black window opens, then a page in your browser. Keep the window open while you work, and close it to quit.

The first time, Windows may say "Windows protected your PC" because the program is not code-signed. Click **More info**, then **Run anyway**. The file is built automatically by GitHub from the code in this repository. To confirm that your download is unaltered, compare its checksum with the `.sha256.txt` file on the release page. In PowerShell, run `Get-FileHash .\CitationVerifier.exe -Algorithm SHA256`.

Landing page: https://rksudheer.github.io/Citation-Verifier/

## What it checks

For each reference it:

1. Looks up the DOI (Crossref) or PMID (PubMed) and compares the title, first author and year with what you wrote.
2. If the identifier points to a different paper, or there is none, searches Crossref and OpenAlex for the closest real paper.
3. Checks whether the paper has been retracted (OpenAlex and Crossref).
4. Shows which title words differ from the record, and whether the first authors appear in a different order.

| Verdict | Meaning |
|---|---|
| MATCH | Title, first author and year agree with a real record. A note may still show small differences. |
| REVIEW | Something differs, or the identifier points to a different paper than the one you described. Look at it yourself. This is not a finding of fabrication. |
| MISMATCH | The identifier is real but belongs to a paper with a different title, and no other real paper matches. |
| NOT FOUND | No identifier or search result matches. |
| ERROR | A lookup failed (network problem, rate limit). |

## What it does not do

- It does not check that a paper supports the sentence you cite it for. Only reading the paper can do that.
- A single changed word in a long title can still be marked MATCH. The note on the card shows the difference, so read the notes on MATCH cards.
- Only the first six authors are checked for order, and only authors that appear in your reference.
- REVIEW and NOT FOUND mean "look yourself". Crossref and OpenAlex do not cover everything (some books, theses, old papers).
- English-language references only. Other languages have not been tried.
- The retraction check depends on OpenAlex and Crossref and may be incomplete.

## Run from the source code (needs Python)

You need Python 3.9 or newer. There is nothing to install.

```
python citation_verifier_app.py
```

Then open http://127.0.0.1:8765 (it opens automatically). Stop it with Ctrl+C. If that port is busy, it uses the next free one, and the address is printed in the window.

To use another port: `python citation_verifier_app.py --port 8800`.

### In Jupyter (running on your own computer)

```python
import threading
import citation_verifier_app as cv

srv = cv.ThreadingHTTPServer(("127.0.0.1", 8765), cv.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
# open http://127.0.0.1:8765 in your browser
# to stop: srv.shutdown(); srv.server_close()
```

This does not work on Colab or other remote notebooks, because the address refers to the remote machine.

## Using it

- Paste references into the box, one per line or separated by blank lines, or load a `.docx` manuscript. For a Word file, the references are taken from after the last "References" heading (or "Bibliography", "Literature cited"). Check the loaded list before you start.
- Adding your email is optional. It is sent to Crossref and OpenAlex, which give faster and more reliable service to identified users.
- Up to 200 references per run. Results can be downloaded as CSV.

## Privacy

Your references (and your email, if you enter it) are sent to Crossref, OpenAlex, NCBI PubMed and doi.org to do the lookups. Nothing is stored by this tool. Do not use it on reference lists you must keep confidential. The Word file itself is read on your own computer and is not sent anywhere.

## Security notes

- The server listens on 127.0.0.1 only, so only programs on your own computer can reach it. It also refuses requests whose Host or Origin header is not local, which stops other websites from using it through your browser.
- It has no login and no rate limit. **Do not put it on a public server as it is.** If you start it with a non-local `--host`, it prints a warning and the local-only checks are turned off.
- It uses only the Python standard library, so there are no third-party packages to keep updated.
- Uploaded Word files are limited in size, and files containing XML entity declarations are refused.

To report a security problem, use the repository's private vulnerability reporting (Security tab) rather than a public issue.

## Tests

```
python -m unittest -v
```

The tests use fake data and need no internet connection.

## Status and contributing

This is an early version. Bug reports are welcome, especially a reference that gets a wrong verdict. Please paste the reference and the card text, but do not paste unpublished manuscripts.

Ideas not done yet: `.bib` and `.ris` import, a downloadable report, checking in-text citations against the reference list, and PubMed title search.

## Licence

MIT. See the LICENSE file.

This is an independent project. It is not affiliated with Scholar Sidekick or any other citation-checking service, and it shares no code with them.
