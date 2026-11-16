import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import lab


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--seconds", "0"),
        ("--seconds", "301"),
        ("--rps", "0"),
        ("--rps", "21"),
        ("--delay-ms", "-1"),
        ("--delay-ms", "5001"),
        ("--error-rate", "nan"),
        ("--error-rate", "inf"),
        ("--error-rate", "1.1"),
        ("--error-rate", "-0.1"),
        ("--cpu-iterations", "0"),
        ("--cpu-iterations", "5000001"),
    ],
)
def test_invalid_arguments_never_recover_or_contact_docker(monkeypatch, flag, value):
    monkeypatch.setattr(sys, "argv", ["lab.py", "cpu", flag, value])
    recover, compose, api = Mock(), Mock(), Mock()
    monkeypatch.setattr(lab, "recover", recover)
    monkeypatch.setattr(lab, "compose", compose)
    monkeypatch.setattr(lab, "api", api)
    with pytest.raises(SystemExit) as error:
        lab.main()
    assert error.value.code == 2
    recover.assert_not_called()
    compose.assert_not_called()
    api.assert_not_called()


def test_recover_authenticates_before_mutating_redis(monkeypatch):
    monkeypatch.setattr(lab, "api", Mock(side_effect=RuntimeError("unauthorized")))
    run = Mock()
    monkeypatch.setattr(lab.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="unauthorized"):
        lab.recover()
    run.assert_not_called()


def test_api_token_read_inside_container_not_embedded_in_command(monkeypatch, capsys):
    compose = Mock(return_value=SimpleNamespace(stdout='{"ok":true}'))
    monkeypatch.setattr(lab, "compose", compose)
    assert lab.api("/lab/status") == {"ok": True}
    program = compose.call_args.kwargs["input"]
    token = "test-only-not-a-real-secret-token-1234"
    assert token not in program
    monkeypatch.setenv("LAB_TOKEN", token)
    received = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self):
            return b'{"ok":true}'

    def open_request(req, timeout):
        received.append(req)
        assert timeout == 10
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    exec(compile(program, "<test-cli-program>", "exec"), {})  # noqa: S102 (our own CLI source)
    assert received[0].get_header("Authorization") == "Bearer " + token
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    monkeypatch.delenv("LAB_TOKEN")
    with pytest.raises(SystemExit, match="LAB_TOKEN is missing"):
        exec(compile(program, "<test-cli-program>", "exec"), {})  # noqa: S102 (our own CLI source)
    assert len(received) == 1


def test_compose_default_timeout_and_long_operation_override(monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(lab.subprocess, "run", run)
    lab.compose("ps")
    assert run.call_args.kwargs["timeout"] == 30
    lab.compose("up", timeout=120)
    assert run.call_args.kwargs["timeout"] == 120
