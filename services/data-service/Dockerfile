FROM python:3.12-slim
ARG PIP_INDEX_URL=https://pypi.org/simple
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/server:/app/shared
COPY services/data-service/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
# Only shared knowledge/domain modules enter this image. Workstation MCP
# entrypoints, DCC bridges, installers and client configuration stay outside.
COPY src/fx_agent/__init__.py src/fx_agent/archive.py src/fx_agent/baselines.py \
     src/fx_agent/config.py src/fx_agent/data_api.py src/fx_agent/data_context.py \
     src/fx_agent/data_contracts.py src/fx_agent/data_import.py src/fx_agent/dream.py \
     src/fx_agent/effect_cards.py src/fx_agent/effect_study.py src/fx_agent/handoff.py \
     src/fx_agent/learning.py src/fx_agent/memory.py src/fx_agent/migration.py \
     src/fx_agent/module_guides.py src/fx_agent/modules.py src/fx_agent/production_rules.py \
     src/fx_agent/store.py src/fx_agent/task_gates.py src/fx_agent/templates.py \
     src/fx_agent/template_extract.py /app/shared/fx_agent/
COPY src/fx_agent/vendor /app/shared/fx_agent/vendor
COPY skills /app/skills
COPY services/data-service/src /app/server
EXPOSE 18790
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -m fx_data_service.healthcheck
CMD ["python", "-m", "fx_data_service"]
