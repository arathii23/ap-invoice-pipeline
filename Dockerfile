FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libgl1 libglib2.0-0 libgomp1 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Render injects PORT at runtime; 8765 is the local default. The container serves the public demo: every visitor gets a private, auto-expiring
# workspace (AP_WORKSPACES=1), so "Clear history" can only ever clear their own. API keys are supplied at run time, never baked into the image.
ENV HOST=0.0.0.0 PORT=8765 PYTHONUNBUFFERED=1 AP_WORKSPACES=1
EXPOSE 8765
CMD ["python", "server.py"]
