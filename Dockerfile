    # Dockerfile
    #
    # Cloud Run container for person-detection-based video cropping.
    # Includes:
    # - ffmpeg for robust video decode/encode fallback
    # - opencv-python-headless for per-frame processing
    # - ultralytics (YOLOv8) for person detection
    #
    # Entry point: gunicorn serving api:app

    FROM python:3.11-slim

    # System deps:
    # - ffmpeg: decode/encode video (OpenCV wheels typically bundle ffmpeg, but having ffmpeg helps with edge cases)
    # - libgl1: some OpenCV operations may require it even headless in certain builds
    RUN apt-get update && \
        apt-get install -y --no-install-recommends ffmpeg git libgl1 && \
        apt-get clean && rm -rf /var/lib/apt/lists/*

    WORKDIR /app

    COPY requirements.txt .
    # Install CUDA-enabled PyTorch wheel (cu121 = CUDA 12.1, matches Cloud Run GPU driver).
    # The wheel bundles libcudart/libcublas/etc; libcuda.so.1 is bind-mounted from the host
    # by Cloud Run when --gpu 1 is set.
    # torch 2.5.1+cu121 satisfies the torch==2.5.1 pin in requirements.txt, so pip
    # skips it on the second install pass.
    RUN pip install --no-cache-dir \
          torch==2.5.1 torchvision==0.20.1 \
          --index-url https://download.pytorch.org/whl/cu121
    RUN pip install --no-cache-dir -r requirements.txt

    # Optional: pre-cache YOLO weights (downloads during first run otherwise).
    # Enable with: --build-arg PRECACHE_YOLO=1
    ARG PRECACHE_YOLO=0
    ARG MODEL_NAME=yolov8n.pt
    RUN if [ "$PRECACHE_YOLO" = "1" ]; then \
          python -c "from ultralytics import YOLO; YOLO('${MODEL_NAME}'); print('cached')" ; \
        fi

    COPY api.py worker.py cleanup.py video_cropper.py logging_utils.py ./

    ENV PORT=8080
    ENV PYTHONUNBUFFERED=1

    CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 3600 api:app
