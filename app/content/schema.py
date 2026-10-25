import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

CATEGORIES: list[tuple[str, str]] = [
    ("korean_learner_mistakes", "Common Korean learner mistakes"),
    ("conversational_expressions", "Natural conversational expressions"),
    ("business_workplace", "Business and workplace English"),
    ("interview_prep", "Interview prep"),
    ("grammar_quizzes", "Grammar quizzes and interactive questions"),
    ("pronunciation_confidence", "Pronunciation and speaking confidence"),
    ("educational_posts", "Short educational posts and carousel outlines"),
]
CATEGORY_LABELS = dict(CATEGORIES)

PLATFORMS: list[tuple[str, str]] = [
    ("instagram", "Instagram"),
    ("facebook", "Facebook Page"),
    ("naver_blog", "Naver Blog"),
    ("naver_cafe", "Naver Cafe"),
]
PLATFORM_LABELS = dict(PLATFORMS)

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

Category = Literal[
    "korean_learner_mistakes",
    "conversational_expressions",
    "business_workplace",
    "interview_prep",
    "grammar_quizzes",
    "pronunciation_confidence",
    "educational_posts",
]
Platform = Literal["instagram", "facebook", "naver_blog", "naver_cafe"]

_TOPIC_KEY = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_HANGUL = re.compile(r"[\uac00-\ud7a3]")


def _as_list(value: object) -> object:
    if isinstance(value, str):
        parts = [line.strip() for line in value.splitlines() if line.strip()]
        return parts
    return value


class DraftContent(BaseModel):
    model_config = {"extra": "ignore"}

    topic_key: str
    category: Category
    platform: Platform
    hook: str = Field(min_length=5, max_length=300)
    english_examples: list[str] = Field(min_length=2, max_length=6)
    korean_explanation: str = Field(min_length=10, max_length=2000)
    engagement_question: str = Field(min_length=5, max_length=300)
    cta: str | None = None
    hashtags: list[str] = Field(min_length=1, max_length=20)
    carousel_outline: list[str] | None = None
    uncertain_linguistic_claim: bool
    uncertainty_reason: str = ""

    @field_validator("topic_key")
    @classmethod
    def topic_slug(cls, value: str) -> str:
        slug = value.strip().lower()
        if not _TOPIC_KEY.fullmatch(slug) or len(slug) > 80:
            raise ValueError("topic_key must be a lowercase hyphenated slug")
        return slug

    @field_validator("english_examples", mode="before")
    @classmethod
    def examples_list(cls, value: object) -> object:
        return _as_list(value)

    @field_validator("english_examples")
    @classmethod
    def examples_clean(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        if len(cleaned) < 2:
            raise ValueError("At least two English examples are required")
        return cleaned

    @field_validator("korean_explanation")
    @classmethod
    def must_contain_korean(cls, value: str) -> str:
        text = value.strip()
        if not _HANGUL.search(text):
            raise ValueError("korean_explanation must include Korean text")
        return text

    @field_validator("cta", mode="before")
    @classmethod
    def empty_cta(cls, value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("hashtags", mode="before")
    @classmethod
    def tags_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [part for part in re.split(r"[\s,]+", value.strip()) if part]
        return value

    @field_validator("hashtags")
    @classmethod
    def tags_clean(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for tag in value:
            text = tag.strip()
            if not text:
                continue
            if not text.startswith("#"):
                text = "#" + re.sub(r"\s+", "", text)
            cleaned.append(text[:80])
        if not cleaned:
            raise ValueError("At least one hashtag is required")
        return cleaned

    @field_validator("carousel_outline", mode="before")
    @classmethod
    def slides_list(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return _as_list(value)

    @field_validator("carousel_outline")
    @classmethod
    def slides_clean(cls, value: list[str] | None) -> list[str] | None:
        if not value:
            return None
        cleaned = [item.strip() for item in value if item and item.strip()]
        return cleaned or None

    @model_validator(mode="after")
    def review_rules(self) -> "DraftContent":
        if self.uncertain_linguistic_claim and len(self.uncertainty_reason.strip()) < 5:
            raise ValueError("uncertainty_reason is required when a claim is uncertain")
        if self.category == "educational_posts" and (
            not self.carousel_outline or len(self.carousel_outline) < 2
        ):
            raise ValueError("educational posts need a carousel outline of at least two slides")
        return self
