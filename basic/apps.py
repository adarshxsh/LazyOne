from django.apps import AppConfig


class BasicConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'basic'

    def ready(self):
        from .services import feed  # noqa: F401  registers friendship cache invalidation
