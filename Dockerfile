# GE Connect Technical Assistant — HuggingFace Spaces Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for PyMuPDF
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pin the model cache to a known path inside the image layer
ENV SENTENCE_TRANSFORMERS_HOME=/app/.cache/sentence_transformers
ENV HF_HOME=/app/.cache/huggingface

# Pre-cache both embedding models into this image layer.
# Dual-encoder retrieval requires:
#   - all-MiniLM-L6-v2  (384-dim, text encoder for text-only chunks + query)
#   - clip-ViT-B-32      (512-dim, image + text encoder for image chunks)
# HF_HUB_OFFLINE is temporarily unset during build so downloads succeed;
# at runtime it is set to 1 to prevent any DNS lookups.
RUN HF_HUB_OFFLINE=0 python -c "\
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('all-MiniLM-L6-v2'); \
SentenceTransformer('clip-ViT-B-32'); \
print('Both models cached.')"

# Prevent all HF/transformers network calls at runtime.
# HF_HUB_OFFLINE covers huggingface_hub; TRANSFORMERS_OFFLINE covers the
# transformers library that sentence-transformers uses internally for CLIP.
# Both must be set — either one alone can still trigger DNS lookups.
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1

# Copy all application files (vector_store, chunk_images.zip, templates, etc.)
COPY . .

# HF Spaces exposes port 7860 by default
EXPOSE 7860

# gunicorn with gthread workers properly handles long-lived SSE streaming
# behind HF Spaces' HTTP/2 proxy (Flask dev server does not).
# No --preload so gunicorn binds port 7860 immediately and HF health check passes
# while workers load the model in the background.
CMD ["gunicorn", \
     "--worker-class", "gthread", \
     "--workers", "1", \
     "--threads", "4", \
     "--timeout", "300", \
     "--bind", "0.0.0.0:7860", \
     "app:application"]
