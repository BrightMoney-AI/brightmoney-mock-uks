from django.apps import AppConfig


class DummyAutConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "dummy_aut"
    verbose_name = "Dummy Application Under Test"
