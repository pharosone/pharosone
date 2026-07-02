"""Shared base model for all domain types."""

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Domain base: forbids unknown fields so malformed YAML fails loudly."""

    model_config = ConfigDict(extra="forbid")
