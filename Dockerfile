FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends git make \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml constraints.txt ./
COPY app ./app
COPY evals ./evals
COPY worker.py ./
RUN pip install --no-cache-dir -c constraints.txt . && useradd --uid 10001 --create-home app \
    && mkdir /data && chown app:app /data
USER app
ENV DATA_DIR=/data
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')"
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]


# Superset evaluator deps in their own stage, so app edits never rebuild them.
# The validator needs Python 3.11 plus Superset's dependencies; Superset itself comes from the mounted fork.
FROM python:3.12-slim AS evaluator
ENV UV_PYTHON_INSTALL_DIR=/opt/python
WORKDIR /opt
COPY docker/superset-evaluator-requirements.txt /tmp/superset-evaluator-requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.11.21 \
    && uv python install 3.11 \
    && uv venv --python 3.11 /opt/superset-venv \
    && uv pip install --no-cache --python /opt/superset-venv/bin/python -r /tmp/superset-evaluator-requirements.txt

# Live target: the app plus the evaluator, so the worker and readiness checks run fully in the container.
FROM base AS live
COPY --from=evaluator /opt/python /opt/python
COPY --from=evaluator /opt/superset-venv /opt/superset-venv
USER root
RUN git config --system --add safe.directory '*'
ENV SUPERSET_PYTHON=/opt/superset-venv/bin/python SUPERSET_REPO_PATH=/superset
USER app
