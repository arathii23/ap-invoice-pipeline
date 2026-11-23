FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libgl1 libglib2.0-0 libgomp1 tesseract-ocr tesseract-ocr-eng && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
# RapidOCR (onnxruntime + OpenCV: ~300 MB of RAM while it runs) is left out of the image: Tesseract does the OCR here in well under 100 MB.
RUN grep -vi rapidocr requirements.txt > /tmp/requirements-docker.txt && pip install --no-cache-dir -r /tmp/requirements-docker.txt
COPY . .
# Render injects PORT at runtime; 8765 is the local default. The container serves the public demo: every visitor gets a private, auto-expiring
# workspace (AP_WORKSPACES=1), so "Clear history" can only ever clear their own. API keys are supplied at run time, never baked into the image.
# AP_OCR_MAX_RSS_MB: the OCR worker is stopped if it grows past this, so a 512 MB host is never pushed over its limit (the run is held with a clear error).
ENV HOST=0.0.0.0 PORT=8765 PYTHONUNBUFFERED=1 AP_WORKSPACES=1 AP_OCR_MAX_RSS_MB=290
EXPOSE 8765
CMD ["python", "server.py"]
