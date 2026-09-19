import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "cdp"
    postgres_user: str = "cdp_user"
    postgres_password: str = "changeme123"
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: str = "changeme123"
    server_key: str = "5f3b8a2c1d9e4f7a6b0c5d2e8f1a3b4c"
    kc_url: str = "http://mock-keycloak:8180"
    kc_realm: str = "hd"
    kc_client_id: str = "citizen-gw-exchanger"
    kc_client_secret: str = "exchanger-secret-xyz"
    apisix_admin_url: str = "http://localhost:9180"
    apisix_admin_key: str = "edd1c9f034335f136f87ad84b625c8f1"
    gateway_url: str = "http://citizen-gateway:9080"
    pat_max_per_app: int = 3
    pat_max_per_user: int = 10
    pat_max_days: int = 90
    default_rate_limit_tps: int = 10
    default_burst: int = 20
    default_daily_quota: int = 5000
    default_monthly_quota: int = 100000

    @property
    def db_dsn(self) -> str:
        return f"postgresql://{self.postgres_user}:{self.postgres_password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()
