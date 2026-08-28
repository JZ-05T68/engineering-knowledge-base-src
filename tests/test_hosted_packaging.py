"""WP5 packaging declarations and an explicit real Linux Docker integration gate.

RUN_HOSTED_CONTAINER_TESTS=1 opts into build/run. Without an engine the gate is
NOT EXECUTED, never a static substitute for container verification.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path
from uuid import uuid4

import pytest
from packaging.requirements import Requirement
from test_hosted_api_readiness import offline as offline  # noqa: F401
from test_hosted_storage import KB_UUID
from test_hosted_storage import demo as demo  # noqa: F401
from test_hosted_storage import protect_production as protect_production  # noqa: F401

from src.config import PROJECT_ROOT
from src.hosted.storage_validation import validate_seed_artifact


def test_dockerfile_positive_inventory_nonroot_and_health_policy():
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    logical = dockerfile.replace("\\\n", " ")
    assert "FROM python:3.11-slim" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert 'ENTRYPOINT ["python", "-m", "src.hosted.server"]' in dockerfile
    assert "pip install --no-cache-dir -r requirements-hosted.txt" in dockerfile
    assert not re.search(r"(?im)^(?:COPY|ADD)\s+\.(?:\s|/)", logical)
    assert not re.search(r"(?im)^ADD\s", logical)
    assert "requirements.txt" not in dockerfile
    env = [line for line in logical.splitlines() if line.startswith(("ENV ", "ARG "))]
    assert env == ["ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1"]
    health = logical[logical.index("HEALTHCHECK"):logical.index("ENTRYPOINT")]
    assert "/health" in health and "/ready" not in health
    assert "urllib.request" in health and "EKB_HOSTED_PORT" in health
    copied = [line.split()[1:-1] for line in logical.splitlines() if line.startswith("COPY ")]
    assert copied and all(Path(source).parts[0] in {"src", "requirements-hosted.txt"}
                          for group in copied for source in group)
    assert "src/runtime.py" not in logical and "src/document_service.py" not in logical


def test_dockerignore_deny_default_and_private_exclusions():
    lines = [line.strip() for line in (PROJECT_ROOT / ".dockerignore").read_text().splitlines()
             if line.strip() and not line.startswith("#")]
    assert lines[0] == "**"
    assert {line for line in lines if line.startswith("!")} == {
        "!Dockerfile", "!requirements-hosted.txt", "!src/", "!src/**/", "!src/**/*.py",
    }
    last_include = max(i for i, line in enumerate(lines) if line.startswith("!"))
    for excluded in (".git", ".env*", "data", "backups", "logs", "runtime", "staging-data",
                     "tests", ".venv", "__pycache__", "*.db", "*.db-wal", "*.db-shm",
                     "*.pdf", "*.png", "*.md"):
        assert lines.index("**/" + excluded) > last_include


def test_hosted_requirements_exact_installed_closure_without_local_heavy_dependencies():
    requirements = [Requirement(line) for line in
                    (PROJECT_ROOT / "requirements-hosted.txt").read_text().splitlines()
                    if line and not line.startswith("#")]
    normalize = lambda name: name.lower().replace("_", "-")  # noqa: E731
    pinned = {normalize(item.name): item for item in requirements}
    needed, pending = set(), ["fastapi", "uvicorn", "pydantic-settings", "jieba"]
    while pending:
        name = normalize(pending.pop())
        if name in needed:
            continue
        needed.add(name)
        distribution = importlib.metadata.distribution(name)
        assert str(pinned[name].specifier) == "==" + distribution.version
        for raw in distribution.requires or ():
            dependency = Requirement(raw)
            if not dependency.marker or dependency.marker.evaluate({"extra": ""}):
                pending.append(dependency.name)
    active = {name for name, item in pinned.items()
              if not item.marker or item.marker.evaluate({"extra": ""})}
    assert active == needed
    assert not active & {"pillow", "pymupdf", "streamlit", "rapidocr", "onnxruntime",
                         "pytest", "httpx", "ruff", "rapidfuzz"}


def _build_context(destination: Path) -> dict[str, str]:
    """Archive only validated positive COPY inputs, before contacting the daemon.

    Keep the repository Dockerfile and .dockerignore byte-for-byte. Never walk
    private roots, read production data, or rely only on daemon-side exclusion.
    """
    logical = (PROJECT_ROOT / "Dockerfile").read_text().replace("\\\n", " ")
    files = {PROJECT_ROOT / "Dockerfile", PROJECT_ROOT / ".dockerignore"}
    for line in logical.splitlines():
        if not line.startswith("COPY "):
            continue
        for name in line.split()[1:-1]:
            assert name == "requirements-hosted.txt" or name.startswith("src/")
            path = PROJECT_ROOT / name
            assert path.resolve().is_relative_to(PROJECT_ROOT)
            files.update(path.rglob("*.py") if path.is_dir() else [path])
    manifest = {}
    with tarfile.open(destination, "w") as archive:
        for path in sorted(files):
            relative = path.relative_to(PROJECT_ROOT).as_posix()
            assert relative in {"Dockerfile", ".dockerignore", "requirements-hosted.txt"} or (
                relative.startswith("src/") and relative.endswith(".py")
            )
            assert not {"data", "tests", "logs", "backups", "runtime", "staging-data",
                        ".git", "__pycache__"}.intersection(path.relative_to(PROJECT_ROOT).parts)
            assert path.is_file() and path.stat().st_nlink == 1
            assert all(not parent.is_symlink() for parent in (path, *path.parents))
            assert path.resolve().is_relative_to(PROJECT_ROOT)
            info = archive.gettarinfo(str(path), arcname=relative)
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ""
            info.mode = 0o644
            with path.open("rb") as source:
                archive.addfile(info, source)
            manifest[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    with tarfile.open(destination) as archive:
        assert set(archive.getnames()) == set(manifest)
        assert all(member.isfile() for member in archive.getmembers())
    return manifest


def test_build_context_is_positive_source_only(tmp_path):
    manifest = _build_context(tmp_path / "context.tar")
    assert "src/hosted/server.py" in manifest and "Dockerfile" in manifest
    assert "src/runtime.py" not in manifest
    assert not any(name.endswith((".db", ".db-wal", ".db-shm", ".pdf", ".png", ".md"))
                   for name in manifest)


def _docker(*args: str, check: bool = True, timeout: int = 60,
            stdin=None) -> subprocess.CompletedProcess:
    result = subprocess.run(["docker", *args], cwd=PROJECT_ROOT, stdin=stdin,
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout)
    if check:
        assert result.returncode == 0, result.stdout[-12000:] + result.stderr[-12000:]
    return result


@pytest.mark.skipif(os.environ.get("RUN_HOSTED_CONTAINER_TESTS") != "1",
                    reason="LOCAL LINUX CONTAINER VERIFICATION NOT EXECUTED (explicit opt-in)")
def test_real_linux_container_matrix(demo):
    """Actual D1-D20, dependency, RO-root, network-none and same-volume restart gate."""
    assert shutil.which("docker"), "Linux Docker is required; do not install automatically"
    assert _docker("info", "--format", "{{.OSType}}").stdout.strip() == "linux"
    suffix = uuid4().hex[:12]
    image, volume, container = ("ekb-wp5-" + part + "-" + suffix
                                for part in ("image", "data", "run"))
    created_image = created_volume = created_container = False
    evidence = {"synthetic_uuid": KB_UUID, "synthetic_sha256": demo.settings.demo_db_sha256}
    validate_seed_artifact(demo.artifact, demo.settings.demo_db_sha256, KB_UUID)
    context = demo.artifact.parent / "context.tar"
    manifest = _build_context(context)
    evidence["context_files"] = len(manifest)
    evidence["context_sha256"] = hashlib.sha256(context.read_bytes()).hexdigest()
    print("AUDITED BUILD CONTEXT:", len(manifest), "source/build files; private data absent",
          flush=True)
    try:
        with context.open("rb") as archive:
            build = _docker("build", "--tag", image, "-", stdin=archive, timeout=600)
        (demo.artifact.parent / "build.log").write_text(build.stdout + build.stderr)
        created_image = True
        history = _docker("history", "--no-trunc", image).stdout
        assert "TEST_ONLY_FAKE_KEY" not in history
        image_info = json.loads(_docker("image", "inspect", image).stdout)[0]
        evidence["image_id"] = image_info["Id"]
        image_config = image_info["Config"]
        assert image_config["User"] == "10001:10001"
        assert not any(item.startswith("EKB_") for item in image_config["Env"])
        saved_image = demo.artifact.parent / "image.tar"
        _docker("image", "save", "--output", str(saved_image), image, timeout=120)
        layers_checked = 0
        with tarfile.open(saved_image) as saved:
            image_manifest = json.load(saved.extractfile("manifest.json"))
            for layer_name in image_manifest[0]["Layers"]:
                with tarfile.open(fileobj=saved.extractfile(layer_name), mode="r|*") as layer:
                    for member in layer:
                        name = member.name.removeprefix("./").rstrip("/")
                        parts = Path(name).parts
                        assert ".git" not in parts
                        assert not any(part == ".env" or part.startswith(".env.") for part in parts)
                        assert not name.endswith((".db", ".db-wal", ".db-shm")), name
                        if member.isfile() and name.startswith("app/"):
                            assert name.removeprefix("app/") in manifest, name
                    layers_checked += 1
        evidence["image_layers_audited"] = layers_checked
        print("IMAGE BUILD AND LAYER PRIVACY PASS:", evidence["image_id"], flush=True)
        _docker("volume", "create", volume)
        created_volume = True
        # Only a newly generated synthetic artifact; production DB is never read/copied.
        demo.artifact.parent.chmod(0o755)
        demo.artifact.chmod(0o444)

        def start(seed: bool, key: bool = True, budget: int = 100, observer: str | None = None):
            nonlocal created_container
            args = ["run", "--detach", "--name", container, "--network", "none",
                    "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                    "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m,mode=1777",
                    "--mount", f"type=volume,source={volume},target=/data",
                    "--env", "EKB_RUNTIME_PROFILE=hosted", "--env", "EKB_DATA_ROOT=/data",
                    "--env", f"EKB_DEMO_KB_UUID={KB_UUID}",
                    "--env", f"EKB_AI_DAILY_TOKEN_BUDGET={budget}"]
            if key:
                args += ["--env", "EKB_AI_API_KEY=TEST_ONLY_FAKE_KEY"]
            if seed:
                args += ["--mount",
                         f"type=bind,source={demo.artifact},target=/demo/demo.db,readonly",
                         "--env", "EKB_DEMO_DB_ARTIFACT=/demo/demo.db",
                         "--env", "EKB_DEMO_DB_SHA256=" + demo.settings.demo_db_sha256]
            if observer is not None:
                args += ["--entrypoint", "python"]
            _docker(*args, image, *(["-c", observer] if observer is not None else []))
            created_container = True
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                probe = _docker("exec", container, "python", "-c",
                                "import urllib.request; "
                                "assert urllib.request.urlopen('http://127.0.0.1:8000/health').status==200",
                                check=False, timeout=10)
                if probe.returncode == 0:
                    return
                time.sleep(0.25)
            pytest.fail("Container health deadline exceeded")

        def stop(observed: bool = False):
            nonlocal created_container
            _docker("stop", "--time", "30", container, timeout=45)
            state = json.loads(_docker("inspect", container).stdout)[0]["State"]
            # Uvicorn re-raises handled SIGTERM after its completed ASGI shutdown.
            assert state["ExitCode"] in (0, 143) and not state["OOMKilled"]
            logs = _docker("logs", container)
            assert "hosted_stopped" in logs.stdout + logs.stderr
            assert "TEST_ONLY_FAKE_KEY" not in logs.stdout + logs.stderr
            assert "Traceback" not in logs.stdout + logs.stderr
            if observed:
                assert "TEST_STORAGE_OBSERVER_CLOSED" in logs.stdout + logs.stderr
            evidence.setdefault("stop_exit_codes", []).append(state["ExitCode"])
            _docker("rm", container)
            created_container = False

        start(True)
        inspect = json.loads(_docker("inspect", container).stdout)[0]
        assert inspect["HostConfig"]["ReadonlyRootfs"] is True
        assert inspect["HostConfig"]["NetworkMode"] == "none"
        assert not inspect["HostConfig"]["PortBindings"]
        mounts = {item["Destination"]: item for item in inspect["Mounts"]}
        assert mounts["/data"]["RW"] and not mounts["/demo/demo.db"]["RW"]
        probe = r'''
import importlib.util, json, os, sqlite3, urllib.error, urllib.request
from pathlib import Path
from src.agent import build_single_step_executor
from src.agent.tools import ToolContext, ToolInput, ToolSideEffect
from src.database import Database
assert os.getuid() == 10001
for name in ('streamlit','streamlit_image_coordinates','fitz','PIL',
             'rapidocr','rapidocr_onnxruntime','onnxruntime',
             'pytest','httpx','ruff'):
    assert importlib.util.find_spec(name) is None, name
assert {p.name for p in Path('/app').iterdir()} == {'src','requirements-hosted.txt'}
assert not Path('/app/src/runtime.py').exists()
assert all(p.suffix == '.py' for p in Path('/app/src').rglob('*') if p.is_file())
assert not list(Path('/data').rglob('*.pdf')) and not list(Path('/data').rglob('*.png'))
assert {p.name for p in Path('/data').iterdir()} == {'database','logs'}
assert {p.name for p in Path('/data/database').iterdir()} == {
    'knowledge.db','knowledge.db-wal','knowledge.db-shm'}
mounts = Path('/proc/mounts').read_text().splitlines()
assert any(line.split()[1:3] == ['/tmp','tmpfs'] for line in mounts)
for p in Path('/data').rglob('*'):
    assert p.stat().st_mode & 0o077 == 0
for path in ('/app/forbidden', '/forbidden', '/demo/demo.db'):
    try: Path(path).write_text('forbidden')
    except OSError: pass
    else: raise AssertionError('Read-only mount was writable')
Path('/tmp/ephemeral-proof').write_text('synthetic')
db=sqlite3.connect('file:/data/database/knowledge.db?mode=ro',uri=True)
assert db.execute('SELECT MAX(version) FROM schema_migrations').fetchone() == (12,)
assert db.execute('PRAGMA journal_mode').fetchone() == ('wal',)
assert db.execute('SELECT COUNT(*) FROM ai_calls').fetchone() == (0,)
assert any('ENABLE_FTS5' in row[0] for row in db.execute('PRAGMA compile_options'))
kb=db.execute('SELECT kb_uuid FROM knowledge_base_meta').fetchone()[0]
db.close()
# Test-only read handle: do not initialize/migrate a live server's database.
database=object.__new__(Database)
database.database_path=Path('/data/database/knowledge.db')
executor=build_single_step_executor(database)
definitions=executor._registry.list_definitions()
assert len(definitions)==7 and all(d.side_effect is ToolSideEffect.READ_ONLY for d in definitions)
result=executor._handlers['page_search'](ToolInput('page_search',{'query':'motor'}),ToolContext())
assert result.status.value in ('success','partial')
for path, expected in (('/health',200),('/ready',200),('/v0.6/sources/'+kb+':page:1',200),
                       ('/docs',404),('/redoc',404),('/openapi.json',404)):
    try: response=urllib.request.urlopen('http://127.0.0.1:8000'+path)
    except urllib.error.HTTPError as error: response=error
    assert response.code == expected, path
    if '/sources/' in path:
        display=json.load(response)
        assert 'demo://' not in json.dumps(display) and '/data' not in json.dumps(display)
        assert 'source_path' not in display and 'image_path' not in display
print(json.dumps({'kb_uuid':kb,'schema':12,'journal':'wal','ai_calls':0,
                  'db_inode':Path('/data/database/knowledge.db').stat().st_ino,
                  'uid':os.getuid(),'python':__import__('platform').python_version(),
                  'sqlite':sqlite3.sqlite_version}))
'''
        evidence.update(json.loads(_docker("exec", container, "python", "-c", probe).stdout))
        assert evidence["kb_uuid"] == KB_UUID
        _docker("exec", container, "python", "-m", "pip", "check")
        # Execute the image's actual configured healthcheck, not a replacement.
        _docker("exec", container, *image_config["Healthcheck"]["Test"][1:])
        process_lines = _docker("top", container, "-eo", "pid,args").stdout.splitlines()[1:]
        assert len(process_lines) == 1 and "src.hosted.server" in process_lines[0]
        print("FIRST SEED / FTS5 / HTTP / UID / READ-ONLY / PIP CHECK PASS", flush=True)
        _docker("exec", container, "python", "-c", """
import sqlite3
c=sqlite3.connect('/data/database/knowledge.db')
c.execute('''INSERT INTO ai_calls(call_uuid,capability,model,prompt_sha256,
input_chars,status,source_feature,created_at) VALUES
('synthetic-restart-marker','completion','test',?,0,'rejected','test','2026')''', ('a'*64,))
c.commit()
c.close()
""")
        stop()
        start(False)
        _docker("exec", container, "python", "-c", f"""
import pathlib,sqlite3,urllib.request
assert not pathlib.Path('/demo/demo.db').exists()
assert not pathlib.Path('/tmp/ephemeral-proof').exists()
assert pathlib.Path('/data/database/knowledge.db').stat().st_ino=={evidence['db_inode']}
c=sqlite3.connect('file:/data/database/knowledge.db?mode=ro',uri=True)
assert c.execute('PRAGMA integrity_check').fetchone()==('ok',)
assert c.execute('PRAGMA journal_mode').fetchone()==('wal',)
assert c.execute('SELECT MAX(version) FROM schema_migrations').fetchone()==(12,)
assert c.execute('SELECT kb_uuid FROM knowledge_base_meta').fetchone()[0]=='{KB_UUID}'
assert c.execute("SELECT COUNT(*) FROM ai_calls WHERE call_uuid='synthetic-restart-marker'")\
.fetchone()[0]==1
assert urllib.request.urlopen('http://127.0.0.1:8000/ready').status==200
c.close()
""")
        stop()
        print("PERSISTENT RESTART WITHOUT SEED / SAME DB INODE AND UUID PASS", flush=True)
        for key, budget, reason in ((False, 100, "ai_not_configured"),
                                    (True, 0, "budget_not_configured")):
            start(False, key=key, budget=budget)
            _docker("exec", container, "python", "-c",
                    "import json,urllib.request,urllib.error\n"
                    "try: urllib.request.urlopen('http://127.0.0.1:8000/ready')\n"
                    "except urllib.error.HTTPError as e:\n"
                    f" assert e.code==503 and json.load(e)['reasons']==['{reason}']\n"
                    "else: raise AssertionError('Expected not ready')")
            stop()
        # Observe actual Config return and Storage.close in the production main
        # without replacing its functions, adding routes or altering any policy.
        observer = r'''
import json,runpy,sys
from pathlib import Path
def observe(frame,event,value):
    if event != 'return': return
    if frame.f_code.co_name == 'build_server_config' and value is not None:
        fields=('workers','proxy_headers','access_log','reload','host','port')
        Path('/tmp/server-config.json').write_text(json.dumps({k:getattr(value,k) for k in fields}))
    if frame.f_code.co_name == 'close' and frame.f_code.co_filename.endswith('/hosted/storage.py'):
        assert frame.f_locals['self']._closed
        print('TEST_STORAGE_OBSERVER_CLOSED',flush=True)
sys.setprofile(observe)
runpy.run_module('src.hosted.server',run_name='__main__')
'''
        start(False, observer=observer)
        observed_config = json.loads(_docker("exec", container, "python", "-c",
                                            "print(open('/tmp/server-config.json').read())").stdout)
        assert observed_config == {"workers": 1, "proxy_headers": False, "access_log": False,
                                   "reload": False, "host": "0.0.0.0", "port": 8000}
        evidence["actual_uvicorn_config"] = observed_config
        stop(observed=True)
        # Conflict must fail before touching the existing persistent database.
        digest_code = """
from pathlib import Path
import hashlib, sqlite3
from src.hosted.logging import HostedLogEvent
with sqlite3.connect('file:/data/database/knowledge.db?mode=ro', uri=True) as connection:
    assert connection.execute('PRAGMA integrity_check').fetchone() == ('ok',)
events = Path('/data/logs/engineering-kb.log').read_text().splitlines()
assert 'hosted_stopped' in events and set(events) <= {event.value for event in HostedLogEvent}
print(hashlib.sha256(Path('/data/database/knowledge.db').read_bytes()).hexdigest())
"""
        def volume_digest():
            return _docker("run", "--rm", "--network", "none", "--read-only", "--mount",
                           f"type=volume,source={volume},target=/data,readonly", "--entrypoint",
                           "python", image, "-c", digest_code).stdout.strip()
        before_conflict = volume_digest()
        conflict = _docker("run", "--rm", "--network", "none", "--read-only", "--mount",
                           f"type=volume,source={volume},target=/data", "--env",
                           "EKB_RUNTIME_PROFILE=hosted", "--env", "EKB_DATA_ROOT=/data",
                           "--env", "WEB_CONCURRENCY=2", image, check=False)
        assert conflict.returncode == 1
        assert conflict.stderr.strip() == "hosted_startup_failed"
        assert volume_digest() == before_conflict
        evidence["worker_conflict"] = "fail_closed_db_unchanged"
        evidence["application_external_network"] = 0
        evidence["result"] = "PASS"
        _docker("tag", image, "ekb-v060-wp5:test")
        evidence["retained_tag"] = "ekb-v060-wp5:test"
        evidence_path = Path(os.environ.get("WP5_CONTAINER_EVIDENCE",
                                          str(demo.artifact.parent / "container-evidence.json")))
        assert not evidence_path.resolve().is_relative_to(PROJECT_ROOT)
        evidence_path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print("CONTAINER MATRIX PASS; EVIDENCE:", evidence_path, flush=True)
    finally:
        if created_container:
            _docker("rm", "--force", container, check=False)
        if created_volume:
            _docker("volume", "rm", volume, check=False)
        if created_image:
            _docker("image", "rm", image, check=False)
