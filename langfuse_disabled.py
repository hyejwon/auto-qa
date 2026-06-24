from contextlib import contextmanager


class _NoopObservation:
    trace_id = None

    def update(self, *args, **kwargs):
        return None


class _NoopLangfuse:
    @contextmanager
    def start_as_current_observation(self, *args, **kwargs):
        yield _NoopObservation()

    def create_score(self, *args, **kwargs):
        return None

    def flush(self, *args, **kwargs):
        return None


def get_client():
    return _NoopLangfuse()
