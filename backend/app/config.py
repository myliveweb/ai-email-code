import os

from dotenv import find_dotenv, load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv(find_dotenv())


def allow_direct_localhost() -> None:
    """В окружении заданы http_proxy/https_proxy на внешний proxy, из-за которых
    запросы к локальному Supabase обрываются. Дописываем localhost в no_proxy,
    не затирая то, что там уже есть."""
    for var in ("NO_PROXY", "no_proxy"):
        current = os.environ.get(var, "")
        hosts = [h.strip() for h in current.split(",") if h.strip()]
        for host in ("localhost", "127.0.0.1"):
            if host not in hosts:
                hosts.append(host)
        os.environ[var] = ",".join(hosts)


allow_direct_localhost()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=find_dotenv(), extra="ignore")

    supabase_url: str
    supabase_key: str

    api_host: str = "127.0.0.1"
    api_port: int = 4000
    frontend_origins: list[str] = [
        "http://localhost:4100",
        "http://127.0.0.1:4100",
    ]


settings = Settings()
