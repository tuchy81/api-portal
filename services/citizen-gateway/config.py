from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: str = "changeme123"
    server_key: str = "5f3b8a2c1d9e4f7a6b0c5d2e8f1a3b4c"
    portal_backend_url: str = "http://portal-backend:8080"
    txs_url: str = "http://token-exchange-service:8081"
    internal_gw_url: str = "http://mock-internal-gw:8090"
    audit_batch_size: int = 100
    audit_flush_interval_s: int = 5
    neg_cache_ttl: int = 30

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()
