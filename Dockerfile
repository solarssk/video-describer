FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid appuser --no-create-home appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt faster-whisper==1.2.1

COPY . .
RUN chown -R appuser:appuser /app

USER appuser

# To expose the server on the network pass -e BIND_HOST=0.0.0.0 at runtime.
# Omitting the variable keeps the safe default (127.0.0.1 — localhost only).

EXPOSE 5555

CMD ["python3", "web_app.py"]
