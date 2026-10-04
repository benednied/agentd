from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]


def load_module(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


updater = load_module(ROOT / "tools/prepare_codex_update.py")
probe = load_module(ROOT / "deploy/security/runtime_sandbox_probe.py")


def seed(root):
    for name, _ in (*updater.SDK_PINS, updater.CLI_PIN):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((ROOT / name).read_text())


def test_update_synchronizes_sdk_guards_and_keeps_cli_separate(tmp_path):
    seed(tmp_path)
    assert updater.prepare(tmp_path, "1.0.0", "2.0.0")
    for name, pattern in updater.SDK_PINS:
        assert updater.re.search(pattern, (tmp_path / name).read_text())[2] == "1.0.0"
    assert "ARG CODEX_VERSION=2.0.0" in (tmp_path / "Dockerfile").read_text()
    assert not updater.prepare(tmp_path, "1.0.0", "2.0.0")


@pytest.mark.parametrize("sdk,cli", [("1.0.0rc1", "2.0.0"), ("1.0.0", "0.0.1")])
def test_bad_candidate_does_not_partially_write(tmp_path, sdk, cli):
    seed(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ValueError):
        updater.prepare(tmp_path, sdk, cli)
    assert all(p.read_bytes() == data for p, data in before.items())


class CatalogClient:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    def request(self, method, params, **kwargs):
        assert method == "model/list"
        assert params["includeHidden"] is True
        self.calls.append(params)
        return next(self.pages)


def page(models=(), cursor=None):
    return SimpleNamespace(data=models, next_cursor=cursor)


def model(effort="xhigh"):
    return SimpleNamespace(
        model="gpt-6-luna",
        supported_reasoning_efforts=[SimpleNamespace(reasoning_effort=effort)],
    )


def test_model_check_follows_catalog_pagination():
    client = CatalogClient([page(cursor="next"), page([model()])])
    probe._require_model(client, "gpt-6-luna")
    assert client.calls[1]["cursor"] == "next"


@pytest.mark.parametrize(
    "pages,error",
    [
        ([page()], "unavailable"),
        ([page([model("low")])], "reasoning effort"),
        ([page(cursor="same"), page(cursor="same")], "repeated"),
    ],
)
def test_model_check_fails_closed(pages, error):
    with pytest.raises(RuntimeError, match=error):
        probe._require_model(CatalogClient(pages), "gpt-6-luna")


def test_inconsistent_sdk_guards_stop_update(tmp_path):
    seed(tmp_path)
    path = tmp_path / "deploy/security/runtime_sandbox_probe.py"
    pattern = updater.SDK_PINS[-1][1]
    path.write_text(updater.re.sub(pattern, lambda m: m[1] + "0.0.1", path.read_text()))
    before = path.read_text()
    with pytest.raises(ValueError, match="disagree"):
        updater.prepare(tmp_path, "1.0.0", "2.0.0")
    assert path.read_text() == before
