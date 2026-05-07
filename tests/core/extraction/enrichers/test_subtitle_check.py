"""Tests for SubtitleCheckEnricher — text_evidence on transcript writes."""

from unittest.mock import patch

import pytest

from kebi.core.extraction.enrichers.subtitle_check import (
    SubtitleCheckEnricher,
)
from kebi.core.extraction.types import (
    ExtractionContext,
    Medium,
    Producer,
)


@pytest.fixture
def enricher() -> SubtitleCheckEnricher:
    return SubtitleCheckEnricher()


class TestSubtitleCheckEnricher:
    async def test_appends_text_evidence_on_transcript_write(
        self,
        enricher: SubtitleCheckEnricher,
    ) -> None:
        ctx = ExtractionContext(
            url="https://youtube.com/watch?v=abc", user_id="u1"
        )
        # _strip_vtt is called on the raw vtt; mock _download_subtitles
        # to return a vtt that strips to a non-empty transcript.
        raw_vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nLoved Fuji Ramen tonight\n"
        with patch.object(
            enricher, "_download_subtitles", return_value=raw_vtt
        ):
            await enricher.enrich(ctx)
        assert ctx.transcript == "Loved Fuji Ramen tonight"
        assert len(ctx.text_evidence) == 1
        assert ctx.text_evidence[0].producer == Producer.SUBTITLE_CHECK
        assert ctx.text_evidence[0].medium == Medium.TRANSCRIPT
        assert ctx.text_evidence[0].snippet == "Loved Fuji Ramen tonight"

    async def test_no_evidence_when_transcript_already_set(
        self,
        enricher: SubtitleCheckEnricher,
    ) -> None:
        ctx = ExtractionContext(
            url="https://youtube.com/watch?v=abc",
            user_id="u1",
            transcript="already there",
        )
        with patch.object(enricher, "_download_subtitles") as d:
            await enricher.enrich(ctx)
        d.assert_not_called()
        assert ctx.text_evidence == []

    async def test_no_evidence_when_photo_post(
        self,
        enricher: SubtitleCheckEnricher,
    ) -> None:
        ctx = ExtractionContext(
            url="https://tiktok.com/v/photo",
            user_id="u1",
            is_photo_post=True,
        )
        with patch.object(enricher, "_download_subtitles") as d:
            await enricher.enrich(ctx)
        d.assert_not_called()
        assert ctx.text_evidence == []

    async def test_no_evidence_when_no_subtitles_returned(
        self,
        enricher: SubtitleCheckEnricher,
    ) -> None:
        ctx = ExtractionContext(
            url="https://youtube.com/watch?v=abc", user_id="u1"
        )
        with patch.object(enricher, "_download_subtitles", return_value=None):
            await enricher.enrich(ctx)
        assert ctx.transcript is None
        assert ctx.text_evidence == []
