"""Markdown parser — AST-based extraction using mistune v3 with frontmatter."""

import logging

import frontmatter
import mistune

from parsing.base import BaseParser, ParseResult

logger = logging.getLogger(__name__)


class MarkdownParser(BaseParser):

    def __init__(self) -> None:
        self._md = mistune.create_markdown(renderer="ast", plugins=["table"])

    @property
    def supported_extensions(self) -> list[str]:
        return [".md", ".markdown"]

    def parse(self, file_bytes: bytes) -> ParseResult:
        logger.info(f"[MarkdownParser] Parsing Markdown ({len(file_bytes)} bytes)")
        raw_text = file_bytes.decode("utf-8", errors="replace")

        post = frontmatter.loads(raw_text)
        front_meta = dict(post.metadata) if post.metadata else {}
        content = post.content

        tokens = self._md(content)
        headers, tables, sections = _walk_ast(tokens)

        logger.info(
            f"[MarkdownParser] Extracted {len(content)} chars, "
            f"{len(headers)} headers, {len(tables)} tables, {len(sections)} sections"
        )

        return ParseResult(
            full_text=content,
            pages=[],
            page_count=1,
            metadata={
                "format": "markdown",
                "frontmatter": front_meta,
                "headers": headers,
                "header_count": len(headers),
                "tables": tables,
                "table_count": len(tables),
                "sections": sections,
            },
        )


def _extract_text(children: list[dict]) -> str:
    parts = []
    for child in children:
        t = child.get("type", "")
        if t in ("softbreak", "linebreak"):
            parts.append("\n")
        elif child.get("raw"):
            parts.append(child["raw"])
        elif child.get("children"):
            parts.append(_extract_text(child["children"]))
        elif child.get("text"):
            parts.append(child["text"])
    return "".join(parts)


def _extract_table(token: dict) -> dict:
    table_headers = []
    rows = []
    for child in token.get("children", []):
        if child["type"] == "table_head":
            for cell in child.get("children", []):
                table_headers.append(_extract_text(cell.get("children", [])))
        elif child["type"] == "table_body":
            for row in child.get("children", []):
                row_data = []
                for cell in row.get("children", []):
                    row_data.append(_extract_text(cell.get("children", [])))
                rows.append(row_data)
    return {"headers": table_headers, "rows": rows}


def _extract_list_text(token: dict) -> str:
    lines = []
    for item in token.get("children", []):
        parts = []
        for child in item.get("children", []):
            if child.get("type") == "list":
                nested = _extract_list_text(child)
                parts.append("\n".join("  " + line for line in nested.splitlines()))
            elif child.get("type") in ("block_text", "paragraph"):
                parts.append(_extract_text(child.get("children", [])))
            elif child.get("children"):
                parts.append(_extract_text(child["children"]))
            elif child.get("raw"):
                parts.append(child["raw"])
        lines.append("- " + " ".join(parts))
    return "\n".join(lines)


def _walk_ast(tokens: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    headers = []
    tables = []
    sections = []

    hierarchy = {}
    current_section_body: list[str] = []
    current_prefix = ""

    def _flush_section():
        body = "\n\n".join(current_section_body).strip()
        if body:
            sections.append(f"{current_prefix}{body}" if current_prefix else body)

    for token in tokens:
        token_type = token.get("type", "")

        if token_type == "heading":
            _flush_section()
            current_section_body.clear()

            level = token.get("attrs", {}).get("level", 1)
            text = _extract_text(token.get("children", []))

            hierarchy[level] = text
            for old_level in [k for k in hierarchy if k > level]:
                del hierarchy[old_level]
            context_path = " > ".join(hierarchy[lvl] for lvl in sorted(hierarchy))

            headers.append({"level": level, "text": text, "context_path": context_path})
            current_prefix = f"Section: {context_path}\n\n"

        elif token_type == "table":
            tables.append(_extract_table(token))
            tbl = tables[-1]
            table_text = " | ".join(tbl["headers"])
            for row in tbl["rows"]:
                table_text += "\n" + " | ".join(row)
            current_section_body.append(table_text)

        elif token_type == "paragraph":
            current_section_body.append(_extract_text(token.get("children", [])))

        elif token_type == "list":
            current_section_body.append(_extract_list_text(token))

        elif token_type == "block_code":
            current_section_body.append(token.get("raw", ""))

        elif token_type in ("blank_line", "thematic_break"):
            pass

        elif token_type == "block_quote":
            inner = (
                _extract_text(token.get("children", []))
                if token.get("children")
                else ""
            )
            if inner.strip():
                current_section_body.append(inner)

        else:
            text = (
                _extract_text(token.get("children", []))
                if token.get("children")
                else token.get("raw", "")
            )
            if text.strip():
                current_section_body.append(text)

    _flush_section()

    return headers, tables, sections
