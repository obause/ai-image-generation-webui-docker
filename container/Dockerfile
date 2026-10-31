# InvokeAI 6.14.2 CUDA image: torch 2.7.1+cu128 supports RTX 5090.
# OCI config checked: ENTRYPOINT /opt/invokeai/docker-entrypoint.sh, CMD invokeai-web.
FROM --platform=$BUILDPLATFORM mcr.microsoft.com/dotnet/sdk:8.0-noble AS swarm-build
ARG SWARMUI_COMMIT=165cd29bcbdc26f72e7874441cee8e74b9874cb7
RUN git init /opt/SwarmUI \
    && cd /opt/SwarmUI \
    && git remote add origin https://github.com/mcmonkeyprojects/SwarmUI.git \
    && git fetch --depth 1 origin "${SWARMUI_COMMIT}" \
    && git checkout --detach FETCH_HEAD \
    && dotnet build src/SwarmUI.csproj --configuration Release \
        -p:UseAppHost=false -o ./src/bin/live_release \
    && git rev-parse HEAD > ./src/bin/last_build \
    && rm -rf .git src/obj src/bin/Release

FROM mcr.microsoft.com/dotnet/aspnet:8.0-noble AS dotnet-runtime

FROM ubuntu:24.04 AS webtools
ADD --checksum=sha256:614877cf71005f14ea252bd2d9b1c12237cf7d73681ef150f0dff7f13849fdb5 https://github.com/coder/code-server/releases/download/v4.143.1/code-server-4.143.1-linux-amd64.tar.gz /tmp/code-server.tar.gz
ADD --checksum=sha256:5710dc6f51d77f1db884eace44f6952286c49f2ce40b6a59af37e0dc7c4a8bbd https://github.com/gtsteffaniak/filebrowser/releases/download/v2.0.2-stable/linux-amd64-filebrowser /usr/local/bin/filebrowser
ADD --checksum=sha256:b66d11eca8cacbf18a2de6943c6e18a79ffbdc8a3c30aa13a49dc6cfc15d4239 https://github.com/runpod/runpodctl/releases/download/v3.0.1/runpodctl-linux-amd64 /usr/local/bin/runpodctl
ADD --checksum=sha256:efbfe852181f7191eb3c9043ed1ab49c9b2d0ba045c61c926a5da111c939ec5c https://github.com/rclone/rclone/releases/download/v1.75.2/rclone-v1.75.2-linux-amd64.deb /tmp/rclone.deb
RUN mkdir -p /opt/code-server \
    && tar -xzf /tmp/code-server.tar.gz -C /opt/code-server --strip-components=1 \
    && chmod 755 /usr/local/bin/filebrowser /usr/local/bin/runpodctl

FROM ghcr.io/invoke-ai/invokeai@sha256:3b24a0dd5fae20c2712e98cca9618ed51adf0ffad6799f0b182f72df236779be

USER root
COPY --from=dotnet-runtime /usr/share/dotnet /usr/share/dotnet
COPY --from=swarm-build /opt/SwarmUI /opt/SwarmUI
COPY --from=webtools /opt/code-server /opt/code-server
COPY --from=webtools /usr/local/bin/filebrowser /usr/local/bin/filebrowser
COPY --from=webtools /usr/local/bin/runpodctl /usr/local/bin/runpodctl
COPY --from=webtools /tmp/rclone.deb /tmp/rclone.deb
RUN export DEBIAN_FRONTEND=noninteractive \
    && ln -s /usr/share/dotnet/dotnet /usr/local/bin/dotnet \
    && apt-get update \
    && apt-get install -y --no-install-recommends git openssh-server ca-certificates nginx openssl ffmpeg \
    && dpkg -i /tmp/rclone.deb \
    && rm -f /tmp/rclone.deb \
    && rm -f /etc/ssh/ssh_host_* \
    && rm -rf /var/lib/apt/lists/*

# Build ComfyUI once in the image. The InvokeAI venv already has cu128 torch.
ARG COMFYUI_COMMIT=426f5eea52a7ece1dd599fff4cb6cd1f352ddd24
RUN git init /opt/ComfyUI \
    && cd /opt/ComfyUI \
    && git remote add origin https://github.com/comfyanonymous/ComfyUI.git \
    && git fetch --depth 1 origin "${COMFYUI_COMMIT}" \
    && git checkout --detach FETCH_HEAD \
    && uv pip install --python /opt/venv/bin/python -r requirements.txt \
    && /opt/venv/bin/python -c 'import comfy, invokeai, torch; assert torch.version.cuda == "12.8"' \
    && rm -rf output user \
    && ln -s /workspace/output/comfyui output \
    && ln -s /workspace/comfy-user user

# SwarmUI's compiled .NET IL is portable; the target image supplies amd64 runtime.
RUN rm -rf /opt/SwarmUI/Models /opt/SwarmUI/Output \
    && ln -s /workspace/models /opt/SwarmUI/Models \
    && ln -s /workspace/output /opt/SwarmUI/Output \
    && chown -R ubuntu:ubuntu /opt/SwarmUI /opt/ComfyUI

COPY container/extra_model_paths.yaml /opt/ComfyUI/extra_model_paths.yaml
COPY --chmod=755 container/entrypoint.sh /opt/runpod-bootstrap/entrypoint.sh
COPY --chmod=755 container/start-comfy.sh /opt/runpod-bootstrap/start-comfy.sh
COPY --chmod=755 container/start-swarm.sh /opt/runpod-bootstrap/start-swarm.sh
COPY --chmod=755 container/start-web-tools.sh /opt/runpod-bootstrap/start-web-tools.sh
COPY container/filebrowser.yaml /opt/runpod-bootstrap/filebrowser.yaml
COPY container/nginx.conf /opt/runpod-bootstrap/nginx.conf
COPY --chmod=755 container/worker.py /opt/runpod-bootstrap/worker.py

ENV INVOKEAI_ROOT=/workspace/invokeai \
    INVOKEAI_MODELS_DIR=/workspace/models/invokeai \
    INVOKEAI_HOST=127.0.0.1 \
    INVOKEAI_PORT=19090

EXPOSE 7801 8188 9090 8443 8081 22
# The wrapper calls the inspected InvokeAI entrypoint only after B2 restore.
ENTRYPOINT ["/opt/runpod-bootstrap/entrypoint.sh"]
CMD ["invokeai-web"]
