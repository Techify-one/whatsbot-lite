FROM python:3.11-slim

# Empty by default: the version comes from the tracked GOWA_VERSION file so
# there is a single source of truth shared with the launchers and the panel.
# Override at build time with --build-arg GOWA_VERSION=x.y.z.
ARG GOWA_VERSION=
ARG TARGETARCH=amd64

ENV WHATSBOT_DOCKER=1
ENV PYTHONUNBUFFERED=1

# Install curl and unzip for downloading GOWA
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl unzip && \
    rm -rf /var/lib/apt/lists/*

# Single source of truth for the bundled GOWA version
COPY GOWA_VERSION /tmp/GOWA_VERSION

# Download and install GOWA binary for Linux
RUN V="${GOWA_VERSION:-$(cat /tmp/GOWA_VERSION)}" && \
    curl -fsSL "https://github.com/aldinokemal/go-whatsapp-web-multidevice/releases/download/v${V}/whatsapp_${V}_linux_${TARGETARCH}.zip" \
        -o /tmp/gowa.zip && \
    unzip /tmp/gowa.zip -d /tmp/gowa && \
    cp /tmp/gowa/linux-${TARGETARCH} /usr/local/bin/gowa && \
    chmod +x /usr/local/bin/gowa && \
    rm -rf /tmp/gowa /tmp/gowa.zip && \
    echo "$V" > /tmp/GOWA_INSTALLED

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code only
COPY agent/ agent/
COPY assets/ assets/
COPY config/ config/
COPY gowa/ gowa/
COPY db/ db/
COPY plugins/ plugins/
COPY portability/ portability/
COPY server/ server/
COPY web/ web/
COPY main.py alembic.ini GOWA_VERSION WHATSBOT_VERSION AGENTS.md CLAUDE.md README.md ./

# Create bin/gowa symlink so gowa/manager.py finds the binary at expected path,
# plus the stamp that tells the updater which version this image shipped.
RUN mkdir -p bin && ln -s /usr/local/bin/gowa bin/gowa && \
    cp /tmp/GOWA_INSTALLED bin/.gowa_stamp

# Create runtime directories and declare as volumes for persistence
RUN mkdir -p logs storages statics
VOLUME ["/app/storages", "/app/statics", "/app/logs"]

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=5 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["python", "main.py"]
