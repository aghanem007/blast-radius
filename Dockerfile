# The scorer builds this on linux/amd64 and runs it with no network. The base digest
# is the image the organizers labeled every commit in; keep it.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# The early-cutoff check (earlycut.py) runs sqlglot's parser, optimizer and planner on
# the patched code. Two small pure-Python packages change how sqlglot itself behaves
# when they are importable (date arithmetic in optimizer/simplify.py, helper
# decorators in helper.py), and the tests were labeled with them installed. They are
# pinned to the versions in the kit's requirements.lock. Nothing else from that file
# is needed: no test is run with pytest here.
RUN pip install --no-cache-dir --disable-pip-version-check python-dateutil==4.0.0 mypy_extensions==1.2.0

# sqlglot's source at the pinned commit (allowed: sqlglot's own repository at eb11aec).
# The selector reads it to see what each patch changes; earlycut.py imports it.
ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tar.gz
RUN mkdir -p /app/src \
    && tar -xzf /tmp/sqlglot.tar.gz -C /app/src --strip-components=1 \
    && rm /tmp/sqlglot.tar.gz

COPY selector.py prerun.py probe.py earlycut.py /app/
COPY maps/ /app/maps/

# Bytecode for the pinned source (the run's disk is read-only, so it cannot be cached
# then), and the pinned commit's fingerprints for the early-cutoff check, made by the
# same interpreter the run uses. If the fingerprints cannot be made the build still
# succeeds: the selector then computes them once at run time, or keeps the test.
RUN python -m compileall -q /app/src/sqlglot /app/src/tests/__init__.py /app/src/tests/helpers.py \
    ; (PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 python /app/earlycut.py baseline /app/src /app/maps/tpcds_base.json \
       || echo "early-cutoff baseline not built") \
    ; chmod -R a+rX /app
# The run is as user nobody on a read-only disk: everything must be world-readable.

ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /tmp
CMD ["python", "/app/selector.py"]
