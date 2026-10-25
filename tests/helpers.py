from app.config import Settings
from app.transport import HttpResult


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "database_path": str(tmp_path / "app.db"),
        "log_dir": str(tmp_path / "logs"),
        "backup_dir": str(tmp_path / "backups"),
        "start_scheduler": False,
        "ollama_host": "http://127.0.0.1:9",
        "ollama_timeout": 2,
        "generation_attempts": 3,
    }
    values.update(overrides)
    return Settings(**values)


def valid_draft(**overrides) -> dict:
    data = {
        "topic_key": "been-vs-gone",
        "category": "korean_learner_mistakes",
        "platform": "instagram",
        "hook": "Been and gone are not the same trip.",
        "english_examples": [
            "I have been to Busan. I went and came back.",
            "I have gone to Busan. I am still there.",
        ],
        "korean_explanation": "한국 학습자는 경험과 현재 결과를 자주 섞습니다. have been은 갔다 온 경험입니다.",
        "engagement_question": "Which sentence matches your last trip?",
        "cta": "Save this for your next speaking practice.",
        "hashtags": ["#EnglishWithKorean", "#영어공부", "#KoreanLearners"],
        "carousel_outline": None,
        "uncertain_linguistic_claim": False,
        "uncertainty_reason": "",
    }
    data.update(overrides)
    return data


class FakeTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.handler(method, url, kwargs)


def ok_json(payload: dict, status: int = 200) -> HttpResult:
    import json

    return HttpResult(status, json.dumps(payload))
