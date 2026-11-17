"""The legacy metrics checker must not bypass the scoped demo runner."""

import pytest

from scripts import accept_k8s


def test_legacy_recovery_flag_exits_before_any_cluster_or_network_access(monkeypatch):
    def forbidden(*_args):
        pytest.fail("read-only checker attempted external access for a retired mutation flag")

    monkeypatch.setattr(accept_k8s, "kubectl", forbidden)
    monkeypatch.setattr(accept_k8s, "get_json", forbidden)
    monkeypatch.setattr("sys.argv", ["accept_k8s.py", "--prometheus-url",
                                     "http://127.0.0.1:9090", "--exercise-recovery"])
    with pytest.raises(SystemExit) as exc:
        accept_k8s.main()
    assert exc.value.code == 2
