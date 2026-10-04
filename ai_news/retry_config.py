"""Optional, local topic and retry settings for the two Codex stages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import time
import tomllib

DEFAULT_TOPIC_PROMPT = (
    "24時間以内に発表された、注目に値する面白いAIニュースを1件選び、何が起きたかを要約してください。"
)


@dataclass(frozen=True)
class RetryPolicy:
    retry_count: int
    interval_seconds: int
    deadline_seconds: int
    attempt_timeout_seconds: int


@dataclass(frozen=True)
class Topic:
    id: str
    label: str
    prompt: str


DEFAULT_TOPIC = Topic("ai_news", "最新AIニュース", DEFAULT_TOPIC_PROMPT)
TOPIC_ID = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")


@dataclass(frozen=True)
class RetryConfig:
    news: RetryPolicy = RetryPolicy(2, 0, 0, 180)
    image: RetryPolicy = RetryPolicy(2, 0, 0, 300)
    selected_topic_id: str = "ai_news"
    topics: tuple[Topic, ...] = (DEFAULT_TOPIC,)

    @property
    def topic_prompt(self) -> str:
        for topic in self.topics:
            if topic.id == self.selected_topic_id:
                return topic.prompt
        raise ValueError("selected topic ID is missing from news.topics")


def load_retry_config(path: Path | None = None) -> RetryConfig:
    """Read project-root ``config``; a missing file uses the documented defaults."""
    path = path or Path(__file__).resolve().parents[1] / "config"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return RetryConfig()
    if not isinstance(data, dict) or set(data) - {"news", "image", "push"}:
        raise ValueError("config supports only [news], [image] and [push]")

    def section(name: str, default: RetryPolicy) -> RetryPolicy:
        values = data.get(name, {})
        numeric_fields = {"retry_count", "interval_seconds", "deadline_seconds",
                          "attempt_timeout_seconds"}
        allowed = numeric_fields | ({"selected_topic_id", "topics"} if name == "news" else set())
        if not isinstance(values, dict) or set(values) - allowed:
            raise ValueError("invalid config section [" + name + "]")
        result = {}
        for field in numeric_fields:
            value = values.get(field, getattr(default, field))
            if type(value) is not int or value < (1 if field == "attempt_timeout_seconds" else 0):
                raise ValueError(name + "." + field + " must be "
                                 + ("a positive" if field == "attempt_timeout_seconds"
                                    else "a nonnegative") + " integer")
            result[field] = value
        return RetryPolicy(**result)

    defaults = RetryConfig()
    news = section("news", defaults.news)
    image = section("image", defaults.image)
    values = data.get("news", {})
    selected = values.get("selected_topic_id", defaults.selected_topic_id)
    if not isinstance(selected, str) or not TOPIC_ID.fullmatch(selected):
        raise ValueError("news.selected_topic_id must be a stable ID")
    raw_topics = values.get("topics")
    if raw_topics is None:
        topics = defaults.topics
    else:
        if not isinstance(raw_topics, list) or not raw_topics:
            raise ValueError("news.topics must be a nonempty TOML topic list")
        topics = []
        ids = set()
        for item in raw_topics:
            if not isinstance(item, dict) or set(item) != {"id", "label", "prompt"}:
                raise ValueError("each topic needs id, label and prompt")
            ident, label, prompt = item["id"], item["label"], item["prompt"]
            if not isinstance(ident, str) or not TOPIC_ID.fullmatch(ident) or ident in ids:
                raise ValueError("topic IDs must be unique, stable lowercase names")
            if (not isinstance(label, str) or not label.strip() or len(label) > 80
                    or not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 1200
                    or any(ord(char) < 32 and char not in "\t\n" for char in prompt)):
                raise ValueError("topic label or prompt is empty or too long")
            ids.add(ident)
            topics.append(Topic(ident, label.strip(), prompt.strip()))
        topics = tuple(topics)
    return RetryConfig(news, image, selected, topics)


def run_with_retry(operation, policy: RetryPolicy, retryable: tuple[type[Exception], ...],
                   stage: str, *, initial_attempts: int = 0,
                   initial_failure: str = "", on_failure=None):
    """Retry one stage; deadline 0 disables the extra elapsed-time cutoff."""
    started = time.monotonic()
    if initial_attempts >= policy.retry_count + 1:
        raise RuntimeError(stage + " already used all permitted attempts")
    failure = initial_failure
    for attempt in range(initial_attempts, policy.retry_count + 1):
        remaining = policy.deadline_seconds - (time.monotonic() - started)
        if policy.deadline_seconds and remaining <= 0:
            raise TimeoutError(stage + " retry deadline reached")
        timeout = (min(policy.attempt_timeout_seconds, remaining)
                   if policy.deadline_seconds else policy.attempt_timeout_seconds)
        try:
            result = operation(attempt, timeout, failure)
            if policy.deadline_seconds and time.monotonic() - started > policy.deadline_seconds:
                raise TimeoutError(stage + " retry deadline reached")
            return result
        except retryable as exc:
            failure = type(exc).__name__ + ": " + str(exc).replace("\n", " ")[:160]
            if on_failure is not None:
                on_failure(failure)
            if attempt == policy.retry_count:
                raise
            remaining = policy.deadline_seconds - (time.monotonic() - started)
            if policy.deadline_seconds and remaining <= policy.interval_seconds:
                raise TimeoutError(stage + " retry deadline reached") from exc
            if policy.interval_seconds:
                time.sleep(policy.interval_seconds)
    raise AssertionError("retry loop exhausted")
