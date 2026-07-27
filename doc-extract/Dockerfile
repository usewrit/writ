FROM python:3.11-slim

WORKDIR /app

# RapidOCR (opencv-headless) needs a couple of shared libs at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN adduser --disabled-password --gecos '' appuser
USER appuser

EXPOSE 8092

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8092"]
