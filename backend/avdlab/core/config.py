"""Application settings (env / .env)."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AVDLAB_", env_file=".env", extra="ignore")

    # paths
    data_dir: Path = Field(default=Path("/data"), description="Root for labs/, images/, jobs/, templates cache")
    templates_dir: Path = Field(default=Path(__file__).resolve().parents[3] / "templates")

    # database: sqlite by default, postgres via AVDLAB_DATABASE_URL=postgresql+asyncpg://user:pw@db/avdlab
    database_url: str = "sqlite+aiosqlite:////data/avdlab.db"

    # auth (single admin)
    admin_user: str = "admin"
    admin_password: str = "change-me"
    session_secret: str = "change-me-session-secret"
    session_max_age: int = 60 * 60 * 24 * 7

    # secret store key (urlsafe base64 32 bytes, e.g. `python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"`).
    # If empty, a key is generated at first start and stored at <data_dir>/secret.key (fine for single-node use).
    secret_key: str = ""

    # remote agent image + containerlab
    agent_image: str = "ghcr.io/aristanetworks/avd/universal:python3.11-avd-v6.4.0"
    clab_image: str = "ghcr.io/srl-labs/clab:latest"
    # 0.10.0: dedicated ANTA image for `anta psirt` (the AVD universal image bundles an older ANTA without it); entrypoint = anta
    anta_image: str = "ghcr.io/aristanetworks/anta:1.10.0"
    remote_root: str = "/opt/avdlab"
    # linux client containers (Module 5): needs iperf3, iproute2 (vlan/bond), ping; per-lab knob `client_image` wins
    host_image: str = "ghcr.io/srl-labs/network-multitool:latest"

    # pinned toolchain
    avd_version: str = "6.4.0"
    anta_version: str = "1.10.0"

    # host monitoring sampler (hosts/monitor.py); disabled → no background SSH at all
    monitor_enabled: bool = True
    monitor_interval: float = 20.0

    # phase 5: about/meta + feedback
    copyright: str = "© 2026 Dietrich De Bruyne — Systems Engineer, Arista Networks"  # AVDLAB_COPYRIGHT
    git_sha: str = ""  # AVDLAB_GIT_SHA, baked into the image at build time (build arg)
    build_date: str = ""  # AVDLAB_BUILD_DATE, baked into the image at build time (build arg)
    meta_dir: Path | None = None  # dir holding VERSION/CHANGELOG.md/LICENSE/THIRD_PARTY_NOTICES.md (default: repo root)
    feedback_github_repo: str = ""  # AVDLAB_FEEDBACK_GITHUB_REPO=owner/repo + secret `github_token` → GitHub issues

    # phase 6 (A): update check + self-update (meta/update.py)
    update_source: str = ""  # AVDLAB_UPDATE_SOURCE: github:<owner>/<repo> (Releases API) or a manifest URL; empty = off
    update_mode: Literal["source", "image", "manual"] = "source"  # AVDLAB_UPDATE_MODE
    update_check_interval: float = 24.0  # AVDLAB_UPDATE_CHECK_INTERVAL hours; 0 = no background check
    deploy_dir: str = ""  # AVDLAB_DEPLOY_DIR: HOST path of the deploy/ dir (compose sets it to ${PWD})
    update_docker_socket: Path = Path("/var/run/docker.sock")  # must be mounted for self-update
    update_helper_image: str = "docker:27-cli"  # detached helper container running git/compose

    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:8080"]

    # v0.8.0 (stream B): public deployment behind Caddy (deploy/Caddyfile, compose profile `public`)
    public_url: str = ""  # AVDLAB_PUBLIC_URL=https://avdlab.example.com → appended to cors_origins, Secure cookie
    secure_cookies: bool | None = None  # AVDLAB_SECURE_COOKIES; None = Secure iff public_url starts with https://
    trust_proxy_headers: bool = False  # AVDLAB_TRUST_PROXY_HEADERS: honour X-Forwarded-Proto/For (only behind a proxy)

    # v0.9.0 (stream A): defaults for the admin-editable app settings (core/app_settings.py; a DB value wins)
    app_title: str = "avdlab"  # AVDLAB_APP_TITLE
    github_url: str = ""  # AVDLAB_GITHUB_URL: GitHub button in the top bar / dashboard / register page
    support_email: str = ""  # AVDLAB_SUPPORT_EMAIL: shown on the register and About pages
    registration_enabled: bool = True  # AVDLAB_REGISTRATION_ENABLED: public "request an account" form
    intro_video_url: str = "/intro/avdlab-intro.mp4"  # AVDLAB_INTRO_VIDEO_URL; empty hides the dashboard player
    feedback_mail_to: str = "dietrich@arista.com"  # AVDLAB_FEEDBACK_MAIL_TO (comma-separated list allowed)
    feedback_mail_enabled: bool = True  # AVDLAB_FEEDBACK_MAIL_ENABLED
    registration_mail_to: str = ""  # AVDLAB_REGISTRATION_MAIL_TO; empty = feedback_mail_to
    smtp_host: str = ""  # AVDLAB_SMTP_HOST; empty = e-mail disabled
    smtp_port: int = 587  # AVDLAB_SMTP_PORT (465 = implicit TLS)
    smtp_user: str = ""  # AVDLAB_SMTP_USER
    smtp_password: str = ""  # AVDLAB_SMTP_PASSWORD (the secret `smtp_password` set in the UI wins)
    smtp_starttls: bool = True  # AVDLAB_SMTP_STARTTLS (ignored on port 465)
    smtp_from: str = ""  # AVDLAB_SMTP_FROM; empty = avdlab@<host of public_url or localhost>

    # v0.10.0: AI lab designer (avdlab/ai/) — env defaults of the admin "AI assistant" settings (a DB value wins)
    ai_default_provider: str = ""  # AVDLAB_AI_DEFAULT_PROVIDER: anthropic | openai ("" = no shared model)
    ai_default_base_url: str = ""  # AVDLAB_AI_DEFAULT_BASE_URL (empty = the provider's public API)
    ai_default_model: str = ""  # AVDLAB_AI_DEFAULT_MODEL
    ai_default_api_key: str = ""  # AVDLAB_AI_DEFAULT_API_KEY (the secret `ai_default_api_key` set in the UI wins)
    ai_shared_allowed: bool = False  # AVDLAB_AI_SHARED_ALLOWED: users without their own link may use the shared model
    ai_rate_limit: int = 30  # AVDLAB_AI_RATE_LIMIT: design calls per user per hour
    ai_timeout: float = 180.0  # AVDLAB_AI_TIMEOUT: seconds per provider request (large models with tool use can take a while)

    @model_validator(mode="after")
    def _add_public_origin(self) -> "Settings":
        origin = self.public_url.strip().rstrip("/")
        if origin and origin not in self.cors_origins:
            self.cors_origins = [*self.cors_origins, origin]
        return self

    @property
    def session_https_only(self) -> bool:
        """Mark the session cookie `Secure` (explicit setting wins, else derived from the public URL)."""
        if self.secure_cookies is not None:
            return self.secure_cookies
        return self.public_url.strip().lower().startswith("https://")

    @property
    def labs_dir(self) -> Path:
        return self.data_dir / "labs"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"


settings = Settings()
