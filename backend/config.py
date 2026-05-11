from pydantic import BaseSettings, Field


class Settings(BaseSettings):
    anthropic_api_key: str = Field(default="", env="ANTHROPIC_API_KEY")
    redis_url: str = Field(default="redis://redis:6379/0", env="REDIS_URL")
    cors_origin: str = Field(default="http://localhost:3000", env="CORS_ORIGIN")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


settings = Settings()
