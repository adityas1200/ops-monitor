"""Convert BRD markdown to a formatted Word document."""
import re
from docx import Document
from docx.shared import Inches, Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml.ns import qn, nsdecls
from docx.oxml import parse_xml

def set_cell_shading(cell, color):
    shading_elm = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{color}"/>')
    cell._tc.get_or_add_tcPr().append(shading_elm)

def add_formatted_text(paragraph, text):
    """Parse markdown bold markers and add runs with formatting."""
    parts = re.split(r'\*\*(.+?)\*\*', text)
    for i, part in enumerate(parts):
        if not part:
            continue
        run = paragraph.add_run(part)
        if i % 2 == 1:
            run.bold = True

def create_table_from_rows(doc, headers, rows):
    """Create a formatted table in the document."""
    table = doc.add_table(rows=1 + len(rows), cols=len(headers))
    table.style = 'Table Grid'
    table.alignment = WD_TABLE_ALIGNMENT.CENTER

    # Header row
    for i, header in enumerate(headers):
        cell = table.rows[0].cells[i]
        cell.text = ''
        p = cell.paragraphs[0]
        run = p.add_run(header)
        run.bold = True
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(255, 255, 255)
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        set_cell_shading(cell, "2E4057")

    # Data rows
    for r_idx, row in enumerate(rows):
        for c_idx, cell_text in enumerate(row):
            cell = table.rows[r_idx + 1].cells[c_idx]
            cell.text = ''
            p = cell.paragraphs[0]
            add_formatted_text(p, cell_text)
            for run in p.runs:
                run.font.size = Pt(9)
            if r_idx % 2 == 0:
                set_cell_shading(cell, "F2F6FA")

    return table

def parse_markdown_table(lines, start_idx):
    """Parse a markdown table starting at start_idx. Returns (headers, rows, end_idx)."""
    headers = [h.strip() for h in lines[start_idx].strip('|').split('|')]
    # Skip separator line
    rows = []
    i = start_idx + 2
    while i < len(lines) and '|' in lines[i] and lines[i].strip().startswith('|'):
        row = [c.strip() for c in lines[i].strip('|').split('|')]
        rows.append(row)
        i += 1
    return headers, rows, i

def main():
    md_path = r"c:\Users\EKGAH\Documents\project\ops-monitor\docs\Business_Requirements_Document.md"
    docx_path = r"c:\Users\EKGAH\Documents\project\ops-monitor\docs\Business_Requirements_Document.docx"

    with open(md_path, 'r', encoding='utf-8') as f:
        content = f.read()

    lines = content.split('\n')

    doc = Document()

    # Set default font
    style = doc.styles['Normal']
    font = style.font
    font.name = 'Calibri'
    font.size = Pt(11)

    # Set narrow margins
    for section in doc.sections:
        section.top_margin = Cm(2.0)
        section.bottom_margin = Cm(2.0)
        section.left_margin = Cm(2.5)
        section.right_margin = Cm(2.5)

    # Title
    title = doc.add_heading('Business Requirements Document (BRD)', level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # Subtitle
    subtitle = doc.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = subtitle.add_run('Agentic Data-Ops Monitoring System (Ops Monitor)')
    run.font.size = Pt(14)
    run.font.color.rgb = RGBColor(46, 64, 87)

    doc.add_paragraph()

    i = 0
    # Skip the first two heading lines and the first metadata table
    # Process the document metadata table first
    while i < len(lines):
        line = lines[i]
        if line.startswith('| Field'):
            headers, rows, i = parse_markdown_table(lines, i)
            create_table_from_rows(doc, headers, rows)
            doc.add_paragraph()
            break
        i += 1

    # Now process the rest
    while i < len(lines):
        line = lines[i]

        # Skip horizontal rules
        if line.strip() == '---':
            i += 1
            continue

        # Skip the main title and subtitle (already added)
        if line.startswith('# ') and 'Business Requirements' in line:
            i += 1
            continue
        if line.startswith('## ') and 'Agentic Data-Ops' in line:
            i += 1
            continue

        # Heading 2 (##)
        if line.startswith('## '):
            heading_text = line.lstrip('# ').strip()
            doc.add_heading(heading_text, level=1)
            i += 1
            continue

        # Heading 3 (###)
        if line.startswith('### '):
            heading_text = line.lstrip('# ').strip()
            doc.add_heading(heading_text, level=2)
            i += 1
            continue

        # Table
        if '|' in line and line.strip().startswith('|') and i + 1 < len(lines) and '---' in lines[i + 1]:
            headers, rows, i = parse_markdown_table(lines, i)
            create_table_from_rows(doc, headers, rows)
            doc.add_paragraph()
            continue

        # Code block (architecture diagram or workflow)
        if line.strip().startswith('```'):
            i += 1
            code_lines = []
            while i < len(lines) and not lines[i].strip().startswith('```'):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing ```

            # Add as a formatted code block
            for code_line in code_lines:
                p = doc.add_paragraph()
                p.paragraph_format.space_before = Pt(0)
                p.paragraph_format.space_after = Pt(0)
                p.paragraph_format.left_indent = Cm(1.0)
                run = p.add_run(code_line if code_line else ' ')
                run.font.name = 'Consolas'
                run.font.size = Pt(8)
            doc.add_paragraph()
            continue

        # Numbered list items with bold
        if re.match(r'^\d+\.', line.strip()):
            p = doc.add_paragraph(style='List Number')
            text = re.sub(r'^\d+\.\s*', '', line.strip())
            add_formatted_text(p, text)
            i += 1
            continue

        # Bullet list
        if line.strip().startswith('- '):
            p = doc.add_paragraph(style='List Bullet')
            text = line.strip()[2:]
            add_formatted_text(p, text)
            i += 1
            continue

        # Regular paragraph
        if line.strip():
            p = doc.add_paragraph()
            add_formatted_text(p, line.strip())
            i += 1
            continue

        # Empty line
        i += 1

    # Footer
    doc.add_paragraph()
    footer_p = doc.add_paragraph()
    footer_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = footer_p.add_run('Document prepared based on codebase assessment of the ops-monitor repository (initial-run branch) as of 2026-07-06.')
    run.italic = True
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(128, 128, 128)

    doc.save(docx_path)
    print(f"Word document saved to: {docx_path}")

if __name__ == '__main__':
    main()
