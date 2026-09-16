"""Minimal HTTPS Telegram Bot API client. Never logs request URLs or tokens."""
import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ApiError(Exception):
    def __init__(self, code, description="Telegram API error", retry_after=0):
        self.code = int(code)
        self.description = re.sub(r"\d{5,}:[A-Za-z0-9_-]{20,}", "[TOKEN]", str(description))
        self.retry_after = int(retry_after or 0)
        super().__init__(f"Telegram {self.code}: {self.description}")


class TransportError(Exception):
    pass


class Telegram:
    def __init__(self, token):
        self._token = token

    def call(self, method, **params):
        if not re.fullmatch(r"[A-Za-z]+", method):
            raise ValueError("Invalid API method")
        request = Request(
            f"https://api.telegram.org/bot{self._token}/{method}",
            data=json.dumps(params, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        timeout = int(params.get("timeout", 0)) + 8
        try:
            try:
                with urlopen(request, timeout=timeout) as response:
                    raw = response.read(2_000_000)
            except HTTPError as exc:
                with exc:
                    raw = exc.read(2_000_000)
                try:
                    result = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise ApiError(exc.code, "Ошибка ответа Telegram") from None
                raise ApiError(result.get("error_code", exc.code), result.get("description", ""),
                               result.get("parameters", {}).get("retry_after", 0)) from None
        except (URLError, TimeoutError, OSError):
            raise TransportError("Нет ответа Telegram; запрос может быть выполнен") from None
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise TransportError("Telegram вернул некорректный ответ") from None
        if not result.get("ok"):
            raise ApiError(result.get("error_code", 500), result.get("description", ""),
                           result.get("parameters", {}).get("retry_after", 0))
        return result["result"]
