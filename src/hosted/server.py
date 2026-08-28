"""Formal Hosted entrypoint: python -m src.hosted.server.

Importing this module performs no storage bootstrap, network, listen or writes.
Only main() owns process logging, restrictive umask and storage lifetime.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import uvicorn

from src.hosted.logging import (
    HostedLogEvent,
    attach_hosted_log_file,
    configure_hosted_logging,
    log_event,
)
from src.hosted.runtime import compose_hosted_dependencies
from src.hosted.storage import bootstrap_hosted_storage, validate_hosted_sqlite_runtime_policy
from src.hosted_api.app import create_hosted_app
from src.hosted_config import HostedSettings, load_hosted_settings


def validate_worker_environment() -> None:
    """No heuristic process counting: reject conflicting explicit deployment hints."""
    for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        if name in os.environ and os.environ[name] != "1":
            raise ValueError("hosted_worker_configuration_invalid")
    validate_hosted_sqlite_runtime_policy(process_count=1, worker_count=1)


def build_server_config(app: object, settings: HostedSettings) -> uvicorn.Config:
    """WP3 is the sole authority for interpreting forwarded headers."""
    validate_worker_environment()
    return uvicorn.Config(
        app, host="0.0.0.0", port=settings.hosted_port, workers=1,
        proxy_headers=False, forwarded_allow_ips="", access_log=False,
        reload=False, server_header=False, log_config=None, log_level="info",
        loop="asyncio", http="h11", ws="none", lifespan="on", interface="asgi3",
    )


def main() -> int:
    """Fail closed with safe categories; leave missing-key/budget processes alive."""
    storage = None
    previous_umask = None
    result = 1
    with configure_hosted_logging() as handlers:
        try:
            settings = load_hosted_settings()
            validate_worker_environment()
            previous_umask = os.umask(0o077)
            storage = bootstrap_hosted_storage(settings, process_count=1, worker_count=1)
            attach_hosted_log_file(settings.log_path, handlers)
            dependencies = compose_hosted_dependencies(settings, storage)
            app = create_hosted_app(settings=settings, dependencies=dependencies)
            original_lifespan = app.router.lifespan_context

            @asynccontextmanager
            async def lifespan(application):
                try:
                    async with original_lifespan(application) as state:
                        log_event(HostedLogEvent.STARTED)
                        yield state
                finally:
                    # Uvicorn re-raises captured SIGTERM after its shutdown.
                    # Close inside lifespan before that signal, not only outside run().
                    storage.close()
                    log_event(HostedLogEvent.STOPPED)

            app.router.lifespan_context = lifespan
            server = uvicorn.Server(build_server_config(app, settings))
            server.run()
            result = 0 if server.started else 1
            if result:
                log_event(HostedLogEvent.STARTUP_FAILED)
        except KeyboardInterrupt:
            # Uvicorn re-raises handled SIGINT too; do not print a Python traceback.
            result = 130
            log_event(HostedLogEvent.INTERRUPTED)
        except (Exception, SystemExit):
            log_event(HostedLogEvent.STARTUP_FAILED)
        finally:
            try:
                if storage is not None:
                    storage.close()
            except Exception:
                log_event(HostedLogEvent.SHUTDOWN_FAILED)
                result = 1
            finally:
                if previous_umask is not None:
                    os.umask(previous_umask)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
