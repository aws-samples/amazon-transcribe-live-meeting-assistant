# Use AMD64 platform for compatibility
FROM --platform=linux/amd64 public.ecr.aws/docker/library/python:3.12-slim

# Set working directory
WORKDIR /app

# Set build arguments
ARG TARGETARCH=amd64
ARG BUILDPLATFORM=linux/amd64

# Install system dependencies and security packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    cmake \
    ninja-build \
    ca-certificates \
    tzdata \
    curl \
    # Security packages
    libseccomp-dev \
    libcap2-bin \
    && rm -rf /var/lib/apt/lists/* \
    # Set secure permissions
    && chmod 644 /etc/passwd \
    && chmod 644 /etc/group

# Set platform-specific environment variables and security configurations
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Security hardening
    PYTHON_HASHSEED=random \
    # Logging configuration
    LOG_LEVEL=INFO \
    LOG_FORMAT=json \
    # OpenTelemetry configuration
    OTEL_RESOURCE_ATTRIBUTES=service.name=search_agent,deployment.environment=production \
    OTEL_TRACES_SAMPLER=parentbased_traceidratio \
    OTEL_TRACES_SAMPLER_ARG=1.0 \
    # Resource limits and memory constraints
    MALLOC_ARENA_MAX=2 \
    MALLOC_TRIM_THRESHOLD_=100000 \
    MALLOC_MMAP_THRESHOLD_=100000 \
    PYTHONMALLOC=malloc \
    PYTHONMALLOCSTATS=0 \
    # Garbage collection settings
    PYTHONGC=1 \
    PYTHONDEVMODE=0 \
    # Thread settings
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    RAYON_NUM_THREADS=1

# Set platform-specific optimizations
RUN if [ "$TARGETARCH" = "arm64" ]; then \
        # ARM64-specific optimizations
        echo "export OPENBLAS_NUM_THREADS=1" >> /etc/profile.d/openblas.sh && \
        echo "export GOTO_NUM_THREADS=1" >> /etc/profile.d/openblas.sh && \
        echo "export OMP_NUM_THREADS=1" >> /etc/profile.d/openblas.sh; \
    fi

# Copy requirements first to leverage Docker cache
COPY requirements.txt requirements.txt

# Install Python dependencies with platform-specific optimizations
RUN if [ "$TARGETARCH" = "arm64" ]; then \
        # ARM64-specific installation
        pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu \
            -r requirements.txt \
            aws-opentelemetry-distro>=0.10.0 \
            aws_opentelemetry_distro_genai_beta>=0.1.2; \
    else \
        # AMD64 installation
        pip install --no-cache-dir -r requirements.txt \
            aws-opentelemetry-distro>=0.10.0 \
            aws_opentelemetry_distro_genai_beta>=0.1.2; \
    fi

# Set environment variables
ENV AWS_REGION=us-east-1 \
    AWS_DEFAULT_REGION=us-east-1 \
    DOCKER_CONTAINER=1 \
    PYTHONUNBUFFERED=1

# Copy entire project (respecting .dockerignore)
COPY . .

# Create non-root user with minimal privileges
RUN groupadd -g 1000 agentcore && \
    useradd -u 1000 -g agentcore -s /sbin/nologin -M bedrock_agentcore && \
    # Set up app directory with secure permissions
    mkdir -p /app/logs /app/data && \
    chown -R bedrock_agentcore:agentcore /app && \
    chmod -R 755 /app && \
    # Add specific capabilities instead of running as root
    setcap cap_net_bind_service=+ep /usr/local/bin/python3.12

# Switch to non-root user
USER bedrock_agentcore

# Expose ports (8000 for AgentCore, 8080 for health checks)
EXPOSE 8000 8080

# Set up healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Set security options and resource limits
STOPSIGNAL SIGTERM

# Resource limits (these will be enforced by Docker at runtime)
ENV MEMORY_LIMIT=2g \
    CPU_LIMIT=2

# Resource limit labels (for Docker to enforce)
LABEL com.docker.container.memory.limit="2g" \
      com.docker.container.memory.reservation="1.5g" \
      com.docker.container.cpu.limit="2" \
      com.docker.container.cpu.reservation="1" \
      com.docker.container.ulimits.nofile="65536:65536" \
      com.docker.container.ulimits.nproc="32768:32768"

# Start the agent with OpenTelemetry instrumentation and proper signal handling
CMD ["opentelemetry-instrument", "python", "-m", "search_agent"]
