"""Word (.docx) document extractor used for RAG ingestion.

Supports local file paths and remote URLs downloaded through the unified remote-file fetcher.
"""

import logging
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from contextlib import closing
from typing import override
from urllib.parse import urlparse
from xml.etree.ElementTree import Element, ParseError

from defusedxml import ElementTree as DefusedElementTree
from docx import Document as DocxDocument
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from sqlalchemy.orm import Session

from configs import dify_config
from core.file import remote_fetcher
from core.rag.extractor.extractor_base import BaseExtractor
from core.rag.models.document import Document
from extensions.ext_database import db
from extensions.ext_storage import storage
from extensions.storage.storage_type import StorageType
from libs.datetime_utils import naive_utc_now
from libs.http_headers import content_type_media_type
from models.enums import CreatorUserRole
from models.model import UploadFile

logger = logging.getLogger(__name__)

_OLE2_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
_DOCX_DOCUMENT_PART = "word/document.xml"


def _local_name(tag: str) -> str:
    """Return the local part of a possibly namespaced XML tag.

    Precondition: tag is the tag of a parsed XML element.
    Postcondition: returns the part after the last "}" (or the tag unchanged when it
    carries no namespace), so Strict OOXML, Transitional OOXML and namespace-less
    tags compare equal.
    """
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _paragraph_text(paragraph: Element) -> str:
    """Concatenate the text of every descendant text run of a paragraph element.

    Precondition: paragraph is a <w:p>-like element (any namespace).
    Postcondition: returns the joined text of all descendant <w:t> elements;
    returns "" when the paragraph holds no text.
    """
    return "".join(node.text or "" for node in paragraph.iter() if _local_name(node.tag) == "t")


def _table_to_markdown_from_xml(table: Element) -> str:
    """Render a <w:tbl>-like element as a Markdown table.

    Precondition: table is a <w:tbl>-like element (any namespace).
    Postcondition: returns the same Markdown shape as WordExtractor._table_to_markdown
    (first row as header, "---" separator, one row per table row); returns ""
    when the table has no rows.
    """
    rows: list[list[str]] = []
    for row in table:
        if _local_name(row.tag) != "tr":
            continue
        cells: list[str] = []
        for cell in row:
            if _local_name(cell.tag) != "tc":
                continue
            paragraph_texts: list[str] = []
            for paragraph in cell:
                if _local_name(paragraph.tag) != "p":
                    continue
                text = _paragraph_text(paragraph)
                if text and text not in paragraph_texts:
                    paragraph_texts.append(text)
            cells.append(" ".join(paragraph_texts))
        rows.append(cells)
    if not rows:
        return ""
    total_cols = max(len(row) for row in rows)
    lines = [
        "| " + " | ".join(rows[0] + [""] * (total_cols - len(rows[0]))) + " |",
        "| " + " | ".join(["---"] * total_cols) + " |",
    ]
    for row in rows[1:]:
        lines.append("| " + " | ".join(row + [""] * (total_cols - len(row))) + " |")
    return "\n".join(lines)


def _parse_document_xml(xml_bytes: bytes) -> str:
    """Extract paragraphs and tables from a word/document.xml part by local name.

    Precondition: xml_bytes is the raw content of the word/document.xml part of a .docx package.
    Postcondition: returns paragraphs and Markdown tables in document order, joined with "\\n"
    (blank paragraphs become "\\n", matching WordExtractor.parse_docx); raises ValueError
    with a clear message when the XML itself cannot be parsed.
    """
    try:
        root = DefusedElementTree.fromstring(xml_bytes)
    except ParseError as exc:
        raise ValueError("File is not a valid .docx document: 'word/document.xml' could not be parsed as XML.") from exc
    body = (
        root
        if _local_name(root.tag) == "body"
        else next((child for child in root if _local_name(child.tag) == "body"), None)
    )
    if body is None:
        return ""
    parts: list[str] = []
    for child in body:
        name = _local_name(child.tag)
        if name == "p":
            text = _paragraph_text(child)
            parts.append(text if text.strip() else "\n")
        elif name == "tbl":
            table_markdown = _table_to_markdown_from_xml(child)
            if table_markdown:
                parts.append(table_markdown)
    return "\n".join(parts)


def _is_legacy_ole_doc(file_path: str) -> bool:
    """Detect a legacy OLE2 Compound File Binary (.doc) payload by its magic bytes.

    Precondition: file_path names a readable file.
    Postcondition: returns True only when the first eight bytes are the OLE2 magic
    D0 CF 11 E0 A1 B1 1A E1; returns False for unreadable or shorter files.
    """
    try:
        with open(file_path, "rb") as file:
            return file.read(8) == _OLE2_MAGIC
    except OSError:
        return False


def _convert_legacy_doc_to_docx(source_path: str, output_dir: str) -> str:
    """Convert a legacy OLE .doc file to .docx with LibreOffice.

    Precondition: source_path is a legacy .doc file; output_dir exists and is writable.
    Postcondition: returns the path of the converted .docx file in output_dir.
    Raises ValueError with a clear message when LibreOffice is missing, the
    conversion times out, or LibreOffice exits non-zero / produces no file.
    """
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice is None:
        raise ValueError(
            f"Failed to convert legacy .doc file '{source_path}': LibreOffice conversion was attempted "
            "but no LibreOffice executable ('soffice') was found on this server. "
            "Install LibreOffice or convert the file to .docx first."
        )
    profile_dir = os.path.join(output_dir, "soffice-profile")
    converted_path = os.path.join(output_dir, os.path.splitext(os.path.basename(source_path))[0] + ".docx")
    command = [
        soffice,
        "--headless",
        f"-env:UserInstallation=file://{profile_dir}",
        "--convert-to",
        "docx",
        "--outdir",
        output_dir,
        source_path,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            f"Failed to convert legacy .doc file '{source_path}': LibreOffice conversion timed out after 120 seconds."
        ) from exc
    if result.returncode != 0 or not os.path.isfile(converted_path):
        detail = (result.stderr or result.stdout or "").strip()[-500:]
        raise ValueError(
            f"Failed to convert legacy .doc file '{source_path}': LibreOffice conversion was attempted "
            f"but failed (exit code {result.returncode}). {detail}"
        )
    return converted_path


class WordExtractor(BaseExtractor):
    """Load docx files.

    Args:
        file_path: Path to the file to load.
        session: Session used to persist extracted images.
    """

    _closed: bool
    _session: Session | None

    def __init__(self, file_path: str, tenant_id: str, user_id: str, *, session: Session | None = None):
        """Initialize from a local file or a downloadable URL.

        Downloaded files are closed before extraction can reopen them. If setup
        fails, close the response and remove any owned temporary file before
        propagating the error.
        """
        self._closed = False
        self.file_path = file_path
        self.tenant_id = tenant_id
        self.user_id = user_id
        self._session = session

        if "~" in self.file_path:
            self.file_path = os.path.expanduser(self.file_path)

        # If the file is a web path, download it to a temporary file, and use that
        if not os.path.isfile(self.file_path) and self._is_valid_url(self.file_path):
            response = remote_fetcher.make_request("GET", self.file_path)

            if response.status_code != 200:
                response.close()
                raise ValueError(f"Check the url of your file; returned status code {response.status_code}")

            self.web_path = self.file_path
            try:
                with closing(response), tempfile.NamedTemporaryFile(delete=False) as temp_file:
                    self.temp_file_path = temp_file.name
                    temp_file.write(response.content)
            except Exception:
                self.close()
                raise
            self.file_path = self.temp_file_path
        elif not os.path.isfile(self.file_path):
            raise ValueError(f"File path {self.file_path} is not a valid file or url")

    def close(self) -> None:
        """Remove an owned temporary path once, tolerating missing files.

        Local input files are never removed. Other cleanup errors are logged
        and suppressed so cleanup preserves any original extraction failure.
        """
        if getattr(self, "_closed", False):
            return

        self._closed = True
        temp_file_path = getattr(self, "temp_file_path", None)
        if temp_file_path is None:
            return

        try:
            os.unlink(temp_file_path)
        except FileNotFoundError:
            pass
        except Exception:
            logger.debug("Failed to cleanup downloaded word temp file", exc_info=True)

    def __del__(self):
        self.close()

    @override
    def extract(self) -> list[Document]:
        """Load given path as single page."""
        content = self.parse_docx(self.file_path)
        return [
            Document(
                page_content=content,
                metadata={"source": self.file_path},
            )
        ]

    @staticmethod
    def _is_valid_url(url: str) -> bool:
        """Check if the url is valid."""
        parsed = urlparse(url)
        return bool(parsed.netloc) and bool(parsed.scheme)

    def _extract_images_from_docx(self, doc):
        session = self._session or db.session
        image_count = 0
        image_map = {}
        upload_files: list[UploadFile] = []
        base_url = dify_config.FILES_URL

        for r_id, rel in doc.part.rels.items():
            if rel.reltype == RT.IMAGE:
                image_count += 1
                if rel.is_external:
                    url = rel.target_ref
                    if not self._is_valid_url(url):
                        continue
                    try:
                        response = remote_fetcher.make_request("GET", url)
                    except Exception:
                        logger.warning("Failed to download image from URL: %s", url, exc_info=True)
                        continue
                    if response.status_code == 200:
                        image_ext = mimetypes.guess_extension(
                            content_type_media_type(response.headers.get("Content-Type", ""))
                        )
                        if image_ext is None:
                            continue
                        file_uuid = str(uuid.uuid4())
                        file_key = "image_files/" + self.tenant_id + "/" + file_uuid + image_ext
                        mime_type, _ = mimetypes.guess_type(file_key)
                        storage.save(file_key, response.content)
                        # save file to db
                        upload_file = UploadFile(
                            tenant_id=self.tenant_id,
                            storage_type=StorageType(dify_config.STORAGE_TYPE),
                            key=file_key,
                            name=file_key,
                            size=0,
                            extension=str(image_ext),
                            mime_type=mime_type or "",
                            created_by=self.user_id,
                            created_by_role=CreatorUserRole.ACCOUNT,
                            created_at=naive_utc_now(),
                            used=True,
                            used_by=self.user_id,
                            used_at=naive_utc_now(),
                        )
                        upload_files.append(upload_file)
                        image_map[r_id] = f"![image]({base_url}/files/{upload_file.id}/file-preview)"
                else:
                    image_ext = rel.target_ref.split(".")[-1]
                    if image_ext is None:
                        continue
                    # user uuid as file name
                    file_uuid = str(uuid.uuid4())
                    file_key = "image_files/" + self.tenant_id + "/" + file_uuid + "." + image_ext
                    mime_type, _ = mimetypes.guess_type(file_key)

                    storage.save(file_key, rel.target_part.blob)
                    # save file to db
                    upload_file = UploadFile(
                        tenant_id=self.tenant_id,
                        storage_type=StorageType(dify_config.STORAGE_TYPE),
                        key=file_key,
                        name=file_key,
                        size=0,
                        extension=str(image_ext),
                        mime_type=mime_type or "",
                        created_by=self.user_id,
                        created_by_role=CreatorUserRole.ACCOUNT,
                        created_at=naive_utc_now(),
                        used=True,
                        used_by=self.user_id,
                        used_at=naive_utc_now(),
                    )
                    upload_files.append(upload_file)
                    image_map[rel.target_part] = f"![image]({base_url}/files/{upload_file.id}/file-preview)"
        if upload_files:
            session.add_all(upload_files)
        if self._session is None:
            session.commit()
        return image_map

    def _table_to_markdown(self, table, image_map):
        markdown = []
        # calculate the total number of columns
        total_cols = max(len(row.cells) for row in table.rows)

        header_row = table.rows[0]
        headers = self._parse_row(header_row, image_map, total_cols)
        markdown.extend(("| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * total_cols) + " |"))

        for row in table.rows[1:]:
            row_cells = self._parse_row(row, image_map, total_cols)
            markdown.append("| " + " | ".join(row_cells) + " |")
        return "\n".join(markdown)

    def _parse_row(self, row, image_map, total_cols):
        # Initialize a row, all of which are empty by default
        row_cells = [""] * total_cols
        col_index = 0
        while col_index < len(row.cells):
            # make sure the col_index is not out of range
            while col_index < len(row.cells) and row_cells[col_index] != "":
                col_index += 1
            # if col_index is out of range the loop is jumped
            if col_index >= len(row.cells):
                break
            # get the correct cell
            cell = row.cells[col_index]
            cell_content = self._parse_cell(cell, image_map).strip()
            cell_colspan = cell.grid_span or 1
            for i in range(cell_colspan):
                if col_index + i < total_cols:
                    row_cells[col_index + i] = cell_content if i == 0 else ""
            col_index += cell_colspan
        return row_cells

    def _parse_cell(self, cell, image_map):
        cell_content = []
        for paragraph in cell.paragraphs:
            parsed_paragraph = self._parse_cell_paragraph(paragraph, image_map)
            if parsed_paragraph:
                cell_content.append(parsed_paragraph)
        unique_content = list(dict.fromkeys(cell_content))
        return " ".join(unique_content)

    def _parse_cell_paragraph(self, paragraph, image_map):
        paragraph_content: list[str] = []

        for child in paragraph._element:
            tag = child.tag
            if tag == qn("w:hyperlink"):
                # Note: w:hyperlink elements may also use w:anchor for internal bookmarks.
                # This extractor intentionally only converts external links (HTTP/mailto, etc.)
                # that are backed by a relationship id (r:id) with rel.is_external == True.
                # Hyperlinks without such an external rel (including anchor-only bookmarks)
                # are left as plain text link_text.
                r_id = child.get(qn("r:id"))
                link_text_parts: list[str] = []
                for run_elem in child.findall(qn("w:r")):
                    run = Run(run_elem, paragraph)
                    if run.text:
                        link_text_parts.append(run.text)
                link_text = "".join(link_text_parts).strip()
                if r_id:
                    try:
                        rel = paragraph.part.rels.get(r_id)
                        if rel:
                            target_ref = getattr(rel, "target_ref", None)
                            if target_ref:
                                parsed_target = urlparse(str(target_ref))
                                if rel.is_external or parsed_target.scheme in ("http", "https", "mailto"):
                                    display_text = link_text or str(target_ref)
                                    link_text = f"[{display_text}]({target_ref})"
                    except Exception:
                        logger.exception("Failed to resolve URL for hyperlink with r:id: %s", r_id)
                if link_text:
                    paragraph_content.append(link_text)

            elif tag == qn("w:r"):
                run = Run(child, paragraph)
                if run.element.xpath(".//a:blip"):
                    for blip in run.element.xpath(".//a:blip"):
                        image_id = blip.get(
                            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
                        )
                        if not image_id:
                            continue
                        rel = paragraph.part.rels.get(image_id)
                        if rel is None:
                            continue
                        if rel.is_external:
                            if image_id in image_map:
                                paragraph_content.append(image_map[image_id])
                        else:
                            image_part = rel.target_part
                            if image_part in image_map:
                                paragraph_content.append(image_map[image_part])
                else:
                    if run.text:
                        paragraph_content.append(run.text)

        return "".join(paragraph_content).strip()

    def parse_docx(self, docx_path: str) -> str:
        """Parse a .docx file into plain text with Markdown tables.

        Well-formed packages take the python-docx path unchanged. When python-docx
        cannot open the package (malformed packages such as Strict OOXML namespaces
        or a stripped root _rels/.rels), extraction falls back to reading
        word/document.xml directly; legacy OLE .doc files renamed to .docx are
        converted with LibreOffice first. Payloads that are neither raise a clear
        ValueError instead of the underlying python-docx error.
        """
        try:
            doc = DocxDocument(docx_path)
        except Exception as exc:
            logger.warning("python-docx failed to open %s (%s); trying fallback extraction", docx_path, exc)
            return self._parse_docx_with_fallback(docx_path, exc)
        return self._parse_opened_docx(doc)

    def _parse_docx_with_fallback(self, docx_path: str, original_error: Exception) -> str:
        """Extract text from a .docx package python-docx could not open.

        Precondition: python-docx raised original_error while opening docx_path.
        Postcondition: returns the document text for malformed-but-readable packages
        and for legacy OLE .doc files converted via LibreOffice; raises a clear
        ValueError when the payload is not a readable .docx package at all.
        """
        if not os.path.isfile(docx_path):
            raise original_error
        if _is_legacy_ole_doc(docx_path):
            with tempfile.TemporaryDirectory(prefix="dify-doc-convert-") as tmpdir:
                converted_path = _convert_legacy_doc_to_docx(docx_path, tmpdir)
                return self.parse_docx(converted_path)
        try:
            with zipfile.ZipFile(docx_path) as docx_zip:
                try:
                    document_xml = docx_zip.read(_DOCX_DOCUMENT_PART)
                except KeyError:
                    raise ValueError(
                        f"File '{docx_path}' is not a valid .docx document: "
                        f"it contains no '{_DOCX_DOCUMENT_PART}' part."
                    ) from original_error
        except zipfile.BadZipFile:
            raise ValueError(
                f"File '{docx_path}' is not a valid .docx document: "
                "it is not a ZIP archive (it may be a legacy .doc file or another format)."
            ) from original_error
        return _parse_document_xml(document_xml)

    def _parse_opened_docx(self, doc) -> str:
        """Parse an already-opened python-docx document into text with Markdown tables.

        Precondition: doc is a python-docx Document opened from a well-formed package.
        Postcondition: returns paragraphs and tables in document order, joined with "\\n".
        """

        content: list[str] = []

        image_map = self._extract_images_from_docx(doc)

        def parse_paragraph(paragraph):
            def append_image_link(image_id, has_drawing, target_buffer):
                """Helper to append image link from image_map based on relationship type."""
                rel = doc.part.rels[image_id]
                if rel.is_external:
                    if image_id in image_map and not has_drawing:
                        target_buffer.append(image_map[image_id])
                else:
                    image_part = rel.target_part
                    if image_part in image_map and not has_drawing:
                        target_buffer.append(image_map[image_part])

            def process_run(run, target_buffer):
                # Helper to extract text and embedded images from a run element and append them to target_buffer
                if hasattr(run.element, "tag") and isinstance(run.element.tag, str) and run.element.tag.endswith("r"):
                    # Process drawing type images
                    drawing_elements = run.element.findall(
                        ".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}drawing"
                    )
                    has_drawing = False
                    for drawing in drawing_elements:
                        blip_elements = drawing.findall(
                            ".//{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
                        )
                        for blip in blip_elements:
                            embed_id = blip.get(
                                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
                            )
                            if embed_id:
                                rel = doc.part.rels.get(embed_id)
                                if rel is not None and rel.is_external:
                                    # External image: use embed_id as key
                                    if embed_id in image_map:
                                        has_drawing = True
                                        target_buffer.append(image_map[embed_id])
                                else:
                                    # Internal image: use target_part as key
                                    image_part = doc.part.related_parts.get(embed_id)
                                    if image_part in image_map:
                                        has_drawing = True
                                        target_buffer.append(image_map[image_part])
                    # Process pict type images
                    shape_elements = run.element.findall(
                        ".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pict"
                    )
                    for shape in shape_elements:
                        # Find image data in VML
                        shape_image = shape.find(
                            ".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}binData"
                        )
                        if shape_image is not None and shape_image.text:
                            image_id = shape_image.get(
                                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
                            )
                            if image_id and image_id in doc.part.rels:
                                append_image_link(image_id, has_drawing, target_buffer)
                        # Find imagedata element in VML
                        image_data = shape.find(".//{urn:schemas-microsoft-com:vml}imagedata")
                        if image_data is not None:
                            image_id = image_data.get("id") or image_data.get(
                                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
                            )
                            if image_id and image_id in doc.part.rels:
                                append_image_link(image_id, has_drawing, target_buffer)
                if run.text.strip():
                    target_buffer.append(run.text.strip())

            def process_hyperlink(hyperlink_elem, target_buffer):
                # Helper to extract text from a hyperlink element and append it to target_buffer
                r_id = hyperlink_elem.get(qn("r:id"))

                # Extract text from runs inside the hyperlink
                link_text_parts = []
                for run_elem in hyperlink_elem.findall(qn("w:r")):
                    run = Run(run_elem, paragraph)
                    # Hyperlink text may be split across multiple runs (e.g., with different formatting),
                    # so collect all run texts first
                    if run.text:
                        link_text_parts.append(run.text)

                link_text = "".join(link_text_parts).strip()

                # Resolve URL
                if r_id:
                    try:
                        rel = doc.part.rels.get(r_id)
                        if rel and rel.is_external:
                            link_text = f"[{link_text or rel.target_ref}]({rel.target_ref})"
                    except Exception:
                        logger.exception("Failed to resolve URL for hyperlink with r:id: %s", r_id)

                if link_text:
                    target_buffer.append(link_text)

            paragraph_content = []
            # State for legacy HYPERLINK fields
            hyperlink_field_url = None
            hyperlink_field_text_parts: list[str] = []
            is_collecting_field_text = False
            # Iterate through paragraph elements in document order
            for child in paragraph._element:
                tag = child.tag
                if tag == qn("w:r"):
                    # Regular run
                    run = Run(child, paragraph)

                    # Check for fldChar (begin/end/separate) and instrText for legacy hyperlinks
                    fld_chars = child.findall(qn("w:fldChar"))
                    instr_texts = child.findall(qn("w:instrText"))

                    # Handle Fields
                    if fld_chars or instr_texts:
                        # Process instrText to find HYPERLINK "url"
                        for instr in instr_texts:
                            if instr.text and "HYPERLINK" in instr.text:
                                # Quick regex to extract URL
                                match = re.search(r'HYPERLINK\s+"([^"]+)"', instr.text, re.IGNORECASE)
                                if match:
                                    hyperlink_field_url = match.group(1)

                        # Process fldChar
                        for fld_char in fld_chars:
                            fld_char_type = fld_char.get(qn("w:fldCharType"))
                            if fld_char_type == "begin":
                                # Start of a field: reset legacy link state
                                hyperlink_field_url = None
                                hyperlink_field_text_parts = []
                                is_collecting_field_text = False
                            elif fld_char_type == "separate":
                                # Separator: if we found a URL, start collecting visible text
                                if hyperlink_field_url:
                                    is_collecting_field_text = True
                            elif fld_char_type == "end":
                                # End of field
                                if is_collecting_field_text and hyperlink_field_url:
                                    # Create markdown link and append to main content
                                    display_text = "".join(hyperlink_field_text_parts).strip()
                                    if display_text:
                                        link_md = f"[{display_text}]({hyperlink_field_url})"
                                        paragraph_content.append(link_md)
                                # Reset state
                                hyperlink_field_url = None
                                hyperlink_field_text_parts = []
                                is_collecting_field_text = False

                    # Decide where to append content
                    target_buffer = hyperlink_field_text_parts if is_collecting_field_text else paragraph_content
                    process_run(run, target_buffer)
                elif tag == qn("w:hyperlink"):
                    process_hyperlink(child, paragraph_content)
            return "".join(paragraph_content) if paragraph_content else ""

        for block in doc.iter_inner_content():
            match block:
                case Paragraph():
                    parsed_paragraph = parse_paragraph(block)
                    content.append(parsed_paragraph if parsed_paragraph.strip() else "\n")
                case Table():
                    content.append(self._table_to_markdown(block, image_map))
        return "\n".join(content)
