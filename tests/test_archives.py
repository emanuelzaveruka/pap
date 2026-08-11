"""Book filenames, Drive paths, and the download guards. No network.

Every test here corresponds to a bug that actually occurred against the live API
during the first run of this pipeline.
"""

from __future__ import annotations

import os

import pytest

from pap.archives.fetch import MIN_BOOK_BYTES, BookOutcome, download_to
from pap.sinks.gdrive import studeo_folder_path
from pap.sources.studeo import _book_filename


# -- filenames --------------------------------------------------------------
def test_a_readable_name_is_built_from_the_title():
    name = _book_filename("Gestão de Projetos", "b2466509cb1824a9", "pdf")
    assert name.startswith("gestão-de-projetos-")
    assert name.endswith(".pdf")


def test_a_path_in_the_hash_cannot_escape_the_books_directory():
    """Regression: nomeArquivoHash is not always a hash. A real response contained
    `/33552/5.zip`, and slicing it raw put a path separator in the filename — which
    would write outside the books directory and create nested Drive folders."""
    name = _book_filename("Comunicação Assertiva", "/33552/5.zip", "zip")
    assert "/" not in name
    assert "\\" not in name
    assert os.path.basename(name) == name


def test_two_books_with_the_same_title_do_not_collide():
    """Without the hash suffix, one would silently overwrite the other."""
    a = _book_filename("Empreendedorismo", "aaaaaaaa1111", "pdf")
    b = _book_filename("Empreendedorismo", "bbbbbbbb2222", "pdf")
    assert a != b


@pytest.mark.parametrize("tipo,expected", [("pdf", ".pdf"), ("PDF", ".pdf"),
                                           ("zip", ".zip"), (None, ".pdf")])
def test_extension_comes_from_the_declared_type(tipo, expected):
    assert _book_filename("Livro", "abcd1234", tipo).endswith(expected)


def test_a_hostile_extension_cannot_inject_a_path():
    assert "/" not in _book_filename("Livro", "abcd1234", "../../etc/passwd")


def test_an_empty_title_still_produces_a_usable_name():
    name = _book_filename("", "abcd1234", "pdf")
    assert name and not name.startswith("-")


# -- Drive layout -----------------------------------------------------------
def test_books_are_filed_under_the_module_folder():
    path = studeo_folder_path("Studeo", year=2026, module_folder="53-2026",
                              discipline="FUNDAMENTOS DE REDES", bucket="livros")
    assert path == "Studeo/2026/53-2026/FUNDAMENTOS DE REDES/livros"


def test_a_slash_in_a_discipline_name_does_not_create_a_folder_level():
    path = studeo_folder_path("Studeo", year=2026, module_folder="52-2026",
                              discipline="ALGORITMOS/ESTRUTURAS", bucket="livros")
    assert path.count("/") == 4


def test_an_unparsed_module_still_files_the_book_somewhere():
    """Misfiled is recoverable; dropped is not."""
    path = studeo_folder_path("Studeo", year=None, module_folder="unknown",
                              discipline="X", bucket="livros")
    assert "sem-ano" in path


# -- download guards --------------------------------------------------------
def test_an_error_page_returned_with_http_200_is_rejected(tmp_path, monkeypatch):
    """Studeo's CDN answers 200 with a short error body. Treating that as a book
    would put a corrupt PDF in Drive and fail much later, in the chunker."""
    import pap.archives.fetch as fetch

    class _Response:
        status_code = 200
        headers: dict = {}

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def iter_content(self, chunk_size): yield b"<html>error</html>"

    monkeypatch.setattr(fetch.requests, "get", lambda *a, **k: _Response())
    target = str(tmp_path / "book.pdf")

    with pytest.raises(RuntimeError, match="error page"):
        download_to("https://example.invalid/x", target)

    assert not os.path.exists(target)
    assert not os.path.exists(f"{target}.part")


def test_a_partial_download_never_looks_like_a_finished_book(tmp_path, monkeypatch):
    """Bytes land in *.part and are renamed only after the transfer completes — the
    same rule the database dump follows."""
    import pap.archives.fetch as fetch

    class _Response:
        status_code = 200
        headers: dict = {}

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def iter_content(self, chunk_size):
            yield b"x" * MIN_BOOK_BYTES
            raise ConnectionError("connection dropped mid-transfer")

    monkeypatch.setattr(fetch.requests, "get", lambda *a, **k: _Response())
    target = str(tmp_path / "book.pdf")

    with pytest.raises(ConnectionError):
        download_to("https://example.invalid/x", target)

    assert not os.path.exists(target), "a partial transfer must not leave a finished name"
    assert os.path.exists(f"{target}.part"), "the partial is kept so the next run resumes"


def test_the_size_cap_is_enforced_mid_stream(tmp_path, monkeypatch):
    """The server may not declare Content-Length, so the cap is also checked while
    writing — the disk on this box has already reported that it is failing."""
    import pap.archives.fetch as fetch

    class _Response:
        status_code = 200
        headers: dict = {}

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def iter_content(self, chunk_size):
            for _ in range(10):
                yield b"y" * MIN_BOOK_BYTES

    monkeypatch.setattr(fetch.requests, "get", lambda *a, **k: _Response())

    with pytest.raises(RuntimeError, match="cap"):
        download_to("https://example.invalid/x", str(tmp_path / "big.pdf"),
                    max_bytes=MIN_BOOK_BYTES * 2)


def test_a_declared_oversize_download_is_refused_before_any_bytes(tmp_path, monkeypatch):
    import pap.archives.fetch as fetch

    class _Response:
        status_code = 200
        headers = {"Content-Length": str(500 * 1024 * 1024)}

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def iter_content(self, chunk_size):
            raise AssertionError("must refuse before reading the body")

    monkeypatch.setattr(fetch.requests, "get", lambda *a, **k: _Response())

    with pytest.raises(RuntimeError, match="refusing to download"):
        download_to("https://example.invalid/x", str(tmp_path / "huge.pdf"),
                    max_bytes=10 * 1024 * 1024)


def test_a_server_ignoring_the_range_header_restarts_instead_of_appending(tmp_path, monkeypatch):
    """Appending to a partial when the server replied 200 (not 206) would duplicate the
    prefix — corruption that only surfaces when something tries to read the PDF."""
    import pap.archives.fetch as fetch

    target = tmp_path / "book.pdf"
    partial = tmp_path / "book.pdf.part"
    partial.write_bytes(b"OLD" * 1000)

    class _Response:
        status_code = 200  # range ignored
        headers: dict = {}

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def iter_content(self, chunk_size): yield b"N" * MIN_BOOK_BYTES

    monkeypatch.setattr(fetch.requests, "get", lambda *a, **k: _Response())
    size = download_to("https://example.invalid/x", str(target))

    assert size == MIN_BOOK_BYTES
    assert b"OLD" not in target.read_bytes()


# -- reporting --------------------------------------------------------------
def test_outcome_reports_every_counter():
    text = str(BookOutcome(downloaded=1, reused=2, uploaded=3, skipped=4, failed=5))
    for fragment in ("downloaded=1", "reused=2", "uploaded=3", "skipped=4", "failed=5"):
        assert fragment in text
