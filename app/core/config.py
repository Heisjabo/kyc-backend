from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = "Tuesday KYC Platform"
    APP_VERSION: str = "0.1.0"
    ENVIRONMENT: str = "development"
    ENABLE_DOCS: bool = True

    DATABASE_URL: str = "postgresql+psycopg://kyc:kyc@localhost:5432/kyc"
    REDIS_URL: str = "redis://localhost:6379/0"


settings = Settings()
