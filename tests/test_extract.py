import io
import zipfile

import pytest

from api_summary.extract import Extractor
from api_summary.store import ServiceError


def limits(settings):
    return {
        key: getattr(settings, key)
        for key in (
            "max_input_chars",
            "max_pdf_pages",
            "max_zip_entries",
            "max_zip_expanded_bytes",
            "max_zip_ratio",
        )
    }


def archive(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries:
            z.writestr(name, data)
    buffer.seek(0)
    return buffer


def test_zip_multiple_and_unicode(settings):
    extractor = Extractor(limits(settings))
    extractor.extract(
        archive([("un.txt", "Préfecture"), ("dir/deux.md", "Deuxième document")]), "dossier.zip"
    )
    assert len(extractor.documents) == 2
    assert extractor.documents[0][1] == "Préfecture"
    assert extractor.documents[1][0] == "dossier.zip/dir/deux.md"


@pytest.mark.parametrize(
    "name", ["../secret.txt", "/etc/passwd", "C:/secret.txt", "a\\secret.txt", "nested.zip"]
)
def test_bad_zip_entries(settings, name):
    with pytest.raises(ServiceError):
        Extractor(limits(settings)).extract(archive([(name, "test")]), "dossier.zip")


def test_limits_no_silent_truncation(settings):
    budget = limits(settings)
    budget["max_input_chars"] = 3
    with pytest.raises(ServiceError):
        Extractor(budget).extract(io.BytesIO(b"hello"), "a.txt")
    budget = limits(settings)
    budget["max_zip_expanded_bytes"] = 3
    with pytest.raises(ServiceError):
        Extractor(budget).extract(archive([("a.txt", "hello")]), "a.zip")


def test_docx(settings):
    xml = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Bonjour</w:t></w:r></w:p></w:body></w:document>'
    extractor = Extractor(limits(settings))
    extractor.extract(archive([("word/document.xml", xml)]), "a.docx")
    assert extractor.documents == [("a.docx", "Bonjour")]
