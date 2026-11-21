# The scorer builds this on linux/amd64 and runs it with no network. The base digest
# is the image the organizers labeled every commit in; keep it.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# sqlglot's source at the pinned commit (allowed: sqlglot's own repository at eb11aec).
# The selector reads it to see what each patch changes. It never imports or runs it:
# the cheap-test runner (prerun.py, needs requirements.lock installed) is off.
ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tar.gz
RUN mkdir -p /app/src \
    && tar -xzf /tmp/sqlglot.tar.gz -C /app/src --strip-components=1 \
    && rm /tmp/sqlglot.tar.gz

COPY selector.py prerun.py probe.py /app/
COPY maps/ /app/maps/
# The run is as user nobody on a read-only disk: everything must be world-readable.
RUN chmod -R a+rX /app

ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /tmp
CMD ["python", "/app/selector.py"]
