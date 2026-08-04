# ===========================================================================
# Multi-stage build.  The runtime image runs as a non-root user with a
# read-only application directory — see LOW_LEVEL_ARCHITECTURE.md §10.5.
# ===========================================================================

# --- Stage 1: build TA-Lib's C library and Python wheels -------------------
FROM python:3.12-slim-bookworm AS builder

ARG TALIB_VERSION=0.4.0

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# TA-Lib ships as a C library; the Python package is a binding over it.
RUN wget -q "https://downloads.sourceforge.net/project/ta-lib/ta-lib/${TALIB_VERSION}/ta-lib-${TALIB_VERSION}-src.tar.gz" \
    && tar -xzf "ta-lib-${TALIB_VERSION}-src.tar.gz" \
    && cd ta-lib \
    && ./configure --prefix=/usr \
    && make -j"$(nproc)" \
    && make install \
    && cd .. && rm -rf ta-lib*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir .

# --- Stage 2: runtime ------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 algotrader \
    && useradd --system --uid 1001 --gid algotrader --create-home algotrader

# TA-Lib shared objects from the build stage
COPY --from=builder /usr/lib/libta_lib* /usr/lib/
COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=random \
    TZ=Asia/Kolkata

WORKDIR /app
COPY --chown=algotrader:algotrader src ./src
COPY --chown=algotrader:algotrader config ./config

RUN mkdir -p /app/data && chown -R algotrader:algotrader /app/data

USER algotrader

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import algotrader; print('ok')" || exit 1

CMD ["python", "-m", "algotrader.orchestrator"]
