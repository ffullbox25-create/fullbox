from django.template.backends.django import DjangoTemplates


class FbsTestDjangoTemplates(DjangoTemplates):
    def check(self, **kwargs):
        return []

    def get_templatetag_libraries(self, custom_libraries):
        return {
            "static": "django.templatetags.static",
            **custom_libraries,
        }
