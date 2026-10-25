import json
from dataclasses import dataclass

import httpx


class TransportError(RuntimeError):
    def __init__(self, message: str, *, uncertain: bool) -> None:
        super().__init__(message)
        self.uncertain = uncertain


@dataclass
class HttpResult:
    status_code: int
    text: str

    def json(self) -> dict:
        data = json.loads(self.text)
        if not isinstance(data, dict):
            raise ValueError("JSON body was not an object")
        return data


class HttpxTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        content: bytes | None = None,
        data: dict | None = None,
        params: dict | None = None,
        timeout: float = 30.0,
    ) -> HttpResult:
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.request(
                    method,
                    url,
                    headers=headers,
                    content=content,
                    data=data,
                    params=params,
                )
        except httpx.ConnectError as exc:
            raise TransportError(
                "Could not connect to the platform. Nothing was confirmed.",
                uncertain=False,
            ) from exc
        except httpx.TimeoutException as exc:
            raise TransportError(
                "The platform timed out. The post may or may not exist.",
                uncertain=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise TransportError(
                f"The platform request failed ({exc.__class__.__name__}).",
                uncertain=True,
            ) from exc
        return HttpResult(response.status_code, response.text)
