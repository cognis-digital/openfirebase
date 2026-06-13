import json

import pytest

from openfirebase.cli import main, build_parser


def test_version(capsys):
    rc = main(["version"])
    out = capsys.readouterr().out
    assert rc == 0 and "openfirebase" in out


def test_set_then_get_memory(capsys):
    rc = main(["--memory", "set", "/a/b", "42"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out.strip()) == 42


def test_set_get_persist(tmp_path, capsys):
    d = str(tmp_path / "data")
    rc = main(["--data-dir", d, "set", "/k", '{"x": 1}'])
    assert rc == 0
    capsys.readouterr()
    rc = main(["--data-dir", d, "get", "/k"])
    assert json.loads(capsys.readouterr().out.strip()) == {"x": 1}


def test_set_raw_string_value(capsys):
    main(["--memory", "set", "/s", "hello"])
    assert json.loads(capsys.readouterr().out.strip()) == "hello"


def test_parser_requires_command():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
