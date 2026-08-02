from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://reelmaker:reelmaker@localhost:5432/reelmaker"
    redis_url: str = "redis://localhost:6379/0"

    llm_base_url: str = "http://localhost:11434/v1"
    llm_model: str = "qwen3:14b"
    # Enrichment/judge calls use this model — defaults to local qwen3:14b.
    # Set NVIDIA_API_KEY to route these calls to NVIDIA NIM instead.
    llm_enrichment_model: str = "qwen3:14b"

    # NVIDIA NIM — if set, enrichment + judge calls use the hosted API.
    # Get a key at build.nvidia.com. Leave blank to use local Ollama.
    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_enrichment_model: str = "nvidia/nemotron-3-super-120b-a12b"
    # Set to route main guide generation through NVIDIA NIM instead of local Ollama.
    # Recommended: same model as enrichment, or a capable alternative.
    nvidia_generation_model: str = "nvidia/nemotron-3-super-120b-a12b"
    use_nvidia_for_generation: bool = False

    # "edge" (edge-tts, default) | "kokoro" (Python <3.13) | "silent" (no audio)
    tts_provider: str = "edge"
    asset_store_dir: str = "./data/assets"
    video_store_dir: str = "./data/videos"

    pexels_api_key: str = ""
    pixabay_api_key: str = ""
    huggingface_api_key: str = ""
    huggingface_image_model: str = "black-forest-labs/FLUX.1-schnell"
    huggingface_video_model: str = "Lightricks/LTX-Video"

    # Hard ceiling on paid (NVIDIA NIM) LLM calls per reel. A single successful run
    # already makes up to ~16 (structured path falling through to standard, worst
    # case); this exists to stop a stuck Celery retry loop or a pathological prompt
    # from compounding that across repeated task attempts with no limit at all.
    max_paid_llm_calls_per_reel: int = 20

    # Cost estimation for StageEvent.cost_usd — USD per 1M tokens for NVIDIA NIM calls.
    # NIM rates vary by model and billing plan and change over time, so we don't ship a
    # guessed number here: both default to 0 (cost tracking inert) until set from your
    # actual NVIDIA billing plan. Local Ollama calls are always free (self-hosted).
    nvidia_price_per_1m_input_tokens: float = 0.0
    nvidia_price_per_1m_output_tokens: float = 0.0

    # 32-byte URL-safe base64 key for Fernet credential encryption.
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Leave blank in dev — tokens are stored as plaintext with a warning.
    credentials_key: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
