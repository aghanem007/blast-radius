# Offline map builder: the scorer's exact base image and packages, plus coverage,
# with sqlglot at the pinned commit. Build context: work/ (see tools/build_map/run.ps1).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock coverage==7.6.10
COPY src/sqlglot /sqlglot
COPY recorder.py build_map.py /tools/
WORKDIR /sqlglot
ENV PYTHONPATH=/sqlglot:/tools \
    SKIP_INTEGRATION=0 \
    PYTHONDONTWRITEBYTECODE=1 \
    MAP_OUT=/out
