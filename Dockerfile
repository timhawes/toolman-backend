FROM python:3.14-alpine
COPY --from=ghcr.io/astral-sh/uv:0.11.23 /uv /uvx /bin/

COPY . /app
WORKDIR /app
RUN uv sync --locked
ENV PATH="/app/.venv/bin:$PATH"

ENV \
  TOOLMAN_CACHE_PATH=/cache \
  TOOLMAN_COMMAND_SOCKET=/run/toolman.sock

CMD ["./server.py"]
VOLUME ["/cache"]
