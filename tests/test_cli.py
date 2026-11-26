import io
import json
import os
import signal
import stat

import pytest

from parse_broken_json import __version__
from parse_broken_json.cli import main


def fake_stdin(data):
    """Stand in for sys.stdin, whose underlying bytes the CLI reads."""
    return io.TextIOWrapper(io.BytesIO(data), encoding="utf-8")


def test_repairs_a_file_to_stdout(tmp_path, capsys):
    path = tmp_path / "in.json"
    path.write_text('{"a": [1, 2, "b": 3', encoding="utf-8")
    assert main([str(path)]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out) == {"a": [1, 2], "b": 3}
    assert out.startswith("{\n  ")  # indented by default
    assert err == ""


def test_reads_stdin_and_writes_compact(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", fake_stdin(b"Here: {'a': 1, 'b': [1, 2,]}"))
    assert main(["--compact"]) == 0
    assert capsys.readouterr().out == '{"a": 1, "b": [1, 2]}\n'


def test_stdin_replaces_undecodable_bytes_like_a_file(tmp_path, monkeypatch, capsys):
    data = b'{"a": "caf\xe9"}'  # Latin-1, not UTF-8
    path = tmp_path / "in.json"
    path.write_bytes(data)
    assert main([str(path), "--compact"]) == 0
    from_file = capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", fake_stdin(data))
    assert main(["--compact"]) == 0
    assert capsys.readouterr().out == from_file == '{"a": "caf\ufffd"}\n'


def test_lone_surrogates_are_written_back_as_escapes(tmp_path, capsys):
    # An unpaired \u escape is valid JSON, but the lone surrogate it decodes to
    # has no UTF-8 encoding. Writing it raised UnicodeEncodeError, and with -i
    # left the input file empty. It goes back out as the same escape.
    value = {"a": chr(0xD800), "b" + chr(0xDCE9): "caf\N{LATIN SMALL LETTER E WITH ACUTE}"}
    path = tmp_path / "in.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    assert main([str(path), "--compact"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == value
    assert "caf\N{LATIN SMALL LETTER E WITH ACUTE}" in out  # other characters stay as they are
    assert main([str(path), "-i", "--compact"]) == 0
    assert path.read_text(encoding="utf-8") == out


def test_output_file_and_inline(tmp_path):
    src = tmp_path / "in.json"
    src.write_text('{"a": 1,}', encoding="utf-8")
    out = tmp_path / "out.json"
    assert main([str(src), "-o", str(out), "--compact"]) == 0
    assert out.read_text(encoding="utf-8") == '{"a": 1}\n'
    assert main([str(src), "-i", "--compact"]) == 0
    assert src.read_text(encoding="utf-8") == '{"a": 1}\n'


@pytest.mark.skipif(not hasattr(signal, "SIGXFSZ"), reason="needs RLIMIT_FSIZE")
def test_inline_keeps_the_input_when_the_disk_is_full(tmp_path):
    import resource

    # Files cannot grow past 100 bytes, as on a full disk. Overwriting the
    # input in place left only its first 100 bytes; now it is left as it was.
    text = '{"a": "' + "x" * 1000 + '",}'
    path = tmp_path / "in.json"
    path.write_text(text, encoding="utf-8")
    soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    handler = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # fail the write, not the process
    resource.setrlimit(resource.RLIMIT_FSIZE, (100, hard))
    try:
        with pytest.raises(OSError):
            main([str(path), "-i", "--compact"])
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
        signal.signal(signal.SIGXFSZ, handler)
    assert path.read_text(encoding="utf-8") == text
    assert [entry.name for entry in tmp_path.iterdir()] == ["in.json"]  # no temporary file left


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions and symbolic links")
def test_inline_keeps_the_permissions_and_follows_symbolic_links(tmp_path):
    real = tmp_path / "real.json"
    real.write_text('{"a": 1,}', encoding="utf-8")
    real.chmod(0o640)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    assert main([str(link), "-i", "--compact"]) == 0
    assert link.is_symlink()
    assert real.read_text(encoding="utf-8") == '{"a": 1}\n'
    assert stat.S_IMODE(real.stat().st_mode) == 0o640
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["link.json", "real.json"]


def test_inline_overwrites_in_place_when_the_directory_is_read_only(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError("the directory is read-only")

    monkeypatch.setattr("tempfile.mkstemp", refuse)
    path = tmp_path / "in.json"
    path.write_text('{"a": 1,}', encoding="utf-8")
    assert main([str(path), "-i", "--compact"]) == 0
    assert path.read_text(encoding="utf-8") == '{"a": 1}\n'


def test_inline_needs_a_file(capsys):
    assert main(["-i"]) == 2
    assert "needs a file" in capsys.readouterr().err


def test_report_lists_repairs(tmp_path, capsys):
    path = tmp_path / "in.json"
    path.write_text('{"a": 1,, "b": 2}', encoding="utf-8")
    assert main([str(path), "--report", "--compact"]) == 0
    out, err = capsys.readouterr()
    assert out == '{"a": 1, "b": 2}\n'
    assert err == "8: extra comma\n"


def test_keys_multiple_and_options(tmp_path, capsys):
    path = tmp_path / "in.json"
    path.write_text('{"a": 1, "z": 2} {"a": 3, "z": 4}', encoding="utf-8")
    assert main([str(path), "--keys", "a", "--multiple", "first", "--compact"]) == 0
    assert capsys.readouterr().out == '{"a": 1}\n'
    assert main([str(path), "--keys", "a, z", "--compact"]) == 0
    assert capsys.readouterr().out == '[{"a": 1, "z": 2}, {"a": 3, "z": 4}]\n'


def test_strict_and_nothing_found_fail(tmp_path, capsys):
    path = tmp_path / "in.json"
    path.write_text('{"a": 1,}', encoding="utf-8")
    assert main([str(path), "--strict"]) == 1
    assert "trailing comma at position 8" in capsys.readouterr().err
    path.write_text("no json here", encoding="utf-8")
    assert main([str(path)]) == 1
    assert "no JSON value found" in capsys.readouterr().err


def test_stream_stable_flag(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", fake_stdin(b'{"a": 1, "b": "x'))
    assert main(["--stream-stable", "--compact"]) == 0
    assert capsys.readouterr().out == '{"a": 1}\n'


def test_version(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert capsys.readouterr().out.strip() == f"parse-broken-json {__version__}"
