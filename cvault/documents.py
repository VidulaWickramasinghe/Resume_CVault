"""Conservative DOCX edits: replace text-node payloads, never reconstruct XML."""
import io
import re
import zipfile
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
NS = {'w': W}
P = re.compile(rb'<w:p(?:\s[^>]*)?>.*?</w:p>', re.S)
T = re.compile(rb'(<w:t(?:\s[^>]*)?>)(.*?)(</w:t>)', re.S)
HEADINGS = {'professional summary', 'summary', 'profile', 'key skills', 'skills', 'work experience', 'employment history', 'experience', 'education', 'qualifications', 'certifications', 'achievements', 'projects', 'references', 'personal details'}
MAX_SIZE = 20 * 1024 * 1024

class DocumentError(ValueError):
    pass

def unpack(data):
    if len(data) > MAX_SIZE:
        raise DocumentError('The DOCX exceeds the 20 MB limit.')
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        infos = archive.infolist()
        if len(infos) > 2000 or sum(i.file_size for i in infos) > 60 * 1024 * 1024:
            raise DocumentError('The document package is too large.')
        if len({i.filename for i in infos}) != len(infos):
            raise DocumentError('Duplicate package entries are not supported.')
        parts = {i.filename: archive.read(i) for i in infos}
        xml = parts['word/document.xml']
        if b'<!DOCTYPE' in xml or b'<!ENTITY' in xml:
            raise DocumentError('XML entities are not supported.')
        root = ET.fromstring(xml)
        if root.tag != '{%s}document' % W or b'xmlns:w="' + W.encode() + b'"' not in xml:
            raise DocumentError('This Word XML namespace is not supported. Save a standard DOCX in Word first.')
        if any('vbaProject' in n for n in parts):
            raise DocumentError('Macro-enabled documents are not supported.')
        return parts, xml, root
    except (zipfile.BadZipFile, KeyError, ET.ParseError, RuntimeError) as exc:
        raise DocumentError('This is not a readable DOCX document.') from exc

def inspect(data, mapping=None):
    parts, xml, root = unpack(data)
    mapping = mapping or {}
    paragraphs = []
    section = 'Personal details'
    unsupported = bool(root.findall('.//w:txbxContent', NS) or root.findall('.//w:ins', NS) or root.findall('.//w:del', NS) or root.findall('.//w:altChunk', NS))
    warnings = []
    if unsupported:
        warnings.append('Text boxes, tracked changes or embedded content detected. This document is read-only; simplify it in Word before editing.')
    if root.findall('.//w:tbl', NS):
        warnings.append('Tables detected. Check PDF reading order and layout before submitting.')
    if any(int(c.get('{%s}num' % W, '1')) > 1 for c in root.findall('.//w:cols', NS)):
        warnings.append('Multiple columns detected. Check extracted reading order.')
    for index, match in enumerate(P.finditer(xml)):
        raw = match.group()
        # Parse in the original namespace context through the complete document.
        texts = list(T.finditer(raw))
        values = []
        for t in texts:
            try:
                values.append(ET.fromstring(b'<x>' + t.group(2) + b'</x>').text or '')
            except ET.ParseError as exc:
                raise DocumentError('Unsupported text markup.') from exc
        content = ''.join(values)
        if not content.strip():
            continue
        heading = content.strip().rstrip(':').lower() in HEADINGS or bool(re.search(rb'<w:pStyle[^>]+w:val="Heading[1-6]"', raw, re.I))
        if heading:
            section = content.strip()
        assigned = mapping.get(str(index), section)
        complex_run = bool(re.search(rb'<w:(?:fldChar|instrText|drawing|object|br|tab|sym|hyperlink)\b', raw))
        editable = not unsupported and not heading and not complex_run
        paragraphs.append({'id': str(index), 'section': assigned, 'heading': heading, 'text': content,
                           'runs': [{'id': str(i), 'text': v} for i, v in enumerate(values)],
                           'editable': editable, 'bullet': b'<w:numPr' in raw,
                           'reason': '' if editable else ('Heading styling is protected.' if heading else 'Complex Word content is protected.')})
    if not paragraphs:
        raise DocumentError('No supported Word text paragraphs were found.')
    all_text = '\n'.join(p['text'] for p in paragraphs)
    checks = [{'label': 'Readable document text', 'ok': True},
              {'label': 'Email address present', 'ok': bool(re.search(r'[^\s@]+@[^\s@]+\.[^\s@]+', all_text))},
              {'label': 'Recognisable section headings', 'ok': any(p['heading'] for p in paragraphs)}]
    return {'paragraphs': paragraphs, 'sections': list(dict.fromkeys(p['section'] for p in paragraphs)), 'warnings': warnings,
            'checks': checks, 'characters': len(all_text), 'read_only': unsupported}

def edit(data, changes, unlocked, mapping=None):
    model = inspect(data, mapping)
    by_id = {p['id']: p for p in model['paragraphs']}
    if not isinstance(changes, dict):
        raise DocumentError('Changes must be a paragraph map.')
    replacements = {}
    for pid, runs in changes.items():
        p = by_id.get(pid)
        if not p or not p['editable'] or p['section'] not in unlocked:
            raise DocumentError('An edit targeted protected content.')
        if not isinstance(runs, dict):
            raise DocumentError('Changes must identify text runs.')
        for rid, value in runs.items():
            if rid not in {r['id'] for r in p['runs']} or not isinstance(value, str) or len(value) > 20000:
                raise DocumentError('Invalid text edit.')
            if any(ord(c) < 32 and c not in '\n\t' for c in value) or '\n' in value or '\t' in value:
                raise DocumentError('Use one paragraph per field; line breaks and tabs are protected.')
        replacements[pid] = runs
    parts, xml, _ = unpack(data)
    counter = [-1]
    def paragraph(match):
        counter[0] += 1
        runs = replacements.get(str(counter[0]))
        if runs is None:
            return match.group()
        run_index = [-1]
        def text_node(t):
            run_index[0] += 1
            key = str(run_index[0])
            if key not in runs:
                return t.group()
            opening = t.group(1)
            if b'xml:space=' not in opening:
                opening = opening[:-1] + b' xml:space="preserve">'
            return opening + escape(runs[key]).encode('utf-8') + t.group(3)
        return T.sub(text_node, match.group())
    changed = P.sub(paragraph, xml)
    ET.fromstring(changed)
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, changed if name == 'word/document.xml' else content)
    result = out.getvalue()
    inspect(result, mapping)
    return result

def sample(kind='resume'):
    if kind == 'cover':
        lines = [('ALEX MORGAN', False), ('alex@example.com · Melbourne, VIC', False), ('Cover letter', True),
                 ('Dear Hiring Manager,', False), ('I am applying for the role at your organisation.', False),
                 ('My experience and skills align with the requirements of this position.', False),
                 ('Thank you for considering my application.', False), ('Kind regards, Alex Morgan', False)]
    else:
        lines = [('ALEX MORGAN', False), ('alex@example.com · Melbourne, VIC · 0400 000 000', False),
                 ('Professional Summary', True), ('Reliable professional with experience in customer service, administration and team collaboration.', False),
                 ('Key Skills', True), ('Customer service • Organisation • Communication • Problem solving', False),
                 ('Employment History', True), ('Customer Service Assistant | Example Company | 2023–Present', False),
                 ('Supported customers, maintained accurate records and collaborated with the team.', False),
                 ('Education', True), ('Certificate in Business | Example Institute | 2022', False)]
    paragraphs = ''.join('<w:p><w:pPr>%s</w:pPr><w:r><w:rPr>%s</w:rPr><w:t xml:space="preserve">%s</w:t></w:r></w:p>' %
                         ('<w:pStyle w:val="Heading1"/>' if h else '<w:spacing w:after="180"/>', '<w:b/>' if h else '', escape(t)) for t, h in lines)
    parts = {'[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>',
             '_rels/.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
             'word/_rels/document.xml.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
             'word/styles.xml': '<w:styles xmlns:w="%s"><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/><w:sz w:val="22"/></w:rPr></w:rPrDefault></w:docDefaults><w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:pPr><w:spacing w:before="260" w:after="120"/></w:pPr><w:rPr><w:b/><w:color w:val="245C4F"/><w:sz w:val="25"/></w:rPr></w:style></w:styles>' % W,
             'word/document.xml': '<w:document xmlns:w="%s"><w:body>%s<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:bottom="1134" w:left="1134" w:right="1134"/></w:sectPr></w:body></w:document>' % (W, paragraphs)}
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for n, c in parts.items():
            z.writestr(n, c)
    return out.getvalue()
