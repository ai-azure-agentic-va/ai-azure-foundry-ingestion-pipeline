"""Markdown parser - AST-based extraction using mistune v3.

Replaces regex-based parsing with a proper AST walk. Extracts:
  - Hierarchical headers with context paths
  - Tables as structured data
  - YAML frontmatter (Wiki metadata)
  - Sections: header + body pairs ready for the chunker

The structured sections eliminate the need for MarkdownHeaderTextSplitter
in the chunker — parse once, chunk on pre-split sections.
"""

import logging
import mistune
import frontmatter
from .base import BaseParser, ParseResult

logger = logging.getLogger(__name__)


class MarkdownParser(BaseParser):

    def __init__(self):
        # Table plugin required — without it, tables are parsed as plain paragraphs
        self._md = mistune.create_markdown(renderer='ast', plugins=['table'])

    @property
    def supported_extensions(self) -> list[str]:
        return [".md", ".markdown"]

    def parse(self, file_bytes: bytes) -> ParseResult:
        logger.info(f"[MarkdownParser] Parsing Markdown ({len(file_bytes)} bytes)")
        raw_text = file_bytes.decode("utf-8", errors="replace")

        # Extract YAML frontmatter if present (common in Wiki .md files)
        post = frontmatter.loads(raw_text)
        front_meta = dict(post.metadata) if post.metadata else {}
        content = post.content

        # Parse to AST
        tokens = self._md(content)

        # Walk AST to extract structured data
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
    """Recursively extract plain text from AST children."""
    parts = []
    for child in children:
        t = child.get("type", "")
        if t == "softbreak":
            parts.append("\n")
        elif t == "linebreak":
            parts.append("\n")
        elif child.get("raw"):
            parts.append(child["raw"])
        elif child.get("children"):
            parts.append(_extract_text(child["children"]))
        elif child.get("text"):
            parts.append(child["text"])
    return "".join(parts)


def _extract_table(token: dict) -> dict:
    """Extract table as {headers: [...], rows: [[...], ...]}."""
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
    """Extract text from a list token, preserving bullet structure."""
    lines = []
    for item in token.get("children", []):
        parts = []
        for child in item.get("children", []):
            if child.get("type") == "list":
                # Nested list — recurse with indent
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
    """Walk AST to extract headers, tables, and header-prefixed sections.

    Returns:
        headers:  [{"level": int, "text": str, "context_path": str}, ...]
        tables:   [{"headers": [...], "rows": [[...], ...]}, ...]
        sections: ["Section: H1 > H2\\n\\nbody text", ...] — ready for the chunker
    """
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
            # Flush previous section
            _flush_section()
            current_section_body.clear()

            level = token.get("attrs", {}).get("level", 1)
            text = _extract_text(token.get("children", []))

            # Build hierarchy
            hierarchy[level] = text
            for l in [k for k in hierarchy if k > level]:
                del hierarchy[l]
            context_path = " > ".join(hierarchy[l] for l in sorted(hierarchy))

            headers.append({"level": level, "text": text, "context_path": context_path})
            current_prefix = f"Section: {context_path}\n\n"

        elif token_type == "table":
            tables.append(_extract_table(token))
            # Render table as text for the section body
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
            pass  # skip whitespace and horizontal rules

        elif token_type == "block_quote":
            inner = _extract_text(token.get("children", [])) if token.get("children") else ""
            if inner.strip():
                current_section_body.append(inner)

        else:
            # Catch-all for other block types
            text = _extract_text(token.get("children", [])) if token.get("children") else token.get("raw", "")
            if text.strip():
                current_section_body.append(text)

    # Flush last section
    _flush_section()

    return headers, tables, sections
