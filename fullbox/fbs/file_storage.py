from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible
from django.utils.functional import cached_property


@deconstructible
class FbsLabelFileSystemStorage(FileSystemStorage):
    def __init__(self):
        super().__init__(
            location=None,
            base_url=None,
            # The marketplace queue and the web application intentionally run
            # as separate users in the shared fullbox-runtime group.
            file_permissions_mode=0o660,
            directory_permissions_mode=0o2770,
        )

    @cached_property
    def base_location(self):
        return settings.FBS_LABEL_ROOT

    @cached_property
    def base_url(self):
        return None


fbs_label_storage = FbsLabelFileSystemStorage()
