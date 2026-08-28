"""WP5 S1-S12: server policy, import safety, logging and lifecycle ownership."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from test_hosted_api_readiness import offline as offline  # noqa: F401
from test_hosted_storage import configured
from test_hosted_storage import demo as demo  # noqa: F401
from test_hosted_storage import protect_production as protect_production  # noqa: F401

import src.hosted.server as server
from src.config import PROJECT_ROOT, Settings
from src.hosted.logging import (
    HostedLogEvent,
    attach_hosted_log_file,
    configure_hosted_logging,
    log_event,
)
from src.hosted_config import HostedSettings
from src.runtime_profile import RuntimeConfigurationError


@pytest.fixture(autouse=True)
def clean_workers(monkeypatch):
    for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("profile", [None, "", "local", "Hosted", "production"])
def test_main_requires_explicit_hosted_without_storage(profile, monkeypatch, capsys):
    if profile is None:
        monkeypatch.delenv("EKB_RUNTIME_PROFILE")
    else:
        monkeypatch.setenv("EKB_RUNTIME_PROFILE", profile)
    storage = Mock(side_effect=AssertionError("No storage before explicit Hosted config"))
    monkeypatch.setattr(server, "bootstrap_hosted_storage", storage)
    assert server.main() == 1
    storage.assert_not_called()
    assert capsys.readouterr().err == "hosted_startup_failed\n"


@pytest.mark.parametrize("port", [1, 8000, 65535, "9090"])
def test_port_valid_and_server_transport_flags(demo, monkeypatch, port):
    monkeypatch.setenv("UVICORN_PROXY_HEADERS", "true")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    monkeypatch.setenv("UVICORN_RELOAD", "true")
    settings = configured(demo, hosted_port=port)
    config = server.build_server_config(Mock(), settings)
    assert (config.host, config.port, config.workers) == ("0.0.0.0", int(port), 1)
    assert not config.proxy_headers and not config.access_log and not config.reload
    assert not config.server_header and config.forwarded_allow_ips == ""
    assert config.log_config is None and config.interface == "asgi3"


@pytest.mark.parametrize("port", [0, 65536, "", " 8000", "8000.0", True, 8000.5])
def test_port_invalid(demo, port):
    with pytest.raises(RuntimeConfigurationError):
        configured(demo, hosted_port=port)


@pytest.mark.parametrize("name", ["WEB_CONCURRENCY", "UVICORN_WORKERS"])
@pytest.mark.parametrize("value", ["0", "2", "", "auto", "1.0", " 1", "01"])
def test_conflicting_worker_env_fails_before_storage(demo, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(server, "load_hosted_settings", lambda: demo.settings)
    storage = Mock()
    monkeypatch.setattr(server, "bootstrap_hosted_storage", storage)
    assert server.main() == 1
    storage.assert_not_called()


def test_worker_one_explicit_is_allowed(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    monkeypatch.setenv("UVICORN_WORKERS", "1")
    server.validate_worker_environment()


def test_loaded_uvicorn_preserves_immediate_peer_before_wp3(demo):
    observed = []

    async def app(scope, receive, send):
        observed.append(scope["client"])

    config = server.build_server_config(app, demo.settings)
    config.load()
    scope = {"type": "http", "client": ("192.0.2.9", 34567), "scheme": "http",
             "headers": [(b"x-forwarded-for", b"203.0.113.1"),
                         (b"x-forwarded-proto", b"https")]}
    asyncio.run(config.loaded_app(scope, Mock(), Mock()))
    assert observed == [("192.0.2.9", 34567)]
    assert scope["scheme"] == "http"


@pytest.mark.parametrize("retry", [0, 1, 2, "2"])
def test_hosted_retry_range_and_local_model_defaults(demo, retry):
    settings = configured(demo, ai_max_extra_attempts=retry)
    assert settings.ai_max_extra_attempts == int(retry)
    assert HostedSettings.model_fields["ai_max_extra_attempts"].default == 0
    assert Settings.model_fields["ai_max_extra_attempts"].default == 2
    for name in ("ai_llm_model", "ai_llm_model_hard", "ai_embedding_model", "ai_rerank_model",
                 "ai_timeout_seconds"):
        assert getattr(settings, name) == Settings.model_fields[name].default


@pytest.mark.parametrize("retry", [-1, 3, "", "auto", True, 0.5])
def test_hosted_retry_invalid(demo, retry):
    with pytest.raises(RuntimeConfigurationError):
        configured(demo, ai_max_extra_attempts=retry)


@pytest.mark.parametrize("key,budget,ready", [(True, 100, 200), (False, 100, 503), (True, 0, 503)])
def test_lifespan_closes_before_server_returns_and_restores_process(
    demo, monkeypatch, key, budget, ready,
):
    demo.settings = configured(demo, ai_api_key="TEST_ONLY_FAKE_KEY" if key else "",
                               ai_daily_token_budget=budget)
    monkeypatch.setattr(server, "load_hosted_settings", lambda: demo.settings)
    original_bootstrap = server.bootstrap_hosted_storage
    owned = []
    original_handlers = logging.getLogger().handlers[:]
    original_hook = sys.excepthook
    old_mask = os.umask(0o077)
    os.umask(old_mask)
    umask_spy = Mock(wraps=os.umask)
    monkeypatch.setattr(server.os, "umask", umask_spy)

    def bootstrap(*args, **kwargs):
        assert kwargs == {"process_count": 1, "worker_count": 1}
        # Windows CRT does not retain POSIX permission bits. Verify ordering
        # here; actual Linux file modes belong to the real container gate.
        umask_spy.assert_called_once_with(0o077)
        storage = original_bootstrap(*args, **kwargs)
        owned.append(storage)
        return storage

    class TestServer:
        started = False

        def __init__(self, config):
            self.config = config

        def run(self):
            with TestClient(self.config.app) as client:
                assert not owned[0]._closed
                for path, expected in (("/health", 200), ("/ready", ready), ("/docs", 404),
                                       ("/redoc", 404), ("/openapi.json", 404)):
                    assert client.get(path).status_code == expected
                self.started = True
            assert owned[0]._closed  # Before Uvicorn could re-raise SIGTERM.

    monkeypatch.setattr(server, "bootstrap_hosted_storage", bootstrap)
    monkeypatch.setattr(server.uvicorn, "Server", TestServer)
    assert server.main() == 0
    assert owned[0]._closed
    observed_mask = os.umask(old_mask)
    assert observed_mask == old_mask
    assert logging.getLogger().handlers == original_handlers
    assert sys.excepthook is original_hook
    assert demo.settings.log_path.read_text(encoding="utf-8").splitlines() == [
        "hosted_started", "hosted_stopped",
    ]


@pytest.mark.parametrize("failure", ["bootstrap", "logfile", "composition", "serve", "unstarted"])
def test_startup_errors_safe_and_storage_cleanup(demo, monkeypatch, capsys, failure):
    marker = "TEST_ONLY_SECRET /private/path Authorization full-prompt"
    monkeypatch.setattr(server, "load_hosted_settings", lambda: demo.settings)
    owned = []
    original = server.bootstrap_hosted_storage

    def bootstrap(*args, **kwargs):
        if failure == "bootstrap":
            raise ValueError(marker)
        storage = original(*args, **kwargs)
        owned.append(storage)
        return storage

    monkeypatch.setattr(server, "bootstrap_hosted_storage", bootstrap)
    if failure == "logfile":
        monkeypatch.setattr(server, "attach_hosted_log_file", Mock(side_effect=OSError(marker)))
    if failure == "composition":
        monkeypatch.setattr(
            server, "compose_hosted_dependencies", Mock(side_effect=ValueError(marker)),
        )
    fake = Mock(started=False)
    if failure == "serve":
        fake.run.side_effect = SystemExit(marker)
    monkeypatch.setattr(server.uvicorn, "Server", Mock(return_value=fake))
    assert server.main() == 1
    assert all(storage._closed for storage in owned)
    assert capsys.readouterr().err == "hosted_startup_failed\n"


def test_logs_never_format_raw_content_exception_or_access(demo, capsys):
    demo.settings.logs_dir.mkdir()
    logger = logging.getLogger("jieba")
    previous = logger.handlers[:]
    with configure_hosted_logging() as handlers:
        attach_hosted_log_file(demo.settings.log_path, handlers)
        log_event(HostedLogEvent.STARTED)
        try:
            raise RuntimeError("PRIVATE_EXCEPTION /private/path")
        except RuntimeError:
            logger.exception("PRIVATE_PROMPT %s", {"Authorization": "PRIVATE_KEY"})
        logging.getLogger("uvicorn.error").warning("PRIVATE_RESPONSE")
        logging.getLogger("uvicorn.access").error("PRIVATE_XFF PRIVATE_PATH")
        log_event(HostedLogEvent.STOPPED)
    expected = "hosted_started\nhosted_runtime_error\nhosted_runtime_warning\nhosted_stopped\n"
    assert demo.settings.log_path.read_text(encoding="utf-8") == expected
    assert capsys.readouterr().err == expected
    assert logger.handlers == previous


def test_sigint_reraise_closes_storage_without_traceback(demo, monkeypatch, capsys):
    monkeypatch.setattr(server, "load_hosted_settings", lambda: demo.settings)
    original = server.bootstrap_hosted_storage
    owned = []

    def bootstrap(*args, **kwargs):
        storage = original(*args, **kwargs)
        owned.append(storage)
        return storage

    fake = Mock()
    fake.run.side_effect = KeyboardInterrupt
    monkeypatch.setattr(server, "bootstrap_hosted_storage", bootstrap)
    monkeypatch.setattr(server.uvicorn, "Server", Mock(return_value=fake))
    assert server.main() == 130
    assert owned[0]._closed
    assert capsys.readouterr().err == "hosted_interrupted\n"


def test_fresh_import_has_no_io_or_local_heavy_dependencies():
    code = r'''
import importlib.abc, json, os, sys
from pathlib import Path
blocked = {'PIL','streamlit','fitz','pymupdf','rapidocr','rapidocr_onnxruntime',
           'onnxruntime','httpx','pytest','ruff','src.runtime','src.document_service',
           'src.pdf_service','src.backup_service','src.document_deletion_service'}
class BlockImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name+'.') for name in blocked):
            raise AssertionError('Forbidden dependency: '+fullname)
sys.meta_path.insert(0, BlockImports())
def audit(event, args):
    if event.startswith('socket.') or event == 'sqlite3.connect':
        raise AssertionError('Import side effect: '+event)
    if event == 'open':
        mode, flags = args[1:3]
        if (isinstance(mode,str) and any(c in mode for c in 'wax+')) or (
            isinstance(flags,int) and flags & (os.O_CREAT|os.O_WRONLY|os.O_RDWR)):
            raise AssertionError('Import filesystem write')
    if event in {'os.mkdir','os.remove','os.rename'}:
        raise AssertionError('Import filesystem mutation')
sys.addaudithook(audit)
import src.hosted.server
print(json.dumps(sorted(str(Path(m.__file__).relative_to(Path.cwd())).replace('\\','/')
    for name,m in sys.modules.items() if name.startswith('src') and getattr(m,'__file__',None))))
'''
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("EKB_") and k != "DASHSCOPE_API_KEY"}
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=PROJECT_ROOT,
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    modules = json.loads(result.stdout)
    assert "src/hosted/server.py" in modules and "src/runtime.py" not in modules
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text().replace("\\\n", " ")
    copied = [source for line in dockerfile.splitlines() if line.startswith("COPY ")
              for source in line.split()[1:-1]]
    assert all(any(module == source or (source.endswith("/") and module.startswith(source))
                   for source in copied) for module in modules)
