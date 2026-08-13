# NOTE: Completely isolated PNG to SVG processor - does not affect production
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive

# NOTE: Configuration for file storage
ENV STORE_FILES_PERMANENTLY=true

WORKDIR /app

# Install system dependencies for PNG to SVG conversion
# NOTE: Only installing what we need for image processing and OCR
RUN apt-get update \
  && apt-get install -y --no-install-recommends \
  imagemagick \
  potrace \
  cargo \
  curl \
  wget \
  nodejs \
  npm \
  libgl1 \
  libglib2.0-0 \
  libgomp1 \
  libxcb1 \
  libx11-6 \
  libxrender1 \
  libxext6 \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

# Install VTracer
RUN cargo install vtracer && \
  chmod 755 /root/.cargo/bin/vtracer && \
  cp /root/.cargo/bin/vtracer /usr/local/bin/vtracer && \
  chmod 755 /usr/local/bin/vtracer

# NOTE: Install SVGO v3+ for modern SVG optimization
# Version 3.0.2 uses a config-file based approach (see svgo.config.js)
# This is a breaking change from v1/v2 which used --enable= flags
RUN npm install -g svgo@3.0.2

# NOTE: Install svgcleaner for additional optimization
RUN wget -q https://github.com/RazrFalcon/svgcleaner/releases/download/v0.9.5/svgcleaner_linux_x86_64_0.9.5.tar.gz \
  && tar -xzf svgcleaner_linux_x86_64_0.9.5.tar.gz \
  && mv svgcleaner /usr/local/bin/ \
  && chmod +x /usr/local/bin/svgcleaner \
  && rm svgcleaner_linux_x86_64_0.9.5.tar.gz

# NOTE: Copy custom ImageMagick policy for large PNG processing
COPY policy.xml /tmp/policy.xml

# NOTE: Configure ImageMagick security policies for image processing
# Replace the default policy with our custom one that handles large PNG files
RUN POLICY_FILE="" && \
  for path in /etc/ImageMagick-6/policy.xml /etc/ImageMagick/policy.xml /usr/local/etc/ImageMagick-7/policy.xml; do \
  if [ -f "$path" ]; then \
  POLICY_FILE="$path"; \
  break; \
  fi; \
  done && \
  if [ -n "$POLICY_FILE" ]; then \
  echo "Found ImageMagick policy at: $POLICY_FILE" && \
  cp /tmp/policy.xml "$POLICY_FILE" && \
  echo "Updated ImageMagick policy for large PNG processing"; \
  else \
  echo "ImageMagick policy file not found, creating new one" && \
  mkdir -p /etc/ImageMagick-7 && \
  cp /tmp/policy.xml /etc/ImageMagick-7/policy.xml; \
  fi && \
  rm /tmp/policy.xml

# Create directories for processing
RUN mkdir -p /home/sclera/images/png_floors /home/sclera/images/svg_floors

# Copy requirements and install Python dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download RapidOCR models to avoid runtime permission and download latency issues
RUN python -c "from rapidocr import RapidOCR, OCRVersion, EngineType, ModelType, LangDet, LangCls, LangRec; \
RapidOCR(params={ \
    'Det.ocr_version': OCRVersion.PPOCRV5, \
    'Det.engine_type': EngineType.ONNXRUNTIME, \
    'Det.model_type': ModelType.MOBILE, \
    'Det.lang_type': LangDet.CH, \
    'Cls.ocr_version': OCRVersion.PPOCRV5, \
    'Cls.engine_type': EngineType.ONNXRUNTIME, \
    'Cls.model_type': ModelType.MOBILE, \
    'Cls.lang_type': LangCls.CH, \
    'Rec.ocr_version': OCRVersion.PPOCRV5, \
    'Rec.engine_type': EngineType.ONNXRUNTIME, \
    'Rec.model_type': ModelType.MOBILE, \
    'Rec.lang_type': LangRec.EN, \
})"

# Copy SVGO v3+ configuration
COPY svgo.config.js /app/

# Copy your processing script
COPY main.py /app/
COPY utils /app/utils

# Create non-root user for security and grant ownership of app, sclera, and rapidocr packages
RUN groupadd -r processor && useradd -r -g processor processor \
  && chown -R processor:processor /app /home/sclera /usr/local/lib/python3.11/site-packages/rapidocr

USER processor

# NOTE: Default command runs the processing script
ENTRYPOINT ["python", "main.py"]
