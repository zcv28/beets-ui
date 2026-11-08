# syntax=docker/dockerfile:1.7
ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS build
# beets-vocadb (VocaDB / UtaiteDB / TouhouDB metadata source) is not on PyPI,
# so it is installed from a pinned commit.
ARG BEETS_VOCADB_REF=f958bba10a35380e4b926d6582bbf39cf31f0edd
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
RUN python -m venv /venv
ENV PATH=/venv/bin:$PATH PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
# Packages from EXTRA_PIP_PACKAGES live in /config/beets-ui/pip. The .pth file
# appends that folder to sys.path *after* the image's own packages, so plugins
# can never shadow the bundled beets.
RUN pip install . "beets-vocadb @ git+https://github.com/prTopi/beets-vocadb@${BEETS_VOCADB_REF}" \
 && pip freeze | grep -v ' @ ' > /venv/constraints.txt \
 && echo "/config/beets-ui/pip" > "$(python -c 'import site; print(site.getsitepackages()[0])')/zz-beets-ui-extra.pth"

FROM python:${PYTHON_VERSION}-slim
LABEL org.opencontainers.image.title="beets-ui" \
      org.opencontainers.image.description="A lightweight web frontend for the beets music library manager" \
      org.opencontainers.image.source="https://github.com/zcv28/beets-ui" \
      org.opencontainers.image.licenses="MIT"
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libchromaprint-tools tini \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /venv /venv
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY docker/config.default.yaml /usr/local/share/beets-ui/config.default.yaml
ENV PATH=/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/config \
    BEETSDIR=/config/beets \
    BEETSUI_DATA=/config/beets-ui \
    BEETSUI_INBOX=/inbox \
    BEETSUI_PORT=8337
RUN chmod 0755 /usr/local/bin/entrypoint.sh \
 && mkdir -p /config /music /inbox \
 && chmod 0777 /config
VOLUME ["/config"]
EXPOSE 8337
USER 1000:1000
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('BEETSUI_PORT','8337'), timeout=4)"
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["beets-ui"]
