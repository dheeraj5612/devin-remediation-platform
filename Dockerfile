FROM python:3.12-slim AS superset-eval
ARG SUPERSET_SHA=5ecb19cf92ae8dedbf5b33ec92324cc77c4ee10e
RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential pkg-config libssl-dev libffi-dev libpq-dev libsasl2-dev libldap2-dev \
    default-libmysqlclient-dev libicu-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/superset
RUN git init . && git remote add origin https://github.com/apache/superset.git \
    && git fetch --depth 1 origin "$SUPERSET_SHA" && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "$SUPERSET_SHA"
RUN pip install --no-cache-dir -r requirements/base.txt \
    && pip install --no-cache-dir --no-deps -e . \
    && pip install --no-cache-dir pytest==9.0.2 pytest-mock==3.15.1
RUN mkdir -p /evaluator /reports && chown 1000:1000 /reports
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1
LABEL remediation.baseline=$SUPERSET_SHA
USER 1000:1000

FROM python:3.12-slim AS control
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
RUN apt-get update && apt-get install -y --no-install-recommends docker.io \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY app app
COPY evals evals
COPY worker.py .
RUN mkdir -p /data
EXPOSE 8000
CMD ["python", "-m", "app.cli", "serve"]
