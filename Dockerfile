# Default build (GPU): OCR runs on an NVIDIA GPU via onnxruntime's
# CUDAExecutionProvider. Built for the prod GPU host (Tesla P4). For a CPU-only
# box (e.g. the laptop) use `Dockerfile.cpu` / `docker-compose.cpu.yml` instead.
#
# Run on the GPU host with the runtime + toolkit installed:
#   docker compose up -d --build
# or plain docker:
#   docker build -t wk-score-extractor .
#   docker run --gpus all --env-file .env wk-score-extractor
#
# The cudnn-runtime base supplies CUDA 12 + cuDNN 9, which onnxruntime-gpu 1.23
# links against. Tesla P4 is Pascal (compute 6.1); needs host driver >= 525.
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

# python + ffmpeg (frame capture) + opencv/onnxruntime runtime libs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-pip \
        ffmpeg libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
# rapidocr-onnxruntime pulls in the CPU onnxruntime; swap it for the GPU build
# (having both installed makes Python import the CPU one and silently fall back).
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y onnxruntime \
    && pip install --no-cache-dir onnxruntime-gpu==1.23.2

COPY *.py teams.json ./

# Unbuffered stdout so `docker logs` shows events promptly; USE_CUDA flips the
# reader onto the GPU (monitor.py reads it; equivalent to passing --cuda).
# NVIDIA_DRIVER_CAPABILITIES adds `video` so libnvcuvid (NVDEC) is exposed for
# optional GPU decode (FFMPEG_HWACCEL=1 / --hwaccel); the CUDA base image would
# otherwise grant only compute,utility. Harmless when hwaccel is off.
ENV PYTHONUNBUFFERED=1 \
    USE_CUDA=1 \
    NVIDIA_DRIVER_CAPABILITIES=all

ENTRYPOINT ["python3", "monitor.py"]
CMD ["--ip", "10.43.70.192", "--channel", "1"]
