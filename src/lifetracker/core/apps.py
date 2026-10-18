from django.apps import AppConfig, apps
from django.db.models.signals import post_delete


class CoreConfig(AppConfig):
    name = "lifetracker.core"
    label = "core"
    verbose_name = "Core"

    def ready(self):
        # Imported here: it imports models, which aren't loaded until now.
        from lifetracker.core.attachments import (  # noqa: PLC0415
            database_storage_file_fields,
            delete_stored_files,
        )

        # Connected per model, not for every sender: a receiver for a model
        # turns off Django's fast (signal-less) bulk delete for it.
        for model in apps.get_models():
            if database_storage_file_fields(model):
                post_delete.connect(
                    delete_stored_files,
                    sender=model,
                    dispatch_uid=f"core_delete_stored_files_{model._meta.label}",
                )
