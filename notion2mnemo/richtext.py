"""
Notion ``rich_text`` -> Mnemo ``InlineSpan[]``.

The two models line up unusually well, which is why the formatting survives: a
Notion annotation set is flags plus one colour, and a Mnemo ``TextStyle`` is
flags plus two colour tokens plus a link. The only real work is deciding which
of Mnemo's two colour fields a Notion colour belongs in (``colors.py``) and
turning an inline equation into an atom rather than a string.

Inline equations are the case worth naming. Notion stores one as a rich-text
element of type ``equation`` carrying a LaTeX expression; Mnemo stores it as an
``EquationSpan``, an *atomic* inline node that occupies exactly one caret
position no matter how long the LaTeX is. Flattening it to text would render the
raw source in the middle of a sentence, so it is mapped to the atom - and,
because Mnemo lets an atom carry its own marks, a bolded equation stays bold.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .colors import ColorMap
from .mnemo import EquationSpan, InlineSpan, TextSpan, TextStyle, normalize_spans, plain


def _style(annotations: dict[str, Any], colors: ColorMap, link: str | None) -> TextStyle:
    foreground, background = colors.resolve(annotations.get("color"))
    return TextStyle(
        bold=bool(annotations.get("bold")),
        italic=bool(annotations.get("italic")),
        underline=bool(annotations.get("underline")),
        strikethrough=bool(annotations.get("strikethrough")),
        code=bool(annotations.get("code")),
        # Notion has no separate "highlight" flag - a highlight *is* a background
        # colour there. Mapping it to Mnemo's background token rather than its
        # `highlight` boolean keeps the specific hue instead of collapsing nine
        # colours onto one highlighter.
        highlight=False,
        background_color=background,
        foreground_color=foreground,
        link_url=link or None,
        suppress_auto_link=False,
        subscript=False,
        superscript=False,
    )


def _mention_text(item: dict[str, Any]) -> str:
    """
    A readable label for a mention.

    ``plain_text`` is almost always right and is what Notion itself renders, so
    it is preferred; the fallbacks only matter for mention kinds where Notion
    sends an empty string (some link_mention payloads do).
    """
    text = item.get("plain_text") or ""
    if text:
        return text
    mention = item.get("mention") or {}
    kind = mention.get("type")
    if kind == "date":
        date = mention.get("date") or {}
        start, end = date.get("start"), date.get("end")
        return f"{start} - {end}" if end else str(start or "")
    if kind == "user":
        return (mention.get("user") or {}).get("name") or "someone"
    if kind in {"page", "database"}:
        return (mention.get(kind) or {}).get("id") or ""
    if kind == "link_preview":
        return (mention.get("link_preview") or {}).get("url") or ""
    return ""


def convert_rich_text(
    rich_text: Sequence[dict[str, Any]] | None,
    colors: ColorMap,
    *,
    force_style: TextStyle | None = None,
) -> list[InlineSpan]:
    """
    Converts one Notion rich-text array into Mnemo spans.

    ``force_style`` layers a block-level colour underneath the per-run
    annotations: Notion puts a colour on the *block* for "this whole paragraph is
    red", and on the *run* for "these three words are red". Mnemo has only the
    per-run form, so a block colour is pushed down into every run that does not
    already carry one of its own.
    """
    spans: list[InlineSpan] = []
    for item in rich_text or ():
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        annotations = item.get("annotations") or {}
        link = item.get("href")
        style = _style(annotations, colors, link)
        if force_style is not None:
            style = _merge(force_style, style)

        if kind == "equation":
            latex = (item.get("equation") or {}).get("expression") or ""
            if latex:
                spans.append(EquationSpan(latex=latex, style=style))
            continue

        if kind == "mention":
            text = _mention_text(item)
            if text:
                spans.append(TextSpan(text=text, style=style))
            continue

        # `text`, and anything a future API version adds: `plain_text` is
        # required on every rich-text object, so an unknown type degrades to its
        # text rather than vanishing.
        text = (item.get("text") or {}).get("content")
        if text is None:
            text = item.get("plain_text") or ""
        if text:
            spans.append(TextSpan(text=text, style=style))

    return normalize_spans(spans)


def _merge(block_style: TextStyle, run_style: TextStyle) -> TextStyle:
    """
    A run's own colour wins; the block's colour fills in where the run has none.

    Flags are never taken from the block - Notion has no block-level bold, so a
    ``force_style`` only ever carries colour.
    """
    from dataclasses import replace

    return replace(
        run_style,
        foreground_color=run_style.foreground_color or block_style.foreground_color,
        background_color=run_style.background_color or block_style.background_color,
    )


def block_color_style(color: str | None, colors: ColorMap) -> TextStyle | None:
    """The ``force_style`` for a Notion block colour, or None when it is default."""
    foreground, background = colors.resolve(color)
    if foreground is None and background is None:
        return None
    return TextStyle(foreground_color=foreground, background_color=background)


def rich_text_to_plain(rich_text: Iterable[dict[str, Any]] | None) -> str:
    """
    The bare text of a rich-text array, for titles, captions and alt text.

    Non-object entries are skipped rather than raising. This runs against every
    title and caption in a workspace, and one malformed value from a future API
    shape should cost that caption, not the whole export.
    """
    if isinstance(rich_text, dict):
        rich_text = [rich_text]
    return "".join(
        item.get("plain_text") or ""
        for item in (rich_text or ())
        if isinstance(item, dict)
    )


def linked(text: str, url: str, colors: ColorMap) -> list[InlineSpan]:
    """One span of link text - the fallback shape for blocks Mnemo cannot model."""
    return [TextSpan(text=text or url, style=TextStyle(link_url=url))] if url else [plain(text)]
