FROM python:3.12-slim
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
