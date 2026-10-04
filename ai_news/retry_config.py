"""Optional, local retry settings for the two Codex stages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
import tomllib


@dataclass(frozen=True)
class RetryPolicy:
    retry_count: int
    interval_seconds: int
    deadline_seconds: int
    attempt_timeout_seconds: int


@dataclass(frozen=True)
class RetryConfig:
    news: RetryPolicy = RetryPolicy(2, 0, 0, 180)
    image: RetryPolicy = RetryPolicy(2, 0, 0, 300)


def load_retry_config(path: Path | None = None) -> RetryConfig:
    """Read project-root ``config``; a missing file uses the documented defaults."""
    path = path or Path(__file__).resolve().parents[1] / "config"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return RetryConfig()
    if not isinstance(data, dict) or set(data) - {"news", "image"}:
        raise ValueError("config supports only [news] and [image]")

    def section(name: str, default: RetryPolicy) -> RetryPolicy:
        values = data.get(name, {})
        fields = {"retry_count", "interval_seconds", "deadline_seconds",
                  "attempt_timeout_seconds"}
        if not isinstance(values, dict) or set(values) - fields:
            raise ValueError("invalid config section [" + name + "]")
        result = {}
        for field in fields:
            value = values.get(field, getattr(default, field))
            if type(value) is not int or value < (1 if field == "attempt_timeout_seconds" else 0):
                raise ValueError(name + "." + field + " must be "
                                 + ("a positive" if field == "attempt_timeout_seconds"
                                    else "a nonnegative") + " integer")
            result[field] = value
        return RetryPolicy(**result)

    defaults = RetryConfig()
    return RetryConfig(section("news", defaults.news), section("image", defaults.image))


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
