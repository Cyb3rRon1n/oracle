from __future__ import annotations

import pytest

from server.image_backend import ComfyUIBackend, create_image_backend


class FakeResponse:
    def __init__(self, json_data=None, content=b""):
        self._json_data = json_data
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return self._json_data


class FakeHttpxClient:
    """Stands in for httpx.AsyncClient, swapped onto backend._client the
    same way tests/test_narrator_ollama.py reassigns narrator._client -
    no real network call, no respx dependency needed."""

    def __init__(self, history_replies):
        self.history_replies = list(history_replies)
        self.posts = []
        self.gets = []

    async def post(self, path, json=None):
        self.posts.append((path, json))
        return FakeResponse(json_data={"prompt_id": "abc123"})

    async def get(self, path, params=None):
        self.gets.append((path, params))
        if path.startswith("/history/"):
            reply = self.history_replies.pop(0) if len(self.history_replies) > 1 else self.history_replies[0]
            return FakeResponse(json_data=reply)
        assert path == "/view"
        return FakeResponse(content=b"real-png-bytes")


_NOT_READY = {}
_READY = {"abc123": {"outputs": {"9": {"images": [{"filename": "x.png", "subfolder": "", "type": "output"}]}}}}


async def test_generate_portrait_polls_until_ready_and_fetches_the_image():
    backend = ComfyUIBackend(base_url="http://fake", checkpoint="model.safetensors", poll_interval=0)
    fake = FakeHttpxClient(history_replies=[_NOT_READY, _READY])
    backend._client = fake

    result = await backend.generate_portrait("a fighter")

    assert result == b"real-png-bytes"
    assert fake.posts[0][0] == "/prompt"
    assert fake.posts[0][1]["prompt"]["6"]["inputs"]["text"] == "a fighter"
    assert fake.posts[0][1]["prompt"]["4"]["inputs"]["ckpt_name"] == "model.safetensors"
    # Actually polled more than once before getting the real answer.
    assert len([g for g in fake.gets if g[0].startswith("/history/")]) == 2


async def test_generate_portrait_times_out_if_comfyui_never_finishes():
    backend = ComfyUIBackend(base_url="http://fake", checkpoint="model.safetensors", poll_interval=0, timeout=0)
    backend._client = FakeHttpxClient(history_replies=[_NOT_READY])

    with pytest.raises(TimeoutError):
        await backend.generate_portrait("a fighter")


def test_create_image_backend_is_none_when_comfyui_url_unset(monkeypatch):
    monkeypatch.delenv("COMFYUI_URL", raising=False)
    assert create_image_backend() is None


def test_create_image_backend_returns_a_backend_when_configured(monkeypatch):
    monkeypatch.setenv("COMFYUI_URL", "http://127.0.0.1:8188")
    monkeypatch.delenv("COMFYUI_CHECKPOINT", raising=False)
    backend = create_image_backend()
    assert isinstance(backend, ComfyUIBackend)
    assert backend.checkpoint == "v1-5-pruned-emaonly.safetensors"  # documented default


def test_create_image_backend_respects_configured_checkpoint(monkeypatch):
    monkeypatch.setenv("COMFYUI_URL", "http://127.0.0.1:8188")
    monkeypatch.setenv("COMFYUI_CHECKPOINT", "my-model.safetensors")
    backend = create_image_backend()
    assert backend.checkpoint == "my-model.safetensors"
