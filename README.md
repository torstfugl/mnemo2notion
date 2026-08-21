# Notion ↔ Mnemo Converter

Moves notes between [Notion](https://www.notion.com) and [Mnemo](https://github.com/onemnemo/mnemo)
in both directions, keeping formatting: equations, text and background colours, headings,
callouts, tables, columns, images, sub-pages, tags and page icons.

Notion → Mnemo writes a `.mnemo` package that you import from Mnemo's Notes → Import panel.
Mnemo → Notion goes the other way, creating real pages through Notion's API and uploading
the images as it goes.

Everything runs locally. The only traffic is between your machine and Notion's API, and
there is no account or telemetry.

## The app

```
pip install -r requirements-gui.txt
python -m notion2mnemo gui
```

The app asks which direction you want, takes your integration key, and lists the pages it
can see so you can pick from them. There are prebuilt Windows executables on the releases
page if you would rather not install Python.

Setup takes about two minutes and only has to be done once:

1. Create an integration at [notion.so/my-integrations](https://www.notion.so/my-integrations)
   and copy its secret.
2. In Notion, open each top-level page you want to move and choose ⋯ → Connections → your
   integration. Sub-pages are included automatically.
3. Paste the secret into the app.

Going Mnemo → Notion also needs the integration to have insert content capability, which is
on by default for new integrations, and it must be connected to whichever page you want the
imported pages created under.

## The CLI

The CLI does everything the app does. `python -m notion2mnemo --help` lists the flags.

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

The pull direction caches API responses in `.notion-cache/`, so re-running after changing an
option costs no API calls. Ctrl-C is safe to resume from.

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
| Equation block / inline equation | `Equation` / `EquationSpan` | LaTeX verbatim; both apps render KaTeX |
| Image | `Image` + bundled asset | Downloaded into the package; caption kept |
| Table | `Table` tree | Header row and header column |
| Columns | `TwoColumn` | Widths kept; 3+ columns nest (see below) |
| Sub-page | Sub-note + `Page` block | Keeps nesting and position |
| Synced block | Inlined | |
| Toggle | Text + children | Contents survive, the fold does not |
| Database | Folder of notes | Labels → tags; other properties → a table atop each note |
| Page icon / cover | Note emoji / cover | Covers with `--covers` |
| Bookmark, embed, video, file | Text with a link | Never silently dropped |

Bold, italic, underline, strikethrough, inline code, links, text colour and background colour
carry on all of these.

### Mnemo → Notion

| Mnemo | Notion | Notes |
| --- | --- | --- |
| Text, headings, lists, quote, divider | The same | `Heading4` becomes `heading_3`, since Notion has three |
| Checklist | To-do | State kept |
| Code | Code | Language mapped back; caption kept |
| Equation / equation span | Equation block / inline equation | Notion caps expressions at 1000 chars; longer ones become code with a warning |
| Image | Image | Uploaded via Notion's File Upload API, caption kept |
| Callout | Callout | Emoji and tone-derived colour |
| Table | Table | First-row/first-column headers, which is all Notion supports |
| TwoColumn | Column list | Split ratio preserved via `width_ratio` |
| Sub-note (`Page` block) | Real child page, at its position | |
| Folder | A page holding its notes | Notion has no folders |
| Sketch | Code block of its source | Notion has no drawing surface |

### Colours

Mnemo stores colours as theme tokens and Notion has a fixed nine-colour palette. The mapping
runs both ways, matches on hue, and is chosen so that a round trip converges: a note that goes
Notion → Mnemo → Notion keeps its colour instead of drifting. Three Notion pairs collapse on
the way in because Mnemo's palette has no brown, which
[`notion2mnemo/colors.py`](notion2mnemo/colors.py) explains in more detail. `--color-map FILE`
overrides any of it.

One Notion rule applies on the way out. A run holds either a text colour or a background, not
both, so a span carrying both keeps the background.

### Known losses

Toggle collapse state, though the contents survive as nested blocks.

Notion-hosted file attachments (PDF, video, uploaded files). Their URLs expire within the hour,
so they become links, and each one is named in the warnings. Images are fine because they get
downloaded during conversion.

Database views, filters, formulas-as-formulas and relations. Rows and computed values survive
but the machinery does not.

Buttons, AI blocks, and anything Notion's API itself reports as unsupported.

Skipped items are always accounted for, either counted in the summary or named in the warnings.
The app shows these and the CLI writes them next to the output file.

## Re-running

Ids are derived from Notion ids, so converting twice produces the same note ids. Re-import into
Mnemo with the Overwrite conflict policy to update in place rather than duplicating. Unchanged
content produces a byte-identical package.

## Building from source

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -t .     # 132 tests, no network or token needed
python -m notion2mnemo gui                    # run the app from source
pyinstaller notion2mnemo.spec                 # build the Windows executable
```

The tests pin the emitted JSON against what Mnemo's `BlockJsonConverter` actually reads, so a
format change on either side breaks a test here instead of quietly corrupting an import.

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

**The integration can't see any pages.** In Notion, open the page and use ⋯ → Connections to
add your integration. This is per top-level page.

**Import fails in Mnemo with older versions.** Table support in packages needs a Mnemo build
from mid-2026 or later, so update Mnemo.

**A database is missing (CLI).** If it has multiple data sources, pass
`--notion-version 2025-09-03`.

**Mnemo → Notion says 404.** The parent page isn't connected to the integration. Connections
again, on that page.

## License

[Apache-2.0](LICENSE), the same as Mnemo.
