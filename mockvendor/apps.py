from django.apps import AppConfig


class MockvendorConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "mockvendor"
    verbose_name = "Vendor Mock Server"
