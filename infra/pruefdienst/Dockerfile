# Build only on the dedicated CI Docker daemons; register the resulting image ID.
# Before building, tag the verified base ID from runtime.json with this local name.
# No registry pull: the operator verifies the local tag's ID immediately before build.
FROM pruefdienst-rclone-base-7ab1d115ae18
USER root
ENV DEBIAN_FRONTEND=noninteractive \
    UV_PYTHON_INSTALL_DIR=/opt/rclone-ci/python \
    PLAYWRIGHT_BROWSERS_PATH=/opt/rclone-ci/browsers
RUN apt-get update && apt-get install --no-install-recommends -y \
      shellcheck systemd rclone unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN curl --fail --location --proto '=https' --tlsv1.2 \
      https://nodejs.org/dist/v24.22.3/node-v24.22.3-linux-x64.tar.xz \
      --output /tmp/rclone-node.tar.xz \
    && echo '4f2b175a6d53a5c617a80dba2676238a0688cef28c6da4eba35c7d844e66fcfb  /tmp/rclone-node.tar.xz' | sha256sum --check - \
    && mkdir -p /opt/rclone-ci/node \
    && tar -xJf /tmp/rclone-node.tar.xz --strip-components=1 -C /opt/rclone-ci/node \
    && rm /tmp/rclone-node.tar.xz
COPY uv-requirements.lock /tmp/rclone-uv-requirements.lock
RUN python3 -m venv /opt/rclone-ci/uv \
    && /opt/rclone-ci/uv/bin/python -m pip install --require-hashes --only-binary=:all: --no-deps -r /tmp/rclone-uv-requirements.lock \
    && /opt/rclone-ci/uv/bin/uv python install 3.10.22 3.11.18 3.12.16 3.13.18 \
    && mkdir -p /opt/rclone-ci/bin \
    && for v in 3.10 3.11 3.12 3.13; do \
         p=$(/opt/rclone-ci/uv/bin/uv python find --managed-python "$v"); \
         ln -s "$p" "/opt/rclone-ci/bin/python$v"; \
       done
RUN curl --fail --location --proto '=https' --tlsv1.2 \
      https://github.com/rclone/rclone/releases/download/v1.74.4/rclone-v1.74.4-linux-amd64.zip \
      --output /tmp/rclone-production.zip \
    && echo 'fe435e0c36228e7c2f116a8701f01127bb1f694005fc11d1f27186c8bca4115d  /tmp/rclone-production.zip' | sha256sum --check - \
    && mkdir -p /opt/rclone-ci/rclone-1.74.4 \
    && unzip -p /tmp/rclone-production.zip rclone-v1.74.4-linux-amd64/rclone > /opt/rclone-ci/rclone-1.74.4/rclone \
    && chmod 755 /opt/rclone-ci/rclone-1.74.4/rclone \
    && rm /tmp/rclone-production.zip
ENV PATH=/opt/rclone-ci/bin:/opt/rclone-ci/node/bin:$PATH
COPY web/package.json web/package-lock.json /opt/rclone-ci/web/
RUN npm ci --prefix /opt/rclone-ci/web --ignore-scripts --no-audit --no-fund \
    && node /opt/rclone-ci/web/node_modules/playwright/cli.js install chromium \
    && groupadd --system rclone-sync \
    && useradd --system --gid rclone-sync --home-dir /home/rclone-sync rclone-sync \
    && install -d /home/rclone-sync/.config/rclone /mnt /media /srv \
    && ln -s /work/repo /opt/rclone-sync \
    && chmod -R a+rX /opt/rclone-ci
COPY controller/ /opt/rclone-ci/controller/
COPY container-entry.sh /opt/pruefdienst/container-entry.sh
RUN chmod -R a+rX /opt/rclone-ci/controller \
    && chmod 755 /opt/pruefdienst/container-entry.sh
USER 1000:1000
