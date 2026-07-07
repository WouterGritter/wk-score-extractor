FROM python:3.11-slim

# ffmpeg for frame capture; libgl1/libglib2.0-0/libgomp1 are runtime libraries
# for opencv + onnxruntime (pulled in by rapidocr-onnxruntime).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

# Unbuffered stdout so `docker logs` shows events promptly.
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "monitor.py"]
CMD ["--ip", "10.43.70.192", "--channel", "1"]
