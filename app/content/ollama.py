import json

import httpx


class OllamaUnavailable(RuntimeError):
    """Ollama could not produce a draft. Nothing was invented."""


class OllamaBadResponse(RuntimeError):
    """The HTTP call worked, but the body was not a draft object."""


class OllamaClient:
    def __init__(self, host: str, model: str, timeout: float = 120.0) -> None:
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout

    def chat_json(self, messages: list[dict]) -> dict:
        url = f"{self.host}/api/chat"
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": "json",
        }
        try:
            response = httpx.post(url, json=payload, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise OllamaUnavailable(
                f"Ollama is not reachable at {self.host}. Start Ollama and try again. No post was generated."
            ) from exc
        if response.status_code != 200:
            detail = response.text[:240].replace("\n", " ")
            raise OllamaUnavailable(
                f"Ollama returned HTTP {response.status_code} at {self.host}. {detail} No post was generated."
            )
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise OllamaBadResponse("Ollama did not return JSON.") from exc
        if isinstance(data, dict) and "error" in data and "message" not in data:
            raise OllamaUnavailable(f"Ollama error: {data.get('error')}. No post was generated.")
        try:
            content = data["message"]["content"]
        except (TypeError, KeyError) as exc:
            raise OllamaBadResponse("Ollama response is missing message.content.") from exc
        if not isinstance(content, str):
            raise OllamaBadResponse("Ollama message.content was not text.")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise OllamaBadResponse("Ollama content was not valid JSON.") from exc
        if not isinstance(parsed, dict):
            raise OllamaBadResponse("Ollama JSON was not an object.")
        return parsed


def check_ollama(host: str, timeout: float = 3.0) -> dict:
    url = host.rstrip("/") + "/api/tags"
    try:
        response = httpx.get(url, timeout=timeout)
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "detail": f"Ollama is not reachable at {host}. ({exc.__class__.__name__})",
            "models": [],
        }
    if response.status_code != 200:
        return {
            "ok": False,
            "detail": f"Ollama returned HTTP {response.status_code} for /api/tags.",
            "models": [],
        }
    try:
        data = response.json()
    except json.JSONDecodeError:
        return {"ok": False, "detail": "Ollama /api/tags did not return JSON.", "models": []}
    names = []
    for item in data.get("models") or []:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
    return {"ok": True, "detail": "Ollama is reachable.", "models": names}
