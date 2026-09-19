import secrets
from pydantic_settings import BaseSettings
from pydantic import Field

class Settings(BaseSettings):
    keycloak_url: str = "http://mock-keycloak:8180"
    keycloak_realm: str = "hd"
    exchanger_client_id: str = "citizen-gw-exchanger"
    exchanger_client_secret: str = "exchanger-secret-xyz"
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: str = "changeme123"
    jwt_cache_ttl: int = 240
    jwt_ttl: int = 300
    cb_failure_threshold: float = 0.5
    cb_window_size: int = 50
    cb_open_duration: int = 30
    retry_max_attempts: int = 2
    retry_wait_ms: int = 200
    request_timeout_s: float = 3.0
    node_id: str = Field(default_factory=lambda: secrets.token_hex(8))
    allowed_cidr: str = "0.0.0.0/0"

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()
