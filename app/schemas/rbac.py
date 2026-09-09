import uuid

from pydantic import BaseModel, Field


class RoleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    parent_role_id: uuid.UUID | None = None


class RoleOut(BaseModel):
    id: uuid.UUID
    name: str
    parent_role_id: uuid.UUID | None
    is_system: bool

    class Config:
        from_attributes = True


class AttachPermission(BaseModel):
    permission_code: str = Field(min_length=1, max_length=150)


class AssignRole(BaseModel):
    user_id: uuid.UUID
    role_id: uuid.UUID
    org_unit_id: uuid.UUID | None = None


class SetRoleParent(BaseModel):
    new_parent_id: uuid.UUID
