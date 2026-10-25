from __future__ import annotations

from dataclasses import dataclass

from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.integrations.llm.base import LLMProvider
from app.services.dispatcher import TaskDispatcher


@dataclass(slots=True)
class GraphDeps:
    session: AsyncSession
    llm: LLMProvider
    settings: Settings
    dispatcher: TaskDispatcher


def deps(config: RunnableConfig) -> GraphDeps:
    return config["configurable"]["deps"]
