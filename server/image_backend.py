"""Pluggable portrait-generation backends. Mirrors narrator.py's
create_narrator() selector pattern - one Protocol, one factory function -
so the engine never needs to know which backend (or none) is configured.

Unlike the DM backend, having no image backend configured is a normal,
supported state: portrait generation is an optional feature (see
create_image_backend), not a required one."""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import httpx
import websockets

# Called with (step, total) as generation progresses. Only ComfyUIBackend
# ever calls it (real per-step progress from its own websocket) - the
# hosted OpenAIImageBackend is one request/response with no steps to
# report, so it simply never calls the callback.
ProgressCallback = Callable[[int, int], Awaitable[None]]


class ImageBackend(Protocol):
    async def generate_portrait(self, description: str, on_progress: ProgressCallback | None = None) -> bytes: ...


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


@dataclass
class ComfyUIOptions:
    """Optional quality passes, each opt-in and each using only what a
    real ComfyUI install actually has. lora/hires_fix use core node types
    ComfyUI ships with by default - no detection needed, only a real file
    on disk for lora (same "doesn't manage its own downloads" constraint
    Anvil's own README already documents for checkpoints). face_detail
    needs the Impact Pack's FaceDetailer, a real custom node most bare
    installs don't have - real-detected via /object_info before use,
    silently skipped (not an error) when it isn't there."""

    lora: str | None = None
    lora_strength: float = 0.8
    hires_fix: bool = False
    hires_scale: float = 1.5
    hires_denoise: float = 0.5
    face_detail: bool = False
    face_detector: str = "bbox/face_yolov8m.pt"


class ComfyUIBackend:
    """Talks to a real ComfyUI instance's own HTTP + WebSocket API - the
    same server Anvil already generates a Docker stack for, so a
    co-located Anvil install already provides this for free (point
    COMFYUI_URL at it) - the same "already running X on this host?"
    integration nightwire and vulcan already use for Ollama.

    The workflow graph is built up in stages (_base_workflow, then
    _add_lora/_add_hires_fix/_add_face_detail as configured) rather than
    one fixed dict, so each quality pass can be reasoned about and tested
    independently. Every node type used by the base graph and by
    lora/hires_fix is a real ComfyUI core node, shipped with every
    install - the same shape already real-verified against ComfyUI on
    real GPU hardware (Anvil's own v0.17 verification) for the base graph,
    extended here rather than replaced. No reference-photo/img2img
    support - that needs IPAdapter, a real custom-node dependency a bare
    install doesn't have; text-prompt-only for now."""

    def __init__(
        self,
        base_url: str,
        checkpoint: str,
        options: ComfyUIOptions | None = None,
        timeout: float = 120.0,
    ):
        self.checkpoint = checkpoint
        self.options = options or ComfyUIOptions()
        self.timeout = timeout
        base_url = base_url.rstrip("/")
        self._ws_base = "ws" + base_url.removeprefix("http")
        self._client = httpx.AsyncClient(base_url=base_url, timeout=30.0)
        self._connect_ws = websockets.connect
        self._available_nodes: set[str] | None = None

    async def _object_info_keys(self) -> set[str]:
        if self._available_nodes is None:
            response = await self._client.get("/object_info")
            response.raise_for_status()
            self._available_nodes = set(response.json().keys())
        return self._available_nodes

    def _base_workflow(self, prompt: str, seed: int) -> tuple[dict, str]:
        """The always-present graph: checkpoint -> two CLIPTextEncodes ->
        latent -> KSampler -> VAEDecode -> SaveImage. Returns (nodes,
        last_ksampler_id) so later stages know which node's latent output
        to build on."""
        nodes = {
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
        }
        return nodes, "3"

    def _add_lora(self, nodes: dict) -> None:
        """LoraLoader is a core ComfyUI node - always available, no
        detection needed. Rewires the base graph's model/clip references
        (node "4"'s direct outputs) to flow through it instead - every
        node that read from the checkpoint directly now reads from here."""
        nodes["10"] = {
            "class_type": "LoraLoader",
            "inputs": {
                "model": ["4", 0],
                "clip": ["4", 1],
                "lora_name": self.options.lora,
                "strength_model": self.options.lora_strength,
                "strength_clip": self.options.lora_strength,
            },
        }
        nodes["6"]["inputs"]["clip"] = ["10", 1]
        nodes["7"]["inputs"]["clip"] = ["10", 1]
        nodes["3"]["inputs"]["model"] = ["10", 0]

    def _add_hires_fix(self, nodes: dict, sampler_id: str) -> str:
        """The standard "hi-res fix" pattern: upscale the first pass's
        LATENT (not the decoded image - staying in latent space avoids a
        redundant decode/encode round trip), then a second, lower-denoise
        KSampler pass to add real detail at the higher resolution.
        LatentUpscale is a core node. Returns the new final sampler id."""
        nodes["11"] = {
            "class_type": "LatentUpscale",
            "inputs": {
                "samples": [sampler_id, 0],
                "upscale_method": "nearest-exact",
                "width": round(_WIDTH * self.options.hires_scale),
                "height": round(_HEIGHT * self.options.hires_scale),
                "crop": "disabled",
            },
        }
        first = nodes[sampler_id]["inputs"]
        nodes["12"] = {
            "class_type": "KSampler",
            "inputs": {
                "seed": first["seed"],
                "steps": _STEPS,
                "cfg": _CFG,
                "sampler_name": _SAMPLER,
                "scheduler": _SCHEDULER,
                "denoise": self.options.hires_denoise,
                "model": first["model"],
                "positive": first["positive"],
                "negative": first["negative"],
                "latent_image": ["11", 0],
            },
        }
        return "12"

    def _add_face_detail(self, nodes: dict, image_ref: list, sampler_id: str) -> list:
        """FaceDetailer (ComfyUI-Impact-Pack) isn't a core node - only
        added when a real /object_info check (see generate_portrait)
        confirms it's actually installed. Detects faces in the decoded
        image and re-samples just that region at higher fidelity. Returns
        the new final image reference."""
        first = nodes[sampler_id]["inputs"]
        nodes["13"] = {"class_type": "UltralyticsDetectorProvider", "inputs": {"model_name": self.options.face_detector}}
        nodes["14"] = {
            "class_type": "FaceDetailer",
            "inputs": {
                "image": image_ref,
                "model": first["model"],
                "clip": ["4", 1] if "10" not in nodes else ["10", 1],
                "vae": ["4", 2],
                "positive": first["positive"],
                "negative": first["negative"],
                "bbox_detector": ["13", 0],
                "guide_size": 384,
                "guide_size_for": True,
                "max_size": 1024,
                "seed": first["seed"],
                "steps": _STEPS,
                "cfg": _CFG,
                "sampler_name": _SAMPLER,
                "scheduler": _SCHEDULER,
                "denoise": 0.5,
                "feather": 5,
                "noise_mask": True,
                "force_inpaint": True,
                "bbox_threshold": 0.5,
                "bbox_dilation": 10,
                "bbox_crop_factor": 3.0,
                "sam_detection_hint": "center-1",
            },
        }
        return ["14", 0]

    async def _build_workflow(self, prompt: str, seed: int) -> dict:
        nodes, sampler_id = self._base_workflow(prompt, seed)
        if self.options.lora:
            self._add_lora(nodes)
        if self.options.hires_fix:
            sampler_id = self._add_hires_fix(nodes, sampler_id)

        nodes["8"] = {"class_type": "VAEDecode", "inputs": {"samples": [sampler_id, 0], "vae": ["4", 2]}}
        image_ref = ["8", 0]

        if self.options.face_detail:
            available = await self._object_info_keys()
            if "FaceDetailer" in available and "UltralyticsDetectorProvider" in available:
                image_ref = self._add_face_detail(nodes, image_ref, sampler_id)

        nodes["9"] = {"class_type": "SaveImage", "inputs": {"filename_prefix": "oracle_portrait", "images": image_ref}}
        return nodes

    async def generate_portrait(self, description: str, on_progress: ProgressCallback | None = None) -> bytes:
        seed = uuid.uuid4().int & 0xFFFFFFFF
        workflow = await self._build_workflow(description, seed)
        client_id = str(uuid.uuid4())

        async with self._connect_ws(f"{self._ws_base}/ws?clientId={client_id}") as ws:
            submit = await self._client.post("/prompt", json={"prompt": workflow, "client_id": client_id})
            submit.raise_for_status()
            prompt_id = submit.json()["prompt_id"]

            deadline = time.monotonic() + self.timeout
            async for raw in ws:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"ComfyUI didn't finish generating within {self.timeout}s")
                message = json.loads(raw)
                data = message.get("data", {})
                if message.get("type") == "progress" and on_progress is not None:
                    await on_progress(data.get("value", 0), data.get("max", 0))
                elif message.get("type") == "executing" and data.get("prompt_id") == prompt_id and data.get("node") is None:
                    break

        history_response = await self._client.get(f"/history/{prompt_id}")
        history_response.raise_for_status()
        outputs = history_response.json()[prompt_id]["outputs"]
        image_info = next(image for node_output in outputs.values() for image in node_output.get("images", []))

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


class OpenAIImageBackend:
    """A hosted alternative for users without a local GPU/ComfyUI install -
    real OpenAI Images API (gpt-image-1). Costs money per image; a real
    API key is required. No progress reporting - one request/response,
    no per-step data the way a local diffusion sampler has."""

    def __init__(self, api_key: str, model: str = "gpt-image-1", size: str = "1024x1536"):
        self.model = model
        self.size = size
        self._client = httpx.AsyncClient(
            base_url="https://api.openai.com/v1",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=60.0,
        )

    async def generate_portrait(self, description: str, on_progress: ProgressCallback | None = None) -> bytes:
        response = await self._client.post(
            "/images/generations",
            json={"model": self.model, "prompt": description, "size": self.size, "n": 1},
        )
        response.raise_for_status()
        # gpt-image-1 always returns base64 (unlike dall-e-3, it has no
        # url response_format option at all) - confirmed against OpenAI's
        # own current API reference, not assumed from an older model's docs.
        return base64.b64decode(response.json()["data"][0]["b64_json"])


def create_image_backend() -> ImageBackend | None:
    """None (portrait generation disabled) unless IMAGE_BACKEND is set -
    unlike create_narrator's DM backend, which is always required, this
    is an optional feature with a real "not configured" state."""
    backend = os.environ.get("IMAGE_BACKEND", "").strip().lower()
    if not backend:
        return None
    if backend == "comfyui":
        base_url = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")
        checkpoint = os.environ.get("COMFYUI_CHECKPOINT", "v1-5-pruned-emaonly.safetensors")
        options = ComfyUIOptions(
            lora=os.environ.get("COMFYUI_LORA") or None,
            lora_strength=float(os.environ.get("COMFYUI_LORA_STRENGTH", "0.8")),
            hires_fix=os.environ.get("COMFYUI_HIRES_FIX", "").lower() in ("1", "true", "yes"),
            face_detail=os.environ.get("COMFYUI_FACE_DETAIL", "").lower() in ("1", "true", "yes"),
        )
        return ComfyUIBackend(base_url, checkpoint, options=options)
    if backend == "openai":
        api_key = os.environ.get("IMAGE_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("IMAGE_BACKEND=openai requires IMAGE_API_KEY (or OPENAI_API_KEY)")
        return OpenAIImageBackend(api_key=api_key, model=os.environ.get("IMAGE_MODEL", "gpt-image-1"))
    raise ValueError(f"Unknown IMAGE_BACKEND {backend!r}. Valid backends: 'comfyui', 'openai'.")
