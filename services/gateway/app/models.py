from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    role: Mapped[str] = mapped_column(String(16), default="member")  # admin | member
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    invite_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    prefix: Mapped[str] = mapped_column(String(16))  # display only, e.g. "umk_ab12cd"
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sha256 hex
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user: Mapped[User] = relationship(lazy="joined")


class Server(Base):
    __tablename__ = "servers"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(24), unique=True, index=True)  # tool namespace
    name: Mapped[str] = mapped_column(String(120))
    url: Mapped[str] = mapped_column(String(1000))
    auth_type: Mapped[str] = mapped_column(String(16), default="none")  # none | bearer | header
    header_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    credential_enc: Mapped[str | None] = mapped_column(Text, nullable=True)  # Fernet token
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ToolPolicy(Base):
    """Optional restriction: tool is usable only by roles >= min_role.
    No row = allowed for every authenticated user."""
    __tablename__ = "tool_policies"
    __table_args__ = (UniqueConstraint("server_id", "tool_name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    tool_name: Mapped[str] = mapped_column(String(200))
    min_role: Mapped[str] = mapped_column(String(16), default="admin")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    user_email: Mapped[str] = mapped_column(String(255), default="")
    source: Mapped[str] = mapped_column(String(16), default="mcp")  # mcp | chat
    server_slug: Mapped[str] = mapped_column(String(24), default="")
    tool: Mapped[str] = mapped_column(String(200), default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    outcome: Mapped[str] = mapped_column(String(16))  # success|error|timeout|denied|unreachable|not_found
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class RateCounter(Base):
    """Shared fixed-window counter (1-minute windows) so the per-user rate limit holds across replicas."""
    __tablename__ = "rate_counters"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # unix minute
    count: Mapped[int] = mapped_column(Integer, default=0)


class OAuthClient(Base):
    """Dynamically registered (RFC 7591) public client, e.g. Claude Code / Cursor."""
    __tablename__ = "oauth_clients"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    redirect_uris: Mapped[str] = mapped_column(Text)  # JSON list
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OAuthCode(Base):
    __tablename__ = "oauth_codes"
    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    redirect_uri: Mapped[str] = mapped_column(String(1000))
    challenge: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[int] = mapped_column(BigInteger)  # unix seconds


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    access_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    refresh_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    client_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[int] = mapped_column(BigInteger)
    refresh_expires_at: Mapped[int] = mapped_column(BigInteger)
    revoked_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    user: Mapped[User] = relationship(lazy="joined")


class HostedServer(Base):
    """An MCP server the platform runs in-cluster from a container image."""
    __tablename__ = "hosted_servers"
    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), unique=True)
    image: Mapped[str] = mapped_column(String(300))
    port: Mapped[int] = mapped_column(Integer, default=8000)
    created_by: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Store(Base):
    """A provisioned WooCommerce store (bonus). Desired state lives here; the provisioner reconciles it into
    Kubernetes. status: provisioning -> ready | failed; deleting -> (row removed when the namespace is gone)."""
    __tablename__ = "stores"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(24), unique=True, index=True)  # also the MCP tool namespace
    name: Mapped[str] = mapped_column(String(120))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    owner_email: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="provisioning", index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    deadline: Mapped[int] = mapped_column(BigInteger, default=0)  # unix seconds; provisioning fails after this
    secrets_enc: Mapped[str] = mapped_column(Text)  # Fernet(JSON) of generated DB/WP/REST/MCP secrets
    server_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Optimistic locking: the API (user clicks Delete) and the provisioner (marks Ready) may race on one row;
    # the loser's commit fails with StaleDataError and is retried on the next loop instead of overwriting.
    version: Mapped[int] = mapped_column(Integer, default=1)
    __mapper_args__ = {"version_id_col": version}
