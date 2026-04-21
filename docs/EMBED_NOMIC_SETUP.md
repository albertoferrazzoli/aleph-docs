# Running the Nomic multimodal embedder locally

When `EMBED_BACKEND=nomic_multimodal_local`, the aleph MCP embeds text
**and** images into the same 768-dim latent space by calling a tiny
HTTP server that you run **on the host** (not inside the mcp
container). The pattern mirrors the `whisper_local` host bridge: torch
+ model weights (~2 GB) are too heavy to ship inside Docker, so they
live on the host and the container reaches them via
`host.docker.internal:8091`.

The models are:

- [`nomic-ai/nomic-embed-text-v1.5`](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5) — 768-dim text embeddings.
- [`nomic-ai/nomic-embed-vision-v1.5`](https://huggingface.co/nomic-ai/nomic-embed-vision-v1.5) — 768-dim image embeddings **in the same latent space**, so cosine(text, image) is meaningful for cross-modal retrieval.

Recurring cost: **$0**. One-time cost: **~2 GB of disk** for the
model weights on first launch (cached in `~/.cache/huggingface/`).

---

## Install + run the server

On Apple Silicon you get Metal (MPS) acceleration for free — ~50 ms per
request on an M-series Mac.

```bash
cd docker/nomic-embed-server
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# First launch downloads ~2 GB from HuggingFace (logged to stderr).
# Subsequent launches are instant.
uvicorn server:app --host 0.0.0.0 --port 8091
```

Verify:

```bash
curl -s http://localhost:8091/health | jq
# {"status":"ok","device":"mps","text_model":"...","vision_model":"...","dim":768}

curl -s http://localhost:8091/embed/text \
    -H 'content-type: application/json' \
    -d '{"text":"grafico a candele giapponesi"}' | jq '.vector | length'
# 768

curl -s -F file=@/path/to/screenshot.png \
    http://localhost:8091/embed/image | jq '.vector | length'
# 768
```

Both vectors live in the same space, so you can compare them directly:
a text query about "chart with candlesticks" will score high against
an image of exactly that.

---

## Configure aleph

Because pgvector columns are dim-fixed at CREATE TABLE time, switching
from a Gemini (1536) or Ollama `bge-m3` (1024) setup to Nomic (768)
requires a fresh database. The schema init script reads `EMBED_DIM`
from the environment on first boot, so the only friction is a
`docker compose down -v`.

In `.env`:

```bash
EMBED_BACKEND=nomic_multimodal_local
EMBED_DIM=768
HYBRID_MEDIA_EMBEDDING=true
EMBED_NOMIC_HOST=http://host.docker.internal:8091   # macOS / Windows
# EMBED_NOMIC_HOST=http://172.17.0.1:8091           # Linux default bridge
```

Then (destructive — this drops all pgvector data):

```bash
docker compose down -v
docker compose up -d --build
```

---

## Running as a launchd / systemd service (optional)

Keep the server alive across reboots. macOS example (launchd plist,
save as `~/Library/LaunchAgents/com.aleph.nomic-embed.plist`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>Label</key><string>com.aleph.nomic-embed</string>
    <key>ProgramArguments</key><array>
        <string>/ABSOLUTE/PATH/TO/aleph-docs/docker/nomic-embed-server/.venv/bin/uvicorn</string>
        <string>server:app</string>
        <string>--host</string><string>0.0.0.0</string>
        <string>--port</string><string>8091</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/ABSOLUTE/PATH/TO/aleph-docs/docker/nomic-embed-server</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/tmp/nomic-embed.out.log</string>
    <key>StandardErrorPath</key><string>/tmp/nomic-embed.err.log</string>
</dict></plist>
```

Load it:

```bash
launchctl load ~/Library/LaunchAgents/com.aleph.nomic-embed.plist
```

---

## What gets embedded with this backend

| Source file | Chunks produced |
|---|---|
| `.md` / `.mdx` | `doc_chunk` (text) |
| `.png` / `.jpg` / `.webp` | `image` (vision) |
| `.mp4` / `.mov` | `video_transcript` (Whisper → text) **and** `image` (per-scene keyframe, `metadata.origin="video_keyframe"`) when `HYBRID_MEDIA_EMBEDDING=true` |
| `.mp3` / `.wav` | `audio_transcript` (Whisper → text) |
| `.pdf` | `pdf_text` (extracted text) |

Note: `video_scene`, `audio_clip`, and `pdf_page` (full media blobs)
are **not** supported by this backend — it declares only
`{text, image}` modalities. The reconciler skips incompatible kinds
with a clear error. The keyframe-image rows compensate for the missing
`video_scene`: you can still search on what a video *shows*, not only
on what its transcript *says*.
