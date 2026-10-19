from pathlib import Path
from secrets import token_urlsafe

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    database_url: str = "postgresql+psycopg://quality:quality_demo@127.0.0.1:55432/quality"
    jwt_secret: str | None = Field(default=None, repr=False)
    demo_login_enabled: bool = True
    model_mode: str = "fake"
    agnes_api_key: str = Field(default="", repr=False)
    agnes_base_url: str = "https://apihub.agnes-ai.com/v1"
    agnes_model: str = "agnes-3.0-flash"
    siliconflow_api_key: str = Field(default="", repr=False)
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    upload_dir: Path = ROOT / "data" / "uploads"

    @model_validator(mode="after")
    def resolve_jwt_secret(self):
        if self.jwt_secret is None:
            if self.model_mode != "fake":
                # 真实模式拒绝静默随机会话密钥；请显式配置JWT_SECRET环境变量。
                raise ValueError("MODEL_MODE 非 fake 时必须显式配置 JWT_SECRET（环境变量或参数）")
            self.jwt_secret = token_urlsafe(48)
        return self
