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

# Pre-cache the sentence-transformer model into this image layer
# so cold starts don't require a download from HF Hub
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# Copy all application files (vector_store, chunk_images.zip, templates, etc.)
COPY . .

# HF Spaces exposes port 7860 by default
EXPOSE 7860

# app.py handles zip extraction, vector store load, and Flask startup
CMD ["python", "app.py"]
