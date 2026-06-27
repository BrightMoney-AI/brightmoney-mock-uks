"""Dummy AUT models — written to the 'aut' database (its OWN db).

This stands in for a real KYC service so the CSV-driven test runner has a real
database to verify (design §6 verify_db). Intentionally tiny.
"""
from __future__ import annotations

from django.db import models


class Enrollment(models.Model):
    flow_id = models.CharField(max_length=120, unique=True)  # idempotency key
    bright_uid = models.CharField(max_length=120, blank=True)
    test_path = models.CharField(max_length=120, blank=True)
    decision = models.CharField(max_length=20, default="PENDING")  # KycFlow.status analog
    persona_inquiry_id = models.CharField(max_length=120, blank=True, null=True)
    escalation_type = models.CharField(max_length=40, blank=True)
    escalation_status = models.CharField(max_length=20, blank=True)  # SAME_INPUT/DIFFERENT_INPUT
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "enrollment"

    def __str__(self) -> str:
        return f"{self.flow_id}:{self.decision}"
