import io
import json

import pytest

from parse_broken_json import __version__
from parse_broken_json.cli import main


def test_repairs_a_file_to_stdout(tmp_path, capsys):
    path = tmp_path / "in.json"
    path.write_text('{"a": [1, 2, "b": 3', encoding="utf-8")
    assert main([str(path)]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out) == {"a": [1, 2], "b": 3}
    assert out.startswith("{\n  ")  # indented by default
    assert err == ""


def test_reads_stdin_and_writes_compact(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("Here: {'a': 1, 'b': [1, 2,]}"))
    assert main(["--compact"]) == 0
    assert capsys.readouterr().out == '{"a": 1, "b": [1, 2]}\n'


def test_output_file_and_inline(tmp_path):
    src = tmp_path / "in.json"
    src.write_text('{"a": 1,}', encoding="utf-8")
    out = tmp_path / "out.json"
    assert main([str(src), "-o", str(out), "--compact"]) == 0
    assert out.read_text(encoding="utf-8") == '{"a": 1}\n'
    assert main([str(src), "-i", "--compact"]) == 0
    assert src.read_text(encoding="utf-8") == '{"a": 1}\n'


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
    monkeypatch.setattr("sys.stdin", io.StringIO('{"a": 1, "b": "x'))
    assert main(["--stream-stable", "--compact"]) == 0
    assert capsys.readouterr().out == '{"a": 1}\n'


def test_version(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert capsys.readouterr().out.strip() == f"parse-broken-json {__version__}"
