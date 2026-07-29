"""Stub — audio classification is not shipped in this package."""


class AudioClassifier:
    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "utils.audio_classifier.AudioClassifier is not available in "
            "wallfly_schedule_scraper. Use a different schedule_type or LLM mode."
        )
