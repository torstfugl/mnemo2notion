# Notion ↔ Mnemo Converter

Move your notes between [Notion](https://www.notion.com) and [Mnemo](https://github.com/onemnemo/mnemo) —
**both ways, formatting included**: equations, text and background colours, headings, callouts,
tables, columns, images, sub-pages, tags and page icons.

- **Notion → Mnemo** produces a `.mnemo` package you import through Mnemo's own
  **Notes → Import** panel.
- **Mnemo → Notion** creates real pages in your Notion workspace through Notion's API —
  images uploaded, colours mapped, structure intact. Your notes are never locked in.

Everything runs on your machine. Notes travel directly between you and Notion; there is no
server in the middle, no account, and no telemetry — the same deal Mnemo itself offers.

## The app

The desktop app walks you through it: paste an integration token, pick the pages you want
from a list, choose your options, convert.

```
pip install -r requirements-gui.txt
python -m notion2mnemo gui
```

Or grab the prebuilt Windows executable from the releases page — no Python needed.

**Setup (once, ~2 minutes):**

1. Create an integration at [notion.so/my-integrations](https://www.notion.so/my-integrations)
   and copy its secret.
2. In Notion, open each top-level page you want to move and choose
   **⋯ → Connections →** your integration. Sub-pages are included automatically.
3. Paste the secret into the app.

For the **Mnemo → Notion** direction the integration also needs *insert content* capability
(on by default for new integrations), and must be connected to the page you want the imported
pages created under.

## The CLI

Everything the app does is also scriptable. See `python -m notion2mnemo --help` for all flags.

```bash
pip install -r requirements.txt
export NOTION_TOKEN=ntn_...            # PowerShell: $env:NOTION_TOKEN = "ntn_..."

# Notion -> Mnemo: everything the integration can see
python -m notion2mnemo -o notes.mnemo

# ...or specific pages/databases (accepts URLs, dashed or bare ids; repeatable)
python -m notion2mnemo --page https://www.notion.so/My-Page-abc... --covers
python -m notion2mnemo --database 1234abcd... --db-properties none

# Mnemo -> Notion: create pages under a chosen parent page
python -m notion2mnemo push notes.mnemo --parent https://www.notion.so/Imports-...
```

The pull direction caches API responses in `.notion-cache/`, so a re-run after tweaking
options costs no API calls, and Ctrl-C is always safe to resume from.

## What carries over

### Notion → Mnemo

| Notion | Mnemo | Notes |
| --- | --- | --- |
| Paragraph, headings 1–3 | `Text`, `Heading1`–`Heading3` | |
| Bulleted / numbered list | `BulletList` / `NumberedList` | Nesting preserved |
| To-do | `Checklist` | Checked state preserved |
| Quote, divider | `Quote`, `Divider` | |
| Callout | `Callout` | Emoji kept; red/orange/yellow → `warn` tone |
| Code | `Code` | Language and caption kept |
| Equation block / inline equation | `Equation` / `EquationSpan` | LaTeX verbatim — both apps render KaTeX |
| Image | `Image` + bundled asset | Downloaded into the package; caption kept |
| Table | `Table` tree | Header row *and* header column |
| Columns | `TwoColumn` | Widths kept; 3+ columns nest (see below) |
| Sub-page | Sub-note + `Page` block | Keeps nesting and position |
| Synced block | Inlined | |
| Toggle | Text + children | Contents survive; the fold doesn't |
| Database | Folder of notes | Labels → tags; other properties → a table atop each note |
| Page icon / cover | Note emoji / cover | Covers with `--covers` |
| Bookmark, embed, video, file | Text with a link | Never silently dropped |

Bold, italic, underline, strikethrough, inline code, links, text colour and background
colour carry on every one of these.

### Mnemo → Notion

| Mnemo | Notion | Notes |
| --- | --- | --- |
| Text, headings, lists, quote, divider | The same | `Heading4` becomes `heading_3` (Notion has three) |
| Checklist | To-do | State kept |
| Code | Code | Language mapped back; caption kept |
| Equation / equation span | Equation block / inline equation | Notion caps expressions at 1000 chars; longer ones become code with a warning |
| Image | Image | **Uploaded** via Notion's File Upload API, caption kept |
| Callout | Callout | Emoji and tone-derived colour |
| Table | Table | First-row/first-column headers (all Notion supports) |
| TwoColumn | Column list | Split ratio preserved via `width_ratio` |
| Sub-note (`Page` block) | Real child page, at its position | |
| Folder | A page holding its notes | Notion has no folders |
| Sketch | Code block of its source | Notion has no drawing surface |

### Colours

Mnemo stores colours as theme tokens; Notion has a fixed nine-colour palette. The mapping
(both directions) matches on hue and is chosen so a round trip **converges** — a note that
goes Notion → Mnemo → Notion keeps its colour instead of drifting. Three Notion pairs
collapse on the way in because Mnemo's palette has no brown (details in
[`notion2mnemo/colors.py`](notion2mnemo/colors.py)); the CLI accepts `--color-map FILE` to
override any of it. One Notion rule applies on the way out: a run holds *either* a text
colour or a background, so a span carrying both keeps the background.

### Known losses

- **Toggle collapse state** (contents survive as nested blocks).
- **Notion-hosted file attachments** (PDF, video, uploaded files) — their URLs expire within
  the hour, so they become links plus a warning naming each one. Images are fine: they are
  downloaded during conversion.
- **Database views, filters, formulas-as-formulas, relations** — rows and computed values
  survive; the machinery doesn't.
- Buttons, AI blocks, and anything Notion's API itself reports as unsupported.

Nothing is dropped silently: every skipped item is counted in the summary or named in the
warnings, which the app shows and the CLI writes next to the output file.

## Re-running

Ids are derived deterministically from Notion ids, so converting twice produces the same
note ids — re-import into Mnemo with the **Overwrite** conflict policy to update in place
instead of duplicating. Unchanged content produces a byte-identical package.

## Building from source

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -t .     # 132 tests, no network, no token
python -m notion2mnemo gui                    # run the app from source
pyinstaller notion2mnemo.spec                 # build the Windows executable
```

The test suite pins the emitted JSON against what Mnemo's `BlockJsonConverter` actually
reads, so a format change in either app fails a test here rather than silently corrupting
an import.

### Project layout

| Module | Responsibility |
| --- | --- |
| `notion2mnemo/mnemo.py` | Mnemo's block/span/note model and exact JSON encoding |
| `notion2mnemo/colors.py` | Notion ↔ Mnemo colour mapping |
| `notion2mnemo/richtext.py` | Notion rich text → Mnemo spans |
| `notion2mnemo/convert.py` | Notion blocks → Mnemo blocks |
| `notion2mnemo/walker.py` | Workspace discovery, folders, sub-pages, databases |
| `notion2mnemo/reverse.py` | Mnemo blocks → Notion blocks |
| `notion2mnemo/push.py` | The Notion writer: batching, nesting, uploads |
| `notion2mnemo/assets.py` | Image handling and Mnemo asset ids |
| `notion2mnemo/package.py` | `.mnemo` package reader/writer |
| `notion2mnemo/notion.py` | API client: throttling, retries, cache, uploads |
| `notion2mnemo/gui/` | The desktop app (pywebview over the same engine) |
| `notion2mnemo/cli.py` | The command line |

## Troubleshooting

**"The integration can't see any pages."** In Notion: open the page →
⋯ → Connections → add your integration. This is per top-level page.

**Import fails in Mnemo with older versions.** Table support in packages needs a
Mnemo build from mid-2026 or later; update Mnemo.

**A database is missing (CLI).** If it has multiple data sources, pass
`--notion-version 2025-09-03`.

**Mnemo → Notion says 404.** The parent page isn't connected to the integration —
Connections again, on that page.

## License

[Apache-2.0](LICENSE), the same as Mnemo.
