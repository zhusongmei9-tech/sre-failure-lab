import os
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_seconds: int = 60
    redis_timeout_seconds: float = 0.5
    lab_enabled: bool = False
    fallback_enabled: bool = False
    lab_token: str = field(default="", repr=False)

    def __post_init__(self):
        if self.cache_ttl_seconds <= 0:
            raise ValueError("CACHE_TTL_SECONDS must be positive")
        if not 0 < self.redis_timeout_seconds <= 30:
            raise ValueError("REDIS_TIMEOUT_SECONDS must be between 0 and 30")
        if self.lab_enabled and not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", self.lab_token):
            raise ValueError("LAB_TOKEN must be 32..256 URL-safe characters when lab is enabled")

    @classmethod
    def from_env(cls):
        return cls(
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            cache_ttl_seconds=int(os.getenv("CACHE_TTL_SECONDS", "60")),
            redis_timeout_seconds=float(os.getenv("REDIS_TIMEOUT_SECONDS", "0.5")),
            lab_enabled=os.getenv("LAB_ENABLED", "false").lower() == "true",
            fallback_enabled=os.getenv("FALLBACK_ENABLED", "false").lower() == "true",
            lab_token=os.getenv("LAB_TOKEN", ""),
        )
