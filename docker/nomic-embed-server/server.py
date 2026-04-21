"""Tiny FastAPI bridge exposing Nomic text + vision embedders as HTTP.

Runs on the host (not inside the mcp container — torch + weights are
~2 GB and too heavy to ship in-container). The mcp container reaches
it at `host.docker.internal:8091`, same pattern as the whisper_local
ASR bridge. See `docs/EMBED_NOMIC_SETUP.md` for setup.

Models:
    nomic-ai/nomic-embed-text-v1.5     (768-dim, via sentence-transformers)
    nomic-ai/nomic-embed-vision-v1.5   (768-dim, ViT — loaded via transformers
                                        AutoModel + AutoImageProcessor; shares
                                        the latent space with the text model)

On first launch the models are downloaded from HuggingFace (~2 GB) into
the standard HF cache. Subsequent launches are instant.
"""

from __future__ import annotations

import io
import logging
import os
from typing import List

import torch
import torch.nn.functional as F
from fastapi import FastAPI, File, HTTPException, UploadFile
from PIL import Image
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer
from transformers import AutoImageProcessor, AutoModel

log = logging.getLogger("nomic-embed-server")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))


def _pick_device() -> str:
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


DEVICE = _pick_device()
TEXT_MODEL_ID = os.environ.get("NOMIC_TEXT_MODEL", "nomic-ai/nomic-embed-text-v1.5")
VISION_MODEL_ID = os.environ.get("NOMIC_VISION_MODEL", "nomic-ai/nomic-embed-vision-v1.5")
NATIVE_DIM = 768

log.info("nomic-embed-server: loading text=%s vision=%s device=%s",
         TEXT_MODEL_ID, VISION_MODEL_ID, DEVICE)

# Text: sentence-transformers handles tokenizer + pooling.
text_model = SentenceTransformer(
    TEXT_MODEL_ID, device=DEVICE, trust_remote_code=True,
)

# Vision: custom ViT, sentence-transformers can't load it — use
# transformers directly and do the normalization ourselves. CLS token
# of the last hidden state is the joint-space embedding.
vision_processor = AutoImageProcessor.from_pretrained(VISION_MODEL_ID)
vision_model = AutoModel.from_pretrained(
    VISION_MODEL_ID, trust_remote_code=True,
).to(DEVICE)
vision_model.eval()

app = FastAPI(title="nomic-embed-server", version="1.0")


class TextRequest(BaseModel):
    text: str


class EmbedResponse(BaseModel):
    vector: List[float]


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "device": DEVICE,
        "text_model": TEXT_MODEL_ID,
        "vision_model": VISION_MODEL_ID,
        "dim": NATIVE_DIM,
    }


@app.post("/embed/text", response_model=EmbedResponse)
def embed_text(req: TextRequest) -> EmbedResponse:
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "text is empty")
    # For joint text+image retrieval, Nomic requires:
    #   1. "search_query:" prefix (NOT "search_document:") — this is the
    #      prefix aligned with the vision encoder's latent space.
    #   2. A layer_norm BEFORE L2-normalization — this is the projection
    #      step that maps text vectors into the vision-compatible
    #      subspace. Skipping it leaves text and image on parallel but
    #      offset manifolds: cosine collapses to ~0.07 noise.
    # See https://huggingface.co/nomic-ai/nomic-embed-vision-v1.5
    prefixed = f"search_query: {text}"
    # Disable sentence-transformers' internal normalize — we do the
    # layer_norm → L2-norm pipeline manually.
    vec = text_model.encode(prefixed, convert_to_tensor=True, normalize_embeddings=False)
    vec = vec.unsqueeze(0) if vec.dim() == 1 else vec
    vec = F.layer_norm(vec, normalized_shape=(vec.shape[-1],))
    vec = F.normalize(vec, p=2, dim=1)[0]
    return EmbedResponse(vector=[float(x) for x in vec.detach().cpu().tolist()])


@app.post("/embed/image", response_model=EmbedResponse)
async def embed_image(file: UploadFile = File(...)) -> EmbedResponse:
    try:
        raw = await file.read()
        img = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as e:
        raise HTTPException(400, f"could not decode image: {e}") from e

    inputs = vision_processor(images=img, return_tensors="pt").to(DEVICE)
    with torch.no_grad():
        out = vision_model(**inputs)
    # CLS token @ pos 0 of last_hidden_state lives in the shared latent
    # space; L2-normalise so cosine matches dot-product downstream.
    emb = out.last_hidden_state[:, 0]
    emb = F.normalize(emb, p=2, dim=1)[0].detach().cpu().tolist()
    return EmbedResponse(vector=[float(x) for x in emb])
