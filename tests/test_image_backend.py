from __future__ import annotations

import base64
import json

import pytest

from server.image_backend import ComfyUIBackend, ComfyUIOptions, OpenAIImageBackend, create_image_backend


class FakeHttpResponse:
    def __init__(self, json_data=None, content=b""):
        self._json_data = json_data
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return self._json_data


class FakeHttpxClient:
    def __init__(self, object_info=None):
        self.object_info = object_info or {}
        self.posts = []
        self.gets = []

    async def post(self, path, json=None, headers=None):
        self.posts.append((path, json))
        if path == "/images/generations":
            return FakeHttpResponse(json_data={"data": [{"b64_json": base64.b64encode(b"openai-bytes").decode()}]})
        return FakeHttpResponse(json_data={"prompt_id": "abc123"})

    async def get(self, path, params=None):
        self.gets.append((path, params))
        if path == "/object_info":
            return FakeHttpResponse(json_data=self.object_info)
        if path == "/history/abc123":
            return FakeHttpResponse(
                json_data={"abc123": {"outputs": {"9": {"images": [{"filename": "x.png", "subfolder": "", "type": "output"}]}}}}
            )
        assert path == "/view"
        return FakeHttpResponse(content=b"real-png-bytes")


class FakeWebSocket:
    """Stands in for a `websockets.connect(...)` context manager - yields
    pre-scripted JSON text messages, same "reassign the client" testing
    convention tests/test_narrator_ollama.py already established."""

    def __init__(self, messages):
        self._messages = list(messages)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return json.dumps(self._messages.pop(0))


def _fake_connect(messages):
    def connect(url):
        return FakeWebSocket(messages)

    return connect


_DONE_MESSAGE = {"type": "executing", "data": {"node": None, "prompt_id": "abc123"}}


def _backend(object_info=None, **options_kwargs) -> tuple[ComfyUIBackend, FakeHttpxClient]:
    backend = ComfyUIBackend(
        base_url="http://fake:8188", checkpoint="model.safetensors", options=ComfyUIOptions(**options_kwargs)
    )
    fake_http = FakeHttpxClient(object_info=object_info)
    backend._client = fake_http
    return backend, fake_http


async def test_generate_portrait_reports_progress_and_returns_the_final_image():
    backend, http = _backend()
    backend._connect_ws = _fake_connect([
        {"type": "progress", "data": {"value": 4, "max": 20}},
        {"type": "progress", "data": {"value": 12, "max": 20}},
        _DONE_MESSAGE,
    ])
    ticks = []

    async def on_progress(step, total):
        ticks.append((step, total))

    result = await backend.generate_portrait("a fighter", on_progress=on_progress)

    assert result == b"real-png-bytes"
    assert ticks == [(4, 20), (12, 20)]
    submitted = http.posts[0][1]["prompt"]
    assert submitted["6"]["inputs"]["text"] == "a fighter"
    assert submitted["4"]["inputs"]["ckpt_name"] == "model.safetensors"


async def test_generate_portrait_works_with_no_progress_callback():
    backend, _ = _backend()
    backend._connect_ws = _fake_connect([_DONE_MESSAGE])

    result = await backend.generate_portrait("a fighter")

    assert result == b"real-png-bytes"


async def test_generate_portrait_times_out_if_comfyui_never_finishes():
    backend, _ = _backend()
    backend.timeout = 0
    backend._connect_ws = _fake_connect([{"type": "progress", "data": {"value": 1, "max": 20}}])

    with pytest.raises(TimeoutError):
        await backend.generate_portrait("a fighter")


async def test_lora_option_adds_lora_loader_and_rewires_model_and_clip():
    backend, http = _backend(lora="my-style.safetensors", lora_strength=0.6)
    backend._connect_ws = _fake_connect([_DONE_MESSAGE])

    await backend.generate_portrait("a fighter")

    submitted = http.posts[0][1]["prompt"]
    assert submitted["10"]["class_type"] == "LoraLoader"
    assert submitted["10"]["inputs"]["lora_name"] == "my-style.safetensors"
    assert submitted["10"]["inputs"]["strength_model"] == 0.6
    # KSampler and both CLIPTextEncodes now read through the LoRA, not the
    # checkpoint directly.
    assert submitted["3"]["inputs"]["model"] == ["10", 0]
    assert submitted["6"]["inputs"]["clip"] == ["10", 1]
    assert submitted["7"]["inputs"]["clip"] == ["10", 1]


async def test_no_lora_option_leaves_the_base_graph_wired_to_the_checkpoint_directly():
    backend, http = _backend()
    backend._connect_ws = _fake_connect([_DONE_MESSAGE])

    await backend.generate_portrait("a fighter")

    submitted = http.posts[0][1]["prompt"]
    assert "10" not in submitted
    assert submitted["3"]["inputs"]["model"] == ["4", 0]
    assert submitted["6"]["inputs"]["clip"] == ["4", 1]


async def test_hires_fix_adds_upscale_and_a_second_sampler_pass():
    backend, http = _backend(hires_fix=True, hires_scale=2.0, hires_denoise=0.4)
    backend._connect_ws = _fake_connect([_DONE_MESSAGE])

    await backend.generate_portrait("a fighter")

    submitted = http.posts[0][1]["prompt"]
    assert submitted["11"]["class_type"] == "LatentUpscale"
    assert submitted["11"]["inputs"]["samples"] == ["3", 0]  # first pass's latent, not the decoded image
    assert submitted["11"]["inputs"]["width"] == 1024  # 512 * 2.0
    assert submitted["12"]["class_type"] == "KSampler"
    assert submitted["12"]["inputs"]["denoise"] == 0.4
    assert submitted["12"]["inputs"]["latent_image"] == ["11", 0]
    # Final decode/save reads the second pass, not the first.
    assert submitted["8"]["inputs"]["samples"] == ["12", 0]
    assert submitted["9"]["inputs"]["images"] == ["8", 0]


async def test_no_hires_fix_decodes_the_first_pass_directly():
    backend, http = _backend()
    backend._connect_ws = _fake_connect([_DONE_MESSAGE])

    await backend.generate_portrait("a fighter")

    submitted = http.posts[0][1]["prompt"]
    assert "11" not in submitted and "12" not in submitted
    assert submitted["8"]["inputs"]["samples"] == ["3", 0]


async def test_face_detail_adds_the_node_when_comfyui_reports_it_available():
    backend, http = _backend(
        object_info={"FaceDetailer": {}, "UltralyticsDetectorProvider": {}}, face_detail=True
    )
    backend._connect_ws = _fake_connect([_DONE_MESSAGE])

    await backend.generate_portrait("a fighter")

    submitted = http.posts[0][1]["prompt"]
    assert submitted["14"]["class_type"] == "FaceDetailer"
    assert submitted["9"]["inputs"]["images"] == ["14", 0]  # final save reads the refined face pass
    assert any(g[0] == "/object_info" for g in http.gets)


async def test_face_detail_is_silently_skipped_when_comfyui_lacks_the_node():
    # A bare install doesn't have the Impact Pack - this is a real,
    # honestly-scoped gap, not an error.
    backend, http = _backend(object_info={}, face_detail=True)
    backend._connect_ws = _fake_connect([_DONE_MESSAGE])

    result = await backend.generate_portrait("a fighter")

    assert result == b"real-png-bytes"
    submitted = http.posts[0][1]["prompt"]
    assert "14" not in submitted
    assert submitted["9"]["inputs"]["images"] == ["8", 0]


async def test_face_detail_off_never_calls_object_info():
    backend, http = _backend(face_detail=False)
    backend._connect_ws = _fake_connect([_DONE_MESSAGE])

    await backend.generate_portrait("a fighter")

    assert not any(g[0] == "/object_info" for g in http.gets)


async def test_openai_backend_decodes_the_returned_image_and_never_calls_progress():
    backend = OpenAIImageBackend(api_key="sk-fake", model="gpt-image-1")
    backend._client = FakeHttpxClient()
    calls = []

    async def on_progress(step, total):
        calls.append((step, total))

    result = await backend.generate_portrait("a fighter", on_progress=on_progress)

    assert result == b"openai-bytes"
    assert calls == []


def test_create_image_backend_is_none_when_unset(monkeypatch):
    monkeypatch.delenv("IMAGE_BACKEND", raising=False)
    assert create_image_backend() is None


def test_create_image_backend_comfyui_uses_documented_defaults(monkeypatch):
    monkeypatch.setenv("IMAGE_BACKEND", "comfyui")
    monkeypatch.delenv("COMFYUI_URL", raising=False)
    monkeypatch.delenv("COMFYUI_CHECKPOINT", raising=False)
    backend = create_image_backend()
    assert isinstance(backend, ComfyUIBackend)
    assert backend.checkpoint == "v1-5-pruned-emaonly.safetensors"
    assert backend.options.lora is None
    assert backend.options.hires_fix is False


def test_create_image_backend_comfyui_reads_optional_quality_flags(monkeypatch):
    monkeypatch.setenv("IMAGE_BACKEND", "comfyui")
    monkeypatch.setenv("COMFYUI_LORA", "my-style.safetensors")
    monkeypatch.setenv("COMFYUI_HIRES_FIX", "1")
    monkeypatch.setenv("COMFYUI_FACE_DETAIL", "true")
    backend = create_image_backend()
    assert backend.options.lora == "my-style.safetensors"
    assert backend.options.hires_fix is True
    assert backend.options.face_detail is True


def test_create_image_backend_openai_requires_an_api_key(monkeypatch):
    monkeypatch.setenv("IMAGE_BACKEND", "openai")
    monkeypatch.delenv("IMAGE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError):
        create_image_backend()


def test_create_image_backend_openai_returns_a_backend_when_keyed(monkeypatch):
    monkeypatch.setenv("IMAGE_BACKEND", "openai")
    monkeypatch.setenv("IMAGE_API_KEY", "sk-fake")
    backend = create_image_backend()
    assert isinstance(backend, OpenAIImageBackend)


def test_create_image_backend_rejects_an_unknown_backend(monkeypatch):
    monkeypatch.setenv("IMAGE_BACKEND", "midjourney")
    with pytest.raises(ValueError):
        create_image_backend()
