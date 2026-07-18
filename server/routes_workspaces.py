"""Workspace (library) list/create — see workspaces.py for the storage model."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import workspaces
from errors import ConfigError
from server.copy import friendly

router = APIRouter()


class CreateWorkspaceRequest(BaseModel):
    name: str


@router.get("/api/workspaces")
def list_workspaces():
    return {"workspaces": workspaces.list_workspaces()}


@router.post("/api/workspaces")
def create_workspace(req: CreateWorkspaceRequest):
    try:
        return workspaces.create_workspace(req.name)
    except ConfigError as e:
        raise HTTPException(422, friendly(e, "Creating that workspace"))
