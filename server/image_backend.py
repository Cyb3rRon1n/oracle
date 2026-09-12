"""Pluggable portrait-generation backend. Mirrors narrator.py's
create_narrator() selector pattern - one Protocol, one factory function -
so the engine never needs to know which backend (or none) is configured.

Unlike the DM backend, having no image backend configured is a normal,
supported state: portrait generation is an optional, GPU-dependent
feature (see create_image_backend), not a required one."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Protocol

import httpx


class ImageBackend(Protocol):
    async def generate_portrait(self, description: str) -> bytes: ...


# A real, working thing to say when a caller asks for a portrait but no
# style guidance is given.
_NEGATIVE_PROMPT = "blurry, deformed, extra limbs, bad anatomy, watermark, text, low quality"
# Portrait aspect (taller than wide). Steps/cfg/sampler are fixed
# constants, not env-configurable knobs - real values to expose later if
# someone actually needs to tune them, not speculative config now.
_WIDTH = 512
_HEIGHT = 768
_STEPS = 20
_CFG = 7.0
_SAMPLER = "euler"
_SCHEDULER = "normal"


class ComfyUIBackend:
    """Talks to a real ComfyUI instance's own HTTP API - the same server
    Anvil already generates a Docker stack for, so a co-located Anvil
    install already provides this for free (point COMFYUI_URL at it) -
    the same "already running X on this host?" integration nightwire and
    vulcan already use for Ollama.

    A minimal, fixed txt2img workflow graph is posted for every
    generation: CheckpointLoader -> two CLIPTextEncodes (positive/
    negative) -> EmptyLatentImage -> KSampler -> VAEDecode -> SaveImage -
    the same shape already real-verified against ComfyUI on real GPU
    hardware (Anvil's own v0.17 verification), not guessed from docs.
    No reference-photo/img2img support - that needs IPAdapter or similar,
    a real custom-node dependency a bare ComfyUI install doesn't have;
    text-prompt-only for now."""

    def __init__(self, base_url: str, checkpoint: str, poll_interval: float = 1.0, timeout: float = 120.0):
        self.checkpoint = checkpoint
        self.poll_interval = poll_interval
        self.timeout = timeout
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=30.0)

    def _workflow(self, prompt: str, seed: int) -> dict:
        return {
            "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": self.checkpoint}},
            "5": {"class_type": "EmptyLatentImage", "inputs": {"width": _WIDTH, "height": _HEIGHT, "batch_size": 1}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"text": _NEGATIVE_PROMPT, "clip": ["4", 1]}},
            "3": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": seed,
                    "steps": _STEPS,
                    "cfg": _CFG,
                    "sampler_name": _SAMPLER,
                    "scheduler": _SCHEDULER,
                    "denoise": 1.0,
                    "model": ["4", 0],
                    "positive": ["6", 0],
                    "negative": ["7", 0],
                    "latent_image": ["5", 0],
                },
            },
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
            "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "oracle_portrait", "images": ["8", 0]}},
        }

    async def generate_portrait(self, description: str) -> bytes:
        seed = uuid.uuid4().int & 0xFFFFFFFF
        submit = await self._client.post(
            "/prompt", json={"prompt": self._workflow(description, seed), "client_id": str(uuid.uuid4())}
        )
        submit.raise_for_status()
        prompt_id = submit.json()["prompt_id"]

        deadline = time.monotonic() + self.timeout
        while True:
            poll = await self._client.get(f"/history/{prompt_id}")
            poll.raise_for_status()
            history = poll.json()
            if prompt_id in history:
                outputs = history[prompt_id]["outputs"]
                break
            if time.monotonic() > deadline:
                raise TimeoutError(f"ComfyUI didn't finish generating within {self.timeout}s")
            await asyncio.sleep(self.poll_interval)

        image_info = next(
            image for node_output in outputs.values() for image in node_output.get("images", [])
        )
        view = await self._client.get(
            "/view",
            params={
                "filename": image_info["filename"],
                "subfolder": image_info.get("subfolder", ""),
                "type": image_info.get("type", "output"),
            },
        )
        view.raise_for_status()
        return view.content


def create_image_backend() -> ImageBackend | None:
    """None (portrait generation disabled) unless COMFYUI_URL is set -
    unlike create_narrator's DM backend, which is always required, this
    is an optional feature with a real "not configured" state."""
    base_url = os.environ.get("COMFYUI_URL", "").strip()
    if not base_url:
        return None
    checkpoint = os.environ.get("COMFYUI_CHECKPOINT", "v1-5-pruned-emaonly.safetensors")
    return ComfyUIBackend(base_url, checkpoint)
