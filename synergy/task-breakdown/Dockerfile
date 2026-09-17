# The task-breakdown stage as its own container. It touches no Vexa service — it mounts the SAME
# workspaces volume the agent workers write to, and reads/writes files there.
FROM python:3.11-slim

# git: the workspace is a git repo and the stage commits what it writes (git is the undo).
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN pip install --no-cache-dir "httpx>=0.27,<1" "PyYAML>=6,<7"

COPY src/ /app/src/
ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    VEXA_WORKSPACES_ROOT=/workspaces \
    VEXA_TASKS_INTERVAL=60

# git refuses to operate in a tree owned by another uid; the workspaces volume is written by the
# agent workers, so declare it safe rather than chowning a volume this container does not own.
RUN git config --global --add safe.directory '*'

CMD ["python", "-m", "synergy_tasks"]
