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
# NOTE: Only installing what we need for image processing
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        imagemagick \
        potrace \
        curl \
        wget \
        nodejs \
        npm \
        && apt-get clean \
        && rm -rf /var/lib/apt/lists/*

# NOTE: Install SVG optimization tools
RUN npm install -g svgo@3.0.2

# NOTE: Install svgcleaner for additional optimization
RUN wget -q https://github.com/RazrFalcon/svgcleaner/releases/download/v0.9.5/svgcleaner_linux_x86_64_0.9.5.tar.gz \
    && tar -xzf svgcleaner_linux_x86_64_0.9.5.tar.gz \
    && mv svgcleaner /usr/local/bin/ \
    && chmod +x /usr/local/bin/svgcleaner \
    && rm svgcleaner_linux_x86_64_0.9.5.tar.gz

# NOTE: Configure ImageMagick security policies for image processing
RUN POLICY_FILE="" && \
    for path in /etc/ImageMagick-6/policy.xml /etc/ImageMagick/policy.xml /usr/local/etc/ImageMagick-6/policy.xml; do \
        if [ -f "$path" ]; then \
            POLICY_FILE="$path"; \
            break; \
        fi; \
    done && \
    if [ -n "$POLICY_FILE" ]; then \
        sed -i 's/<policy domain="resource" name="memory" value="[^"]*"/<policy domain="resource" name="memory" value="2GiB"/g' "$POLICY_FILE" && \
        sed -i 's/<policy domain="resource" name="map" value="[^"]*"/<policy domain="resource" name="map" value="2GiB"/g' "$POLICY_FILE" && \
        sed -i 's/<policy domain="resource" name="area" value="[^"]*"/<policy domain="resource" name="area" value="64MP"/g' "$POLICY_FILE" && \
        sed -i 's/<policy domain="resource" name="disk" value="[^"]*"/<policy domain="resource" name="disk" value="2GiB"/g' "$POLICY_FILE"; \
    fi

# Create directories for processing
RUN mkdir -p /home/sclera/images/png_floors /home/sclera/images/svg_floors

# Copy your processing script
COPY main.py /app/

# Create non-root user for security
RUN groupadd -r processor && useradd -r -g processor processor \
    && chown -R processor:processor /app /home/sclera

USER processor

# NOTE: Default command runs the processing script
CMD ["python", "main.py"]