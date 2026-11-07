from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./goldmaking.db"
    log_level: str = "INFO"
    max_download_bytes: int = Field(default=32 * 1024 * 1024, ge=1024, le=256 * 1024 * 1024)
    max_csv_rows: int = Field(default=500_000, ge=1, le=2_000_000)
    http_timeout_seconds: float = Field(default=30, gt=0, le=120)
    http_attempts: int = Field(default=3, ge=1, le=5)
    # Fréquence de vérification HTTP, indépendante de la publication du fournisseur.
    tsm_primary_interval_seconds: int = Field(default=1800, ge=300)
    tsm_market_interval_seconds: int = Field(default=3600, ge=300)
    tsm_region_interval_seconds: int = Field(default=10800, ge=300)
    tsm_primary_game_type: str = "forever"
    tsm_primary_region: str = "eu"
    tsm_primary_realm_slug: str = "normal-alliance"
    tsm_market_publication_seconds: int = Field(default=10800, ge=300)
    tsm_region_publication_seconds: int = Field(default=86400, ge=300)
