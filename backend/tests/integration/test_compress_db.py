"""Nén cột lớn trên Postgres thật: huấn luyện từ điển, nén lại."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import make_url, select, text

from jobcrawler.config import Settings
from jobcrawler.db import compress
from jobcrawler.db.compress import Codec, Kind, TrainOptions
from jobcrawler.db.models import JobPosting
from jobcrawler.resources import open_resources

from ..test_compress import samples

pytestmark = pytest.mark.integration


def dsn(url: str) -> str:
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


async def test_train_dictionary_then_recompress(integration_settings: Settings) -> None:
    source = f"compress-{uuid4().hex[:8]}"
    values = [json.loads(s) for s in samples(1200, seed=11)]
    async with open_resources(integration_settings, db_logs=False) as res:
        async with res.sessionmaker.begin() as s:
            s.add_all(
                JobPosting(source=source, external_id=str(i), url=f"https://x.vn/{i}", raw=v)
                for i, v in enumerate(values)
            )
        async with res.sessionmaker() as s:
            before = await compress.storage_stats(s, Kind.POSTING)

        options = TrainOptions(dict_size=8192, level=3, min_samples=1000, delay_seconds=0)
        now = datetime.now(UTC)
        [result] = await compress.train_dictionaries(
            res.sessionmaker, options, now=now, kinds=[Kind.POSTING]
        )
        assert result.adopted, result.reason
        assert result.dict_id is not None
        active = compress.CODEC.active(Kind.POSTING)
        assert active is not None
        assert active.id == result.dict_id

        written = await compress.recompress(res.sessionmaker, Kind.POSTING, limit=100_000)
        assert written >= len(values)
        assert await compress.recompress(res.sessionmaker, Kind.POSTING, limit=100_000) == 0

        async with res.sessionmaker() as s:
            heads = (
                await s.scalars(
                    text("SELECT substring(raw FROM 1 FOR 5) FROM job_postings WHERE source = :s"),
                    {"s": source},
                )
            ).all()
            assert {bytes(h) for h in heads} == {compress.header(result.dict_id)}
            rows = (
                await s.execute(
                    select(JobPosting.external_id, JobPosting.raw).where(
                        JobPosting.source == source
                    )
                )
            ).all()
            assert {int(e): r for e, r in rows} == dict(enumerate(values))
            after = await compress.storage_stats(s, Kind.POSTING)
            # Process khác (codec mới) nạp từ điển từ DB và đọc được.
            fresh = Codec()
            assert await compress.load_dictionaries(await s.connection(), fresh) >= 1
            blob = await s.scalar(
                text("SELECT raw FROM job_postings WHERE source = :s LIMIT 1"), {"s": source}
            )
            assert json.loads(fresh.decode(blob)) in values

        assert before.ratio is not None
        assert after.ratio is not None
        assert after.ratio > before.ratio * 1.5
        assert after.by_dict
        assert result.dict_id in after.by_dict

        # Lượt sau: từ điển còn mới thì không huấn luyện lại.
        [again] = await compress.train_dictionaries(
            res.sessionmaker, options, now=now, kinds=[Kind.POSTING]
        )
        assert not again.adopted
        assert again.reason == "từ điển còn mới"
