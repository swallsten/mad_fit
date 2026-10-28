from .models import StudioSettings


def studio_context(request):
    return {"studio": StudioSettings.load()}
