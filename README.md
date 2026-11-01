# RunPod Workspace

`rp` runs a disposable RTX 5090 workspace backed by Backblaze B2. B2 holds persistent models, image history, projects, uploads, app settings, and outputs. The Pod and its `/workspace` disk can be deleted and reconstructed. No RunPod Network Volume or permanent host volume is used.

The container starts in this order:

```text
B2 connectivity check → rclone restore → readiness marker
                      → periodic backup + ComfyUI + SwarmUI + web tools
                      → original InvokeAI entrypoint → invokeai-web
```

This ordering prevents the applications from downloading missing files before the B2 restore. A failed required restore blocks every application process and leaves the Pod for debugging. The wrapper preserves InvokeAI's inspected entrypoint behavior after bootstrap, including its user switch and root setup.

## Current RunPod interfaces

The implementation targets [RunPod REST API v2](https://api.runpod.io/v2/openapi.json). It uses `POST /v2/pods` with an explicit wrapper `image`, GPU, disk, ports and entrypoint override. A public `templateId` is optional; creating from the image directly avoids needing a RunPod template ID. The API also exposes Pod listing/get/delete, templates, GPU availability, direct SSH endpoints, account secrets and registered SSH keys. The [RunPod CLI reference](https://github.com/runpod/runpodctl/blob/main/docs/runpodctl_pod_create.md) still describes Pod creation, but this project does not parse CLI text or put credentials in process arguments.

**Automatic termination caveat:** RunPod's current REST v2 schema has no Pod deadline. [Current RunPod CLI guidance](https://github.com/runpod/runpod-plugins-official/blob/main/plugins/runpod/skills/runpodctl/SKILL.md) says the old `--terminate-after` flag was never enforced and was removed. `terminate_after` sets a Pod-side deadline measured from creation. At the deadline the watchdog performs a final backup, then uses the pinned `runpodctl pod delete` with the Pod-scoped identity to remove its own Pod. If backup or deletion fails it logs the failure and retries every five minutes. This protects data, but is **not a guaranteed billing cutoff**. Use `rp status` and `rp down` to verify termination. No account-wide RunPod API key is sent to the Pod.

The v2 `disk` field is ephemeral container disk. It includes `/workspace`; it is wiped on Pod restart. v2 `mounts.persistent` is host-local persistent disk, so this project deliberately omits it. There is no separate ephemeral workspace-disk field.

## Installation

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), Docker for building the wrapper, OpenSSH, and a RunPod account with an SSH public key registered. The local CLI talks directly to REST v2, so local `runpodctl` is optional.

```bash
uv sync
uv run rp --help
```

Create a private local credentials file outside the repository. `rp` and the B2 helper scripts load it automatically; environment variables, when present, take precedence. Use raw `KEY=VALUE` lines without `export` or shell quoting. Enter values in your editor so they never appear in shell history:

```bash
mkdir -p ~/.config/runpod-workspace
chmod 700 ~/.config/runpod-workspace
umask 077
nano ~/.config/runpod-workspace/secrets.env
chmod 600 ~/.config/runpod-workspace/secrets.env
```

Use these lines in `secrets.env`, replacing the placeholders locally:

```text
RUNPOD_API_KEY=<RUNPOD_API_KEY>
B2_APPLICATION_KEY_ID=<B2_APPLICATION_KEY_ID>
B2_APPLICATION_KEY=<B2_APPLICATION_KEY>
B2_POD_APPLICATION_KEY_ID=<NEW_BUCKET_ONLY_KEY_ID>
B2_POD_APPLICATION_KEY=<NEW_BUCKET_ONLY_KEY>
WORKSPACE_WEB_PASSWORD=<LONG_UNIQUE_PASSWORD>
CIVITAI_API_TOKEN=<OPTIONAL_CIVITAI_API_TOKEN>
```

Omit the Civitai line if you do not yet have a token. The loader refuses group/world-readable files and never executes file contents as shell code. Do not paste this file or its values into chat or commit them. You can also supply these variables from a password manager instead of a file.

`B2_APPLICATION_KEY_ID` and `B2_APPLICATION_KEY` are used only by the local inspection and migration scripts. For initial migration, use a key limited to the old and new bucket IDs, with `listBuckets`, `listFiles`, `readFiles`, and `writeFiles`, but no `deleteFiles`. Some bucket-restricted rclone setups also require `listAllBucketNames`. One rclone B2 remote can copy between buckets in the same account without transferring the entire collection through this Mac. `B2_POD_APPLICATION_KEY_ID` and `B2_POD_APPLICATION_KEY` must be a separate destination-bucket-only key with read/list/write capabilities and no delete capability. The CLI only uploads this restricted key to RunPod. Backblaze documents [multi-bucket keys](https://www.backblaze.com/docs/cloud-storage-application-keys), [B2 key capabilities](https://www.backblaze.com/docs/cloud-storage-application-key-capabilities), and [rclone setup](https://www.backblaze.com/docs/cloud-storage-integrate-rclone-with-backblaze-b2). Do not send the B2 master key to a Pod.

In [RunPod Credentials](https://docs.runpod.io/get-started/credentials), create a Restricted API key with `pods:read`, `pods:write`, `secrets:read`, `secrets:write`, `catalog:read`, and `authentication:read`; add `templates:read` only if you configure a template ID. These are the scopes used by the current [REST v2 OpenAPI](https://api.runpod.io/v2/openapi.json) endpoints in this project. Register a local SSH public key in the same Credentials page and keep its private key available to your SSH agent. `rp doctor` checks that the agent selected by SSH config exposes a matching public key; a locked key manager may still require approval when SSH signs a connection. GitHub access on this Mac is already configured for `obause`.

`rp up` sends only the restricted Pod B2 key, the web password, and the optional Civitai token via HTTPS to [RunPod account secrets](https://api.runpod.io/v2/openapi.json); the Pod create request includes only `{{ RUNPOD_SECRET_name }}` references. RunPod substitutes them inside the Pod. The wrapper converts the Civitai token to InvokeAI's `INVOKEAI_REMOTE_API_TOKENS` setting in memory, matching Civitai download URLs. The web password protects code-server and FileBrowser Quantum; an ephemeral hash also protects the public InvokeAI, ComfyUI, and SwarmUI proxy ports with HTTP Basic authentication (username `workspace`). The generation apps bind to localhost behind nginx. Secret values are never stored in project config or local state, and API errors do not echo response bodies. The Pod's environment necessarily contains these credentials while it runs, so restrict Pod access and avoid putting them in app logs.

Secret changes take effect on the next newly created Pod. `rp up` reuses an existing managed Pod and does not rotate its running environment.

Copy the example into the default config location, or pass `--config` before a subcommand:

```bash
mkdir -p ~/.config/runpod-workspace
cp config/config.example.yaml ~/.config/runpod-workspace/config.yaml
rp --config ./my-config.yaml doctor
```

The example config points to `ghcr.io/obause/ai-image-generation-webui-docker:0.1.2`. Publish a release with that tag to run the [release workflow](.github/workflows/release-image.yml), which builds the amd64 image and publishes it to GitHub Container Registry using the repository's `GITHUB_TOKEN`; no personal registry token is needed in the workflow. Verify the resulting package is public so RunPod can pull it without registry credentials. For a manual build, use:

```bash
docker buildx build --platform linux/amd64 -f container/Dockerfile \
  -t 'ghcr.io/obause/ai-image-generation-webui-docker:0.1.2' --push .
```

The wrapper's base is pinned to the amd64 digest of the [official InvokeAI 6.14.2 CUDA image](https://github.com/invoke-ai/InvokeAI/pkgs/container/invokeai). Its OCI config has `ENTRYPOINT ["/opt/invokeai/docker-entrypoint.sh"]`, `CMD ["invokeai-web"]`, working directory `/opt/invokeai`, and no declared exposed ports or volumes. InvokeAI 6.14.2 uses PyTorch 2.7.1 with CUDA 12.8, which [supports Blackwell GPUs](https://pytorch.org/blog/pytorch-2-7/). ComfyUI and SwarmUI are built from pinned source commits into the wrapper. [rclone 1.75.2](https://rclone.org/downloads/), [code-server](https://github.com/coder/code-server/releases/tag/v4.143.1), and [FileBrowser Quantum](https://github.com/gtsteffaniak/filebrowser/releases/tag/v2.0.2-stable) are added from versioned release artifacts with checked SHA-256 digests. The sample exposes InvokeAI on 9090, ComfyUI on 8188, SwarmUI on 7801, code-server on 8443, FileBrowser on 8081, and SSH on 22.

SwarmUI's pinned source still depends on ImageSharp 3.1.12, which [has published security advisories](https://github.com/SixLabors/ImageSharp/security/advisories). A trial upgrade to 3.2.0 failed because that release requires a Six Labors build license. The SwarmUI endpoint is protected by the nginx password gate; review the upstream dependency and licensing before widening access or accepting untrusted uploads there.

## Initial setup

1. A RunPod template ID is optional. The GitHub repository URL and GHCR image tag are not template IDs. If you do use a public template, obtain its actual ID from the [RunPod template catalog](https://docs.runpod.io/pods/templates/overview); `rp doctor` fetches it and prints its image. Check its ports, env key names and startup overrides; copy any required nonsecret settings into the wrapper/config. The wrapper image supplies its own preserved startup behavior even when no template is selected.
2. The sample requests `NVIDIA GeForce RTX 5090` on Secure Cloud. Confirm the exact ID and availability with `rp doctor` or the [GPU catalog](https://docs.runpod.io/references/gpu-types). `rp` never substitutes another GPU. Secure Cloud was selected because Community Cloud currently has no 5090 availability; availability changes over time.
3. Set `disk_gb` large enough for the base image, your 200–300 GB model collection, image history, new outputs, and free space. The sample uses 500 GB as a starting point; measure the actual image collection before provisioning. B2 transfer and local disk capacity determine first-start time. `rp up` waits up to `wait_timeout_seconds` and keeps a timed-out Pod alive.
4. Set your dedicated B2 destination bucket in the private local `config.yaml`. With an empty `storage.prefix`, mappings begin at the bucket root. The sample restores `models` into `/workspace/models` before any app starts. ComfyUI reads typed folders such as `checkpoints`, `loras`, and `vae`; SwarmUI's `Models` link points to the same root. InvokeAI installs its own managed models under `/workspace/models/invokeai`, so all weights live under one B2-backed root. InvokeAI keeps a model database under `/workspace/invokeai`; that directory is also backed up. To use an existing file from the common folders in InvokeAI, scan its path in InvokeAI's Model Manager and choose in-place install. InvokeAI's managed formats are not automatically selectable in ComfyUI, so some model registration remains application-specific.
5. SwarmUI is seeded as installed with a “ComfyUI API By URL” backend at `http://127.0.0.1:18188` (the internal port); it starts after ComfyUI is ready. Existing B2-backed SwarmUI settings and backend files are preserved. SwarmUI's `/workspace/swarmui-data` is copied to B2 periodically and restored on later Pods. InvokeAI is available through authenticated port 9090. `CIVITAI_API_TOKEN` in the private local secrets file enables authenticated Civitai URLs in InvokeAI without entering the token in YAML. You can also configure its marketplace key through the InvokeAI UI, but that would persist in InvokeAI's B2-backed data.
6. Open code-server at port 8443 for browser VS Code, terminal, and its built-in file upload. Open FileBrowser Quantum at 8081 as `admin` with `WORKSPACE_WEB_PASSWORD` for uploads, downloads, file management, and image previews; FFmpeg is included for video previews. Its sources include `models`, `images`, `output`, `invokeai-gallery`, `projects`, and `uploads`. Its local SQLite database is recreated per Pod, while the files it manages are backed up. [SwarmUI's model browser](https://github.com/mcmonkeyprojects/SwarmUI/blob/master/docs/APIRoutes/ModelsAPI.md) already supports model titles, descriptions, tags, preview images, and sidecar metadata, so a second model database is unnecessary. Its image gallery covers SwarmUI outputs; FileBrowser covers legacy images and output from every app. The three generation UI endpoints prompt for username `workspace` and the same web password.

### Migrating the old B2 volume backup

The legacy backup contains a large model and image collection; the new destination bucket starts empty. The old bucket remains untouched. Install `rclone` locally if needed. After filling in `secrets.env`, inspect the old layout with the read-only helper:

```bash
uv run python scripts/inspect_b2.py '<LEGACY_BUCKET>' --max-depth 3
```

The observed backup has ComfyUI models, older WebUI models, InvokeAI managed models and database, about 21 GB of InvokeAI output images, and smaller ComfyUI output/training collections. The checked [example manifest](config/migration.example.yaml) maps these into one model root and persistent image folders. It excludes the old virtual environments, app installations, logs, and an incomplete Hugging Face cache. The selected copy is roughly 187 GB. InvokeAI may need to migrate its legacy database and rescan some models on first launch; keep the old bucket until that has been verified in a live Pod.

The helper defaults to a dry run and uses `rclone copy` for B2-to-B2 migration. It never deletes the legacy backup. Copy the manifest to a private path, insert the two bucket names, and review each source and target. `--execute` performs the copy. The key must be allowed to read the old bucket and write the destination. Objects below 4 GiB use efficient B2 server-side copy. Objects above 3 GiB are streamed through the local machine because large B2 copy-part requests stalled during this migration. The overlap is safe: rclone skips already copied identical objects. Aggregate progress prints every 30 seconds. Streaming large objects uses local bandwidth and may incur B2 egress charges.

```bash
cp config/migration.example.yaml ~/.config/runpod-workspace/migration.yaml
# Edit bucket placeholders in the private manifest.
uv run python scripts/migrate_b2.py --manifest ~/.config/runpod-workspace/migration.yaml
# After reviewing the mapping, repeat with --execute to copy.
```

If the old backup is a single archive rather than normal B2 objects, extract it to a temporary local location first and populate the destination tree with `rclone copy`. The helper does not unpack archives. Keep `models.required: true` only after the new bucket contains at least one model file. For very large image history, omit or narrow the `images` restore mapping until the Pod disk is large enough. Both buckets currently keep all file versions; later overwrites can retain old versions and increase storage use. Review B2 lifecycle settings after confirming the migrated data, without deleting the old backup prematurely.

The sample's optional `swarmui-data`, `invokeai`, and `output` restores permit an empty first-run bucket for settings and output. The `models` restore is required and must already contain files in B2; an empty required restore blocks startup. Seed at least one model file in the new bucket before the first `rp up`, or set `required: false` temporarily for first-run model installation and restore it to `true` afterward. `rp doctor` validates syntax and catalog access; it does not perform a B2 transfer from your local machine. The Pod bootstrap checks B2 connectivity and required prefixes.

## Daily use

```bash
uv run rp doctor
uv run rp up --dry-run
uv run rp up
uv run rp status
uv run rp ssh
uv run rp ssh --command 'nvidia-smi'
uv run rp logs --follow
uv run rp sync
uv run rp sync models
uv run rp down
```

Install the package with `uv tool install .` if you want `rp` directly on your `PATH`. `rp up --no-wait` returns as soon as the Pod ID is saved. `rp --config ./config.yaml ...` selects a different config. `rp -v ...` is accepted for extra progress as commands evolve.

Backups use `rclone copy` by default. Deleting a local file therefore does not delete its B2 copy. `mode: sync` is allowed per mapping but explicitly destructive; `rp` warns when configured. Periodic jobs run in a separate process every 300 seconds by default, retry with backoff and share a file lock with manual and final backups. The InvokeAI mapping takes a consistent SQLite snapshot and backs it up as `databases/invokeai.snapshot.db`; bootstrap restores that snapshot before launching InvokeAI. A failed periodic backup is logged and does not kill the application. The final `rp down` backup is checked by exit status before deletion. `rp down --force` is the explicit escape hatch if the Pod cannot be backed up.

`rp status` reconstructs missing local state from a unique matching Pod name, image and GPU. It refuses ambiguous or mismatched Pods. State lives in `~/.local/state/runpod-workspace/state.json`, is atomically replaced under a lock, and contains no credentials.

## Recovery

- **Pod disappeared:** `rp status` repairs stale state. Start a new Pod with `rp up`; it restores B2 data.
- **B2 unavailable or required restore fails:** the original application does not start. The Pod remains for `rp logs` and `rp ssh` inspection. Retry after fixing B2 by starting a new Pod or manually rerunning bootstrap; do not assume local files survived a restart.
- **SSH or bootstrap slow:** `rp up` retries until its configured timeout. A timeout keeps the Pod for debugging.
- **Final backup fails:** `rp down` exits nonzero and leaves the Pod alive. Run `rp logs`, `rp sync`, then `rp down`. `--force` bypasses that safeguard.
- **Forgot `rp down`:** the Pod-side deadline backs up and tries to delete the Pod. Check `rp status`; a failed backup or self-delete leaves it running and can continue billing.

## Updating the upstream image

Inspect the new image's actual OCI `Entrypoint`, `Cmd`, environment, exposed ports and source startup scripts before changing the base digest. The wrapper explicitly executes InvokeAI's original entrypoint after B2 restore; update both `container/Dockerfile` and `container/entrypoint.sh` if it changes. Build and smoke-test a new wrapper tag, then update `wrapper_image` in YAML. Pin ComfyUI and SwarmUI source commits as well. Do not use `latest` for a reproducible workspace.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy src
bash -n container/*.sh
scripts/smoke_container.sh runpod-workspace:local  # optional Docker, fake B2
```

The normal file-based approach is intentional. Archiving huge trees of small files as `tar.zst` and an optional B2 mount for cold data are possible future optimizations. Performance-sensitive models stay on local Pod disk after restore.
