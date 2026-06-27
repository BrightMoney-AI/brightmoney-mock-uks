"""DB router: dummy_aut models live in the 'aut' database, everything else in 'default'.

This mirrors the design's separation — the mock server and the Application Under
Test keep their own databases; the test runner verifies the AUT's database
independently of the mock's.
"""
from __future__ import annotations


class AutRouter:
    aut_app = "dummy_aut"
    aut_db = "aut"

    def db_for_read(self, model, **hints):
        if model._meta.app_label == self.aut_app:
            return self.aut_db
        return "default"

    def db_for_write(self, model, **hints):
        if model._meta.app_label == self.aut_app:
            return self.aut_db
        return "default"

    def allow_relation(self, obj1, obj2, **hints):
        return True

    def allow_migrate(self, db, app_label, **hints):
        if app_label == self.aut_app:
            return db == self.aut_db
        return db == "default"
