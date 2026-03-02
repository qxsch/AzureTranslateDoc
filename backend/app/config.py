from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    azure_translator_endpoint: str = "https://api.cognitive.microsofttranslator.com"
    azure_translator_key: str = ""
    azure_translator_region: str = "eastus"
    use_managed_identity: bool = False
    max_file_size_mb: int = 10

    # Storage Account for batch Document Translation (PDF, DOCX, …)
    azure_storage_account_name: str = ""
    azure_storage_connection_string: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
