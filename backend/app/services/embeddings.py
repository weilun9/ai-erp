"""Text embeddings for knowledge-base search.

EMBED_PROVIDER:
  none    no vectors; search is keyword-only (still works, just less forgiving)
  local   fastembed: an ONNX model run in-process on the CPU, downloaded once into
          var/models. No background service.
  openai  any OpenAI-compatible /embeddings endpoint (model gateway serving bge-m3...)
Tests swap in their own with `set_embedder`.
"""

import os
from pathlib import Path
from typing import Protocol

import httpx

from app.core.config import get_settings


class EmbedError(Exception):
    pass


class Embedder(Protocol):
    name: str

    def documents(self, texts: list[str]) -> list[list[float]]: ...

    def query(self, text: str) -> list[float]: ...


class Local:
    def __init__(self, model: str) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # optional dependency
            raise EmbedError('EMBED_PROVIDER=local needs: pip install -e ".[local-embed]"') from exc
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")  # Windows noise
        cache = Path(__file__).resolve().parents[2] / "var" / "models"
        self.name = model
        self._model = TextEmbedding(model_name=model, cache_dir=str(cache))
        # e5 models are trained with these prefixes
        self._doc, self._q = ("passage: ", "query: ") if "e5" in model.lower() else ("", "")

    def documents(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._model.embed([self._doc + t for t in texts])]

    def query(self, text: str) -> list[float]:
        return next(iter(self._model.embed([self._q + text]))).tolist()


class OpenAICompatible:
    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url, self.api_key, self.name = base_url.rstrip("/"), api_key, model

    def _embed(self, texts: list[str]) -> list[list[float]]:
        try:
            r = httpx.post(
                f"{self.base_url}/embeddings",
                json={"model": self.name, "input": texts},
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=60,
            )
        except httpx.HTTPError as exc:
            raise EmbedError(f"Embedding endpoint unreachable: {exc}") from exc
        if r.status_code != 200:
            raise EmbedError(f"Embedding endpoint returned {r.status_code}: {r.text[:200]}")
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]

    def documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 32):
            out += self._embed(texts[i : i + 32])
        return out

    def query(self, text: str) -> list[float]:
        return self._embed([text])[0]


_embedder: Embedder | None = None
_set = False


def set_embedder(e: Embedder | None) -> None:
    global _embedder, _set
    _embedder, _set = e, True


def reset_embedder() -> None:
    global _embedder, _set
    _embedder, _set = None, False


def get_embedder() -> Embedder | None:
    """The configured embedder, built once (loading a local model takes seconds)."""
    global _embedder, _set
    if _set:
        return _embedder
    s = get_settings()
    if s.embed_provider == "local":
        _embedder = Local(s.embed_model)
    elif s.embed_provider == "openai" and s.embed_base_url:
        _embedder = OpenAICompatible(s.embed_base_url, s.embed_api_key, s.embed_model)
    else:
        _embedder = None
    _set = True
    return _embedder
