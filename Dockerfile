FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-hosted.txt /app/requirements-hosted.txt
RUN python -m pip install --no-cache-dir -r requirements-hosted.txt \
    && python -m pip check \
    && groupadd --gid 10001 ekb \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin ekb \
    && mkdir /data /demo \
    && chown 10001:10001 /data \
    && chmod 700 /data

# Positive source inventory: no Local entrypoints, UI, OCR/import/backup modules.
COPY src/__init__.py src/config.py src/database.py src/migrations.py src/models.py \
     src/runtime_profile.py src/hosted_config.py src/source_metadata.py \
     src/evidence_basket_service.py src/knowledge_context.py \
     src/knowledge_context_packager.py src/knowledge_memory_service.py \
     src/knowledge_object_service.py src/knowledge_search_service.py \
     src/note_geometry.py src/prompt_builder.py src/search_service.py \
     src/source_fingerprint.py src/text_utils.py /app/src/
COPY src/ai/__init__.py src/ai/provider.py src/ai/qwen_client.py \
     src/ai/embedding_store.py src/ai/rag_answer_service.py \
     src/ai/rag_prompt_builder.py /app/src/ai/
COPY src/agent/ /app/src/agent/
COPY src/hosted/ /app/src/hosted/
COPY src/hosted_api/ /app/src/hosted_api/

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('EKB_HOSTED_PORT','8000')+'/health',timeout=3); raise SystemExit(0 if r.status==200 else 1)"]
ENTRYPOINT ["python", "-m", "src.hosted.server"]
