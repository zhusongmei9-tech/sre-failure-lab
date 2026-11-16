import stat

import pytest

from scripts.init_env import initialize


def test_private_environment_without_overwrite(tmp_path, capsys):
    template = tmp_path / "template"
    template.write_text("LAB_TOKEN=\nGRAFANA_ADMIN_PASSWORD=\nAPI_PORT=8000\n")
    output = tmp_path / ".env"
    initialize(template, output, token_factory=lambda _: "test-only-not-a-secret-value-123456")
    assert "API_PORT=8000" in output.read_text()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert capsys.readouterr().out == ""
    original = output.read_bytes()
    with pytest.raises(FileExistsError):
        initialize(template, output, token_factory=lambda _: "replacement")
    assert output.read_bytes() == original


def test_bad_template_does_not_create_file(tmp_path):
    template = tmp_path / "template"
    template.write_text("LAB_TOKEN=\n")
    output = tmp_path / ".env"
    with pytest.raises(ValueError):
        initialize(template, output, token_factory=lambda _: "unused")
    assert not output.exists()
