"""模型聚合导出(保证 Base.metadata 收集全部表)。"""
from app.models.account import Account, UserProfile
from app.models.agent_thread import AgentThread
from app.models.audit import AuditLog, LoginLog
from app.models.credential import AuthCredential
from app.models.rbac import AccountRole, Menu, Permission, Role, RoleMenu, RolePermission

__all__ = [
    "Account",
    "AgentThread",
    "UserProfile",
    "AuthCredential",
    "Role",
    "Permission",
    "Menu",
    "AccountRole",
    "RolePermission",
    "RoleMenu",
    "LoginLog",
    "AuditLog",
]
