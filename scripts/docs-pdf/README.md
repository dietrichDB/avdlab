# docs-pdf — the documentation PDF

`build.py` turns `docs/1.0/*.md` into one A4 PDF in the application's look (navy nav / Arista blue, the sidebar
icons from `frontend/src/components/Brand.tsx`, Inter + JetBrains Mono):

```bash
python3 scripts/docs-pdf/build.py                     # -> dist/avdlab-<version>-documentation.pdf
python3 scripts/docs-pdf/build.py --skip-shots        # reuse the screenshots already in .work/shots
python3 scripts/docs-pdf/build.py --force             # re-render diagrams and retake screenshots
scripts/release.py patch --tar DIR --pdf              # as part of a release
```

Stages, each cached under `.work/` (git-ignored; delete it to start over):

| stage | what | needs |
|---|---|---|
| fonts | Inter + JetBrains Mono from Google Fonts (OFL); installed to `~/.fonts` so diagrams use them too; falls back to system fonts offline | `curl` |
| diagrams | `docs/1.0/diagrams/*.mmd` re-rendered with `mermaid-theme.json` (app colours); very wide `flowchart LR` files are flipped to `TB`; diagrams wider than 1600 px get a landscape page | node (`npx @mermaid-js/mermaid-cli@11`), a Chromium (Playwright's is reused) |
| shots | `shots.py` starts `vite --port 5199` with `VITE_MOCK=1` in `frontend/` and screenshots every page and lab tab at 1440×900 @2x; the mock version badge is rewritten to the real `VERSION` | `npm ci` done in `frontend/`, `pip install playwright && playwright install chromium` |
| pdf | markdown → HTML (python-markdown), two Chromium renders (the first locates every heading with `pdftotext`, the second fills the contents), cover rendered without header/footer and merged with pypdf | `pip install markdown playwright pypdf`, poppler-utils |

Where screenshots go is the `SHOT_MAP` table in `build.py` (chapter file → heading prefix → `[(shot, caption)]`);
add a page to `shots.py` and a row there to document a new screen. Chapter order, icons and blurbs are `CHAPTERS`.
