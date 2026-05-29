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

ENV BIND_HOST=0.0.0.0

EXPOSE 5555

CMD ["python3", "web_app.py"]
