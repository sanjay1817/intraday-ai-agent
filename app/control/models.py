"""Trading Control Center domain models: roles/permissions, sessions,
alerting/notification vocabulary, control-panel commands, and audit
entries.

Built before `dto.py` for this phase specifically — the reverse of most
other engines in this project. `app.control`'s request DTOs (dispatch a
notification, issue a control command) are typed against this module's
enums, so the vocabulary has to exist first.

Some vocabulary here (`ControlCommand`, `AuditActionType`) has no
handler yet: `dashboard_service.py`/`metrics_service.py`/
`health_service.py`/`audit_service.py`/`configuration_service.py`/`api.py`
depend on the database/Redis/auth foundation and the other engines'
running logic, neither of which exists yet. Declaring the vocabulary now
means those services' eventual contracts won't drift once they're built.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class UserRole(StrEnum):
    """Role-based access levels for the Control Center."""

    ADMIN = "ADMIN"
    TRADER = "TRADER"
    ANALYST = "ANALYST"
    VIEWER = "VIEWER"


class Permission(StrEnum):
    """Fine-grained actions `permissions.py` gates by `UserRole`."""

    VIEW_DASHBOARD = "VIEW_DASHBOARD"
    VIEW_AUDIT_LOG = "VIEW_AUDIT_LOG"
    CONTROL_TRADING = "CONTROL_TRADING"
    EDIT_CONFIGURATION = "EDIT_CONFIGURATION"
    MANAGE_USERS = "MANAGE_USERS"
    ACKNOWLEDGE_ALERTS = "ACKNOWLEDGE_ALERTS"


class UserSession(BaseModel):
    """One authenticated user's active session.

    Tracked by `user_session.py`'s in-memory session store for now — a
    fully-functional single-process implementation, not a stand-in.
    Swapping in a Redis-backed store once `app.core` has a Redis
    connection is a drop-in replacement behind the same interface, not a
    rewrite (mirrors `app.utils.cache.LRUCache`'s role in the Indicator
    Engine).

    Deliberately has no `is_expired` property: "is this expired" needs
    "now", and hiding a clock read inside a frozen data model would bury
    a side-effecting dependency where callers wouldn't expect one.
    `user_session.py` compares `expires_at` against an explicit,
    injectable clock instead.
    """

    model_config = ConfigDict(frozen=True)

    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    username: str = Field(min_length=1)
    role: UserRole
    created_at: datetime
    expires_at: datetime
    last_active_at: datetime


class AlertSeverity(StrEnum):
    """How urgently an alert's recipients should treat it."""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class AlertChannel(StrEnum):
    """Where `alert_manager.py` can dispatch an alert.

    `SMS`/`WEB_PUSH` are declared here to match the spec but have no
    concrete sender in `alert_manager.py` yet — both require choosing a
    specific vendor (an SMS gateway; VAPID keys and a push service) the
    spec doesn't name, unlike Telegram/Slack/Discord/Email, which are
    themselves the integration target with one well-known API each.
    """

    TELEGRAM = "TELEGRAM"
    SLACK = "SLACK"
    DISCORD = "DISCORD"
    EMAIL = "EMAIL"
    SMS = "SMS"
    WEB_PUSH = "WEB_PUSH"


class AlertMessage(BaseModel):
    """One alert ready to be dispatched to a specific channel."""

    model_config = ConfigDict(frozen=True)

    channel: AlertChannel
    severity: AlertSeverity
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime


class NotificationType(StrEnum):
    """Every trading-platform event `notification_manager.py` can raise."""

    TRADE_OPENED = "TRADE_OPENED"
    TRADE_CLOSED = "TRADE_CLOSED"
    RISK_REJECTED = "RISK_REJECTED"
    BROKER_DOWN = "BROKER_DOWN"
    HIGH_LATENCY = "HIGH_LATENCY"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    MARKET_OPEN = "MARKET_OPEN"
    MARKET_CLOSE = "MARKET_CLOSE"
    AI_FAILURE = "AI_FAILURE"
    EXECUTION_FAILURE = "EXECUTION_FAILURE"


class NotificationEvent(BaseModel):
    """One instance of a `NotificationType` that occurred, before
    `notification_manager.py` routes it to any `AlertChannel`.
    """

    model_config = ConfigDict(frozen=True)

    notification_type: NotificationType
    severity: AlertSeverity
    title: str = Field(min_length=1)
    message: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime


class NotificationRoutingRule(BaseModel):
    """Configuration: which `AlertChannel`s a `NotificationType` fans out
    to, and the minimum severity that triggers dispatch at all.
    """

    model_config = ConfigDict(frozen=True)

    notification_type: NotificationType
    channels: frozenset[AlertChannel] = Field(min_length=1)
    minimum_severity: AlertSeverity = AlertSeverity.INFO


class ControlCommand(StrEnum):
    """Every control-panel action an operator can issue.

    Declared here as vocabulary; no handler executes these yet. Each
    command targets a live engine instance (Strategy/AI/Risk/Execution,
    or a broker's live connection) that doesn't exist as a running
    process yet — see this module's docstring.
    """

    PAUSE_TRADING = "PAUSE_TRADING"
    RESUME_TRADING = "RESUME_TRADING"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    CLOSE_ALL_POSITIONS = "CLOSE_ALL_POSITIONS"
    DISABLE_AI = "DISABLE_AI"
    DISABLE_STRATEGY = "DISABLE_STRATEGY"
    DISABLE_BROKER = "DISABLE_BROKER"
    DISABLE_EXECUTION = "DISABLE_EXECUTION"
    RESTART_WEBSOCKET = "RESTART_WEBSOCKET"
    RECONNECT_BROKER = "RECONNECT_BROKER"
    RELOAD_CONFIGURATION = "RELOAD_CONFIGURATION"


class AuditActionType(StrEnum):
    """Every category of action `audit_service.py` will eventually record."""

    LOGIN = "LOGIN"
    LOGOUT = "LOGOUT"
    CONFIGURATION_CHANGE = "CONFIGURATION_CHANGE"
    CONTROL_COMMAND = "CONTROL_COMMAND"
    EXECUTION = "EXECUTION"
    ALERT_DISPATCHED = "ALERT_DISPATCHED"
    AI_DECISION = "AI_DECISION"


class AuditEntry(BaseModel):
    """One permanent audit record.

    Shape defined now so `audit_service.py`'s eventual storage schema
    doesn't drift from what the rest of `app.control` already produces —
    not yet written anywhere durable, since that needs the database
    layer (not yet built).
    """

    model_config = ConfigDict(frozen=True)

    action_type: AuditActionType
    actor: str = Field(min_length=1)
    description: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime
