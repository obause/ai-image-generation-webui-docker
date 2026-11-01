# Plan (verified 2026-11-01)

## RunPod capabilities

- Use [REST API v2 OpenAPI](https://api.runpod.io/v2/openapi.json) at `https://api.runpod.io/v2`. It provides Pod create/list/get/delete, public or owned template lookup, GPU availability, SSH endpoints, account SSH keys, account secrets, and Pod logs. A create request may specify `templateId` and override its `image`; it accepts `gpu.id`, `gpu.count`, `disk`, `ports`, `cloud`, `dataCenterIds`, `env`, and `startSsh`.
- Account secrets are write-only and may be referenced in Pod environment variables with `{{ RUNPOD_SECRET_name }}`. The CLI uploads a destination-bucket-only B2 key through HTTPS and passes only references in the Pod create request. The broader local key used for one-time migration is never sent to RunPod.
- REST v2 has **no** `terminateAfter`/`stopAfter` field. [RunPod's current CLI guidance](https://github.com/runpod/runpod-plugins-official/blob/main/plugins/runpod/skills/runpodctl/SKILL.md) says older `runpodctl` flags were never enforced and were removed in v2.12.0. A Pod-side deadline will attempt a final backup and self-delete using pinned `runpodctl` v3.0.1 and its Pod-scoped identity. If backup fails it stays alive to protect data; thus the deadline is not a hard billing cap.
- `disk` is ephemeral container storage. The API's `mounts.persistent` is host-local persistent storage and is intentionally omitted. There is no separate ephemeral `workspace_disk_gb` field in v2.

## Architecture

The local `rp` CLI calls REST v2; B2 is the source of truth. The user selected RTX 5090 with SwarmUI, ComfyUI and InvokeAI. A pinned wrapper based on official InvokeAI 6.14.2 restores B2 before any application starts, then runs periodic copy backup, ComfyUI, SwarmUI, code-server and FileBrowser Quantum, and finally executes InvokeAI's original entrypoint. `/workspace/models` is the common B2-backed root. SSH uses the direct endpoint returned by `GET /v2/pods/{id}`, since the proxy endpoint is interactive only. A RunPod template ID is optional; direct image creation is supported.

## Inspected upstream image

The former Ashley image `8.12.11` has `ENTRYPOINT ["/opt/nvidia/nvidia_entrypoint.sh"]`, `CMD ["/start.sh"]`, CUDA 12.4 and PyTorch CUDA 12.4 builds. [PyTorch 2.7](https://pytorch.org/blog/pytorch-2-7/) introduced Blackwell support in CUDA 12.8 builds, so that image is unsuitable for the requested RTX 5090. The selected [InvokeAI 6.14.2 CUDA image](https://github.com/invoke-ai/InvokeAI/releases/tag/v6.14.2) uses PyTorch 2.7.1+cu128. Its amd64 manifest digest is `sha256:3b24a0dd5fae20c2712e98cca9618ed51adf0ffad6799f0b182f72df236779be`; OCI config reports `ENTRYPOINT ["/opt/invokeai/docker-entrypoint.sh"]`, `CMD ["invokeai-web"]`, working directory `/opt/invokeai`, and no declared ports or volumes. Its [entrypoint source](https://github.com/invoke-ai/InvokeAI/blob/v6.14.2/docker/docker-entrypoint.sh) configures the runtime directory and drops to an unprivileged user. The wrapper invokes it after B2 restore.

## Assumptions and user-specific information

- The GitHub repository `obause/ai-image-generation-webui-docker` is not the RunPod public template ID. A template ID is optional. The wrapper image tag is `ghcr.io/obause/ai-image-generation-webui-docker:0.1.5`. The requested GPU is `NVIDIA GeForce RTX 5090` on Secure Cloud; 500 GB ephemeral disk is the provisional size. Availability changes, so `rp doctor` checks the live catalog.
- The selected public template may contain environment variables or startup overrides that must be inspected with `rp doctor` and copied into configuration if needed. The direct-image path avoids this dependency.
- SwarmUI is a separate process and seeds its installed state and ComfyUI URL backend on first launch; those settings are then stored in B2. InvokeAI's managed models live under the same `/workspace/models` root but require its own database registration. Civitai downloads use the optional `CIVITAI_API_TOKEN` account secret, converted to InvokeAI's documented `INVOKEAI_REMOTE_API_TOKENS` environment setting.
- The old private B2 bucket contains about 227.5 GB and 126,601 objects. Bucket names and IDs are kept in permission-restricted local files outside Git. A 16-mapping migration copied roughly 187 GB of models, InvokeAI database/output, ComfyUI output/settings, and training images into the new bucket; every mapping passed a one-way hash/size verification. The old bucket was left intact. The old virtual environments, app code, logs, and incomplete Hugging Face cache were omitted.
- Local credentials are read from a permission-restricted file outside Git. `rp doctor` confirms the RunPod and dedicated Pod B2 credentials and registered SSH keys. A B2 key restricted to the destination bucket was created without delete capability and its scope and read access were verified. The one-time migration key remains local.
- [FileBrowser Quantum](https://filebrowserquantum.com/en/docs/getting-started/config/) provides the uploader and image previews; [code-server](https://coder.com/docs/code-server/guide) provides browser VS Code and terminal. Both use a RunPod secret for browser login. SwarmUI has a model browser with editable metadata, tags and previews; no extra catalog service is needed initially.

## Phases

1. Implement validated configuration, REST v2 adapter, atomic locked state, SSH client and Typer lifecycle.
2. Implement pinned wrapper, B2 restore/backup/deadline and SwarmUI launch.
3. Test lifecycle and safety invariants, lint/type-check, and document setup and recovery.

## Validation completed

All 53 pytest cases, Ruff, mypy, shell checks, and the amd64 image build passed locally. A fake-B2 container boot verified restore-before-readiness, HTTP password protection, FileBrowser startup, and SSH-style manual backup without inherited credentials. GitHub Actions published the public `0.1.5` amd64 image and its checks passed.

A paid Secure Cloud RTX 5090 Pod pulled `0.1.5`, restored the model and InvokeAI data before application startup, and reached readiness. During restore, the marker was absent and all three generation app ports were closed. Afterward, the GPU reported 32,607 MiB and compute capability 12.0; 193 model files were present in the shared root. SwarmUI, ComfyUI, InvokeAI, code-server, and FileBrowser responded. The generation proxy endpoints returned HTTP 401 without a password. Periodic backups, `rp sync`, and `rp sync output` completed. `rp down` performed a successful final B2 backup, confirmed Pod deletion, and cleared local state. `rp doctor` passes after teardown.

The Pod-side automatic deadline could not be exercised without waiting eight hours; it remains a best-effort safeguard and is not a guaranteed billing cutoff.
