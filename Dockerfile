FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt faster-whisper

COPY . .

ENV BIND_HOST=0.0.0.0

EXPOSE 5555

CMD ["python3", "web_app.py"]
