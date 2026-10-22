"""Bounded extraction, run in a disposable child process by the worker."""

import json
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

import pdfplumber

from .store import ServiceError


class Extractor:
    def __init__(self, limits):
        self.limits = limits
        self.characters = 0
        self.entries = 0
        self.expanded = 0
        self.documents = []

    def add(self, name, text):
        self.characters += len(text)
        if self.characters > self.limits["max_input_chars"]:
            raise ServiceError("Extracted text exceeds MAX_INPUT_CHARS; no partial summary was produced")
        if not text.strip():
            raise ServiceError(f"No readable text in {name[:200]}; OCR is not configured")
        self.documents.append((name, text))

    def checked_members(self, archive):
        members = archive.infolist()
        self.entries += len(members)
        if self.entries > self.limits["max_zip_entries"]:
            raise ServiceError("Too many archive entries")
        result = []
        for item in members:
            path = PurePosixPath(item.filename)
            mode = item.external_attr >> 16
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in item.filename
                or (path.parts and ":" in path.parts[0])
                or stat.S_ISLNK(mode)
                or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))
            ):
                raise ServiceError("Unsafe archive path or special file")
            if item.flag_bits & 1:
                raise ServiceError("Encrypted archives are not supported")
            self.expanded += item.file_size
            if (
                self.expanded > self.limits["max_zip_expanded_bytes"]
                or item.file_size > max(item.compress_size, 1) * self.limits["max_zip_ratio"]
            ):
                raise ServiceError("Archive expansion limit exceeded")
            if not item.is_dir():
                result.append(item)
        return result

    def extract(self, source, name, allow_zip=True):
        suffix = Path(name).suffix.lower()
        if suffix == ".zip" and allow_zip:
            with zipfile.ZipFile(source) as archive:
                for item in self.checked_members(archive):
                    with archive.open(item) as entry:
                        # Spooled child-process temporary file, not a user-controlled path.
                        import tempfile

                        with tempfile.SpooledTemporaryFile(max_size=1024**2) as temp:
                            count = 0
                            while data := entry.read(1024**2):
                                count += len(data)
                                if count > item.file_size:
                                    raise ServiceError("Archive entry size mismatch")
                                temp.write(data)
                            temp.seek(0)
                            self.extract(temp, name + "/" + item.filename, allow_zip=False)
            return
        if suffix == ".pdf":
            with pdfplumber.open(source) as pdf:
                if len(pdf.pages) > self.limits["max_pdf_pages"]:
                    raise ServiceError("PDF page limit exceeded")
                # Validate during extraction, rather than concatenate unbounded pages.
                parts = []
                count = 0
                for page in pdf.pages:
                    text = page.extract_text() or ""
                    count += len(text) + 1
                    if self.characters + count > self.limits["max_input_chars"]:
                        raise ServiceError("Extracted text exceeds MAX_INPUT_CHARS")
                    parts.append(text)
                    page.close()
                self.add(name, "\n".join(parts))
            return
        if suffix == ".docx":
            with zipfile.ZipFile(source) as archive:
                self.checked_members(archive)
                with archive.open("word/document.xml") as xml:
                    data = xml.read(min(32 * 1024**2, self.limits["max_zip_expanded_bytes"]) + 1)
                if len(data) > min(32 * 1024**2, self.limits["max_zip_expanded_bytes"]):
                    raise ServiceError("DOCX XML limit exceeded")
                if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
                    raise ServiceError("XML declarations are not supported")
                root = ElementTree.fromstring(data)
                ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                text = "\n".join("".join(p.itertext()) for p in root.findall(".//w:p", ns))
                self.add(name, text)
            return
        if suffix not in {".txt", ".md", ".csv", ".json"}:
            raise ServiceError(f"Unsupported document: {name[:200]}")
        limit = (self.limits["max_input_chars"] - self.characters) * 4 + 1
        if hasattr(source, "read"):
            data = source.read(limit)
        else:
            with Path(source).open("rb") as handle:
                data = handle.read(limit)
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ServiceError("Text documents must use UTF-8") from None
        self.add(name, text)


def main():
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    payload = json.load(sys.stdin)
    extractor = Extractor(payload["limits"])
    try:
        for path, name in payload["files"]:
            extractor.extract(path, name)
        if not extractor.documents:
            raise ServiceError("No readable documents")
        print(json.dumps({"documents": extractor.documents}))
    except Exception:
        # Document contents, filesystem details and parser exceptions stay private.
        print(
            json.dumps(
                {
                    "error": "Document extraction failed or a configured safety limit was exceeded. Check format, UTF-8, OCR and archive limits."
                }
            )
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
