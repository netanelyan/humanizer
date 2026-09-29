"""Tests for the web layer. Run with:  py tests/test_web.py"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import zipfile
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from humanizer.core import Settings, apply_edits_with_spans, Edit, Humanizer
from humanizer.web import (BadRequest, Handler, _output_name, _settings_from,
                           humanize_document, humanize_text)
from test_humanizer import RICH_DOCUMENT, HEADER_XML, check


# -- span tracking ----------------------------------------------------------

def test_spans_land_on_the_replacements():
    text = "We must utilize this in order to win."
    edits = [Edit(8, 15, "use", "word", "utilize"),
             Edit(21, 32, "to", "phrase", "in order to")]
    result, spans = apply_edits_with_spans(text, edits)
    check(result == "We must use this to win.", result)
    for start, end, kind, old in spans:
        check(result[start:end] in ("use", "to"), result[start:end])
    check(spans[0][3] == "utilize", spans)


def test_spans_mark_deletions_as_zero_width():
    text = "Well, it is important to note that this works."
    edits = [Edit(6, 35, "", "phrase", "it is important to note that ")]
    result, spans = apply_edits_with_spans(text, edits)
    check(spans[0][0] == spans[0][1], "deletion should be zero width")
    check(result == "Well, this works.", repr(result))


def test_spans_agree_with_the_engine():
    humanizer = Humanizer(Settings(level=8, seed=4))
    text = ("It is important to note that we must utilize this — daily. "
            "Furthermore, the long-term benefits are substantial.")
    edits = humanizer.plan(text)
    result, spans = apply_edits_with_spans(text, edits)
    check(result == Humanizer(Settings(level=8, seed=4)).run(text),
          "span-aware apply disagrees with run()")
    for start, end, kind, old in spans:
        check(0 <= start <= end <= len(result), "span out of range")


# -- settings validation ----------------------------------------------------

def test_settings_are_clamped():
    settings = _settings_from({"level": 99})
    check(settings.level == 10, settings.level)
    check(_settings_from({"level": -5}).level == 0)
    check(_settings_from({"typo_rate": 9999}).typo_rate == 200)


def test_settings_reject_junk():
    for payload, reason in (
        ({"level": "loud"}, "non-numeric level"),
        ({"seed": "abc"}, "non-numeric seed"),
        ({"preserve": "not-a-list"}, "preserve as a string"),
        ({"preserve": ["("]}, "invalid regex"),
        ({"preserve": ["x" * 500]}, "over-long pattern"),
        ({"preserve": ["a"] * 50}, "too many patterns"),
    ):
        try:
            _settings_from(payload)
        except BadRequest:
            continue
        raise AssertionError("accepted " + reason)


def test_settings_ignore_unknown_keys():
    settings = _settings_from({"level": 3, "seed": 1, "__class__": "nope",
                               "source": "/etc/passwd", "lexical": False})
    check(settings.level == 3 and settings.lexical is False)
    check(not hasattr(settings, "source"), "an unknown key was set on Settings")


def test_output_name_is_not_a_path():
    check(_output_name("../../etc/passwd") == "passwd.humanized.docx")
    check(_output_name("C:\\Users\\x\\report.docx") == "report.humanized.docx")
    check(_output_name("") == "humanized.docx")
    check(_output_name("notes.exe").endswith(".humanized.docx"))


# -- endpoints in process ---------------------------------------------------

def test_humanize_text_endpoint():
    result = humanize_text({
        "text": "It is important to note that we must utilize this — daily.",
        "settings": {"level": 8, "seed": 3, "typos": False},
    })
    check("—" not in result["text"], result["text"])
    check(result["stats"], "no stats returned")
    check(result["settings"]["level"] == 8, result["settings"])
    for span in result["spans"]:
        check(set(span) == {"start", "end", "kind", "old"}, span)


def test_humanize_text_rejects_oversize():
    try:
        humanize_text({"text": "x " * 2_000_000})
    except BadRequest:
        return
    raise AssertionError("oversize text was accepted")


def test_humanize_document_endpoint(tmp_path):
    path = os.path.join(tmp_path, "in.docx")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        archive.writestr("word/document.xml", RICH_DOCUMENT)
        archive.writestr("word/header1.xml", HEADER_XML)
    with open(path, "rb") as handle:
        data = handle.read()

    result = humanize_document(data, {"name": "My Report.docx",
                                      "settings": {"level": 7, "seed": 2}})
    check(result["name"] == "My Report.humanized.docx", result["name"])
    check(result["rewritten"] >= 3, result["rewritten"])
    check(result["changes"], "no changes reported")

    rebuilt = base64.b64decode(result["file"])
    out = os.path.join(tmp_path, "out.docx")
    with open(out, "wb") as handle:
        handle.write(rebuilt)
    with zipfile.ZipFile(out) as archive:
        check(archive.testzip() is None, "returned archive is corrupt")
        xml = archive.read("word/document.xml").decode("utf-8")
    check("<w:tbl>" in xml and "<w:sectPr>" in xml, "structure was lost")


def test_humanize_document_rejects_non_docx():
    for payload, reason in ((b"hello world", "plain text"),
                            (b"", "empty body")):
        try:
            humanize_document(payload, {})
        except BadRequest:
            continue
        raise AssertionError("accepted " + reason)


# -- over real HTTP ---------------------------------------------------------

class _Server:
    def __enter__(self):
        Handler.quiet = True
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def get(self, path):
        with urlopen(self.url + path, timeout=10) as response:
            return response.status, response.read(), dict(response.headers)

    def post(self, path, payload):
        request = Request(self.url + path,
                          data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json"},
                          method="POST")
        try:
            with urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())


def test_http_serves_the_page():
    with _Server() as server:
        status, body, headers = server.get("/")
        check(status == 200, status)
        check(b"<title>humanizer</title>" in body, "index.html was not served")
        check("Content-Security-Policy" in headers, "no CSP header")

        for path, kind in (("/app.css", "text/css"), ("/app.js", "text/javascript")):
            status, body, headers = server.get(path)
            check(status == 200, path)
            check(kind in headers["Content-Type"], headers["Content-Type"])


def test_http_rejects_path_traversal():
    with _Server() as server:
        for path in ("/../web.py", "/../../pyproject.toml", "/..%2fweb.py",
                     "/webui/../../core.py"):
            try:
                status, body, headers = server.get(path)
            except HTTPError as error:
                status = error.code
            check(status in (403, 404), "%s returned %s" % (path, status))


def test_http_api_roundtrip():
    with _Server() as server:
        status, data = server.post("/api/humanize", {
            "text": "It is important to note that we utilize this — daily.",
            "settings": {"level": 9, "seed": 1, "typos": False},
        })
        check(status == 200, data)
        check("—" not in data["text"], data["text"])

        status, data = server.post("/api/humanize", {"text": 5})
        check(status == 400 and "error" in data, data)

        status, data = server.post("/api/humanize", {"settings": {"level": "x"},
                                                     "text": "hi"})
        check(status == 400, data)

        status, data = server.post("/api/nope", {})
        check(status == 404, status)

        status, data = server.get("/api/defaults")[0], json.loads(
            server.get("/api/defaults")[1])
        check(status == 200 and "settings" in data, data)


def test_http_same_settings_same_answer():
    payload = {"text": "We must utilize a wide range of tools in order to win. "
                       "Furthermore, it is essential to foster collaboration.",
               "settings": {"level": 7, "seed": 77}}
    with _Server() as server:
        first = server.post("/api/humanize", payload)[1]
        second = server.post("/api/humanize", payload)[1]
    check(first["text"] == second["text"], "seeded requests diverged")


def _run_all():
    import tempfile
    failures = 0
    for name, function in sorted(globals().items()):
        if not name.startswith("test_") or not callable(function):
            continue
        try:
            if "tmp_path" in function.__code__.co_varnames[:function.__code__.co_argcount]:
                with tempfile.TemporaryDirectory() as directory:
                    function(directory)
            else:
                function()
        except Exception as error:                      # noqa: BLE001
            failures += 1
            print("FAIL %s\n     %s: %s" % (name, type(error).__name__, error))
        else:
            print("ok   %s" % name)
    print("\n%s" % ("all passed" if not failures else "%d failed" % failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
