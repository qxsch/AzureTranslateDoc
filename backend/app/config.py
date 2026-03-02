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

    # Azure OpenAI – used for glossary-enhanced translation & LLM text translation
    azure_openai_endpoint: str = ""
    azure_openai_key: str = ""  # If set, uses API key auth; otherwise falls back to managed identity
    azure_openai_deployment: str = "gpt-5.2-chat"
    azure_openai_api_version: str = "2024-12-01-preview"

    # OpenAI API – fallback for local development (key-based)
    openai_api_key: str = ""
    openai_model: str = "gpt-5.2-chat"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
