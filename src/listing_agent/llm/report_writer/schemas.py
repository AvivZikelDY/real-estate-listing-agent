"""Pydantic v2 output schema for the `report_writer` LLM module."""

from pydantic import BaseModel


class ReportOutput(BaseModel):
    """The structured output of the `report_writer` LLM module.

    Kept as a single prose field: the report is meant to be read by the
    buyer as one coherent, buyer-facing text covering every required
    section, not consumed as separate structured sub-fields.
    """

    report: str
