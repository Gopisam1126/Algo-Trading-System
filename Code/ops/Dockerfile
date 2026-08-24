# ===========================================================================
# Multi-stage build.  The runtime image runs as a non-root user with a
# read-only application directory — see LOW_LEVEL_ARCHITECTURE.md §10.5.
# ===========================================================================

# --- Stage 1: build the application venv -----------------------------------
#
# 3.11 matches the version CI tests. Ship what is tested: an image on 3.12 while
# the suite runs on 3.11 means the artifact is not the thing that was verified.
# Bumping this requires bumping the CI matrix first.
FROM python:3.11-slim-bookworm AS builder

# NOTE: TA-Lib is NOT built from source here any more.
#
# This stage used to download ta-lib 0.4.0 and compile it. **That build no
# longer works** — 0.4.0's `gen_code.c` predates modern GCC's stricter C rules
# and fails with "Error 1" partway through `make`, so the image could not be
# built at all. It was not slow; it was broken, and nothing was checking.
#
# It is also unnecessary. TA-Lib has published manylinux wheels since 0.6
# (verified against PyPI: 0.7.1 ships 12 of them), and a manylinux wheel must
# vendor its own shared objects to qualify as one — so pip alone now yields a
# self-contained install. That removes a compiler toolchain, a download from
# SourceForge, and several minutes from every build.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md constraints.txt ./
COPY src ./src
# `-c constraints.txt` is what makes this image reproducible. pyproject declares
# floors (`>=`), so without it this build resolves dependencies independently of
# the CI run that verified the code — the artifact would not be the thing that
# was tested, and a breaking upstream release would land silently.
RUN python -m venv /opt/venv \
    # setuptools and wheel too — an outdated build backend is a real, if
    # low, supply-chain exposure and the dependency audit flags it.
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install --no-cache-dir -c constraints.txt . \
    # B7 / CVE-2020-35678 — see pyproject.toml. kiteconnect's autobahn pin is
    # replaced after resolution rather than constrained during it.
    && /opt/venv/bin/pip install --no-cache-dir --no-deps --upgrade "autobahn==26.7.1"

# --- Stage 2: runtime ------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 algotrader \
    && useradd --system --uid 1001 --gid algotrader --create-home algotrader

# The venv only. TA-Lib's shared objects are vendored INSIDE the manylinux
# wheel, so there is no system libta_lib to copy across any more.
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
