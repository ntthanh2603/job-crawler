"""M5 trên Postgres thật: chuẩn hoá → xác minh → gộp → lưu, chạy lại idempotent, export."""

from __future__ import annotations

import dataclasses
import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, update
from typer.testing import CliRunner

from jobcrawler.config import Settings, get_settings
from jobcrawler.db.models import Company, Job, JobPosting
from jobcrawler.fakes import FakeFetcher
from jobcrawler.pipeline import dedup, store
from jobcrawler.pipeline.verify import Verdict
from jobcrawler.resources import Resources, open_resources
from jobcrawler.schemas import CompanyIn, JobOut, JobStatus
from jobcrawler.services import export as export_service
from jobcrawler.services.companies import process_company
from jobcrawler.sources import boards

from ..boards_fake import FakeBoard, fake_site
from ..jobs_fixtures import NOW, normalized, raw_job
from .test_board_index_db import ACME, JOBS, indexer, reset, settings_for

pytestmark = pytest.mark.integration


async def company_jobs(res: Resources, company_id: int) -> list[Job]:
    async with res.sessionmaker() as s:
        rows = await s.scalars(select(Job).where(Job.company_id == company_id).order_by(Job.id))
        return list(rows)


async def test_process_company_saves_clean_jobs_idempotently(
    integration_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(boards, "registry", lambda: {FakeBoard.name: FakeBoard()})
    async with open_resources(settings_for(integration_settings), db_logs=False) as res:
        company_id = await reset(res)
        async with res.sessionmaker.begin() as s:
            await s.execute(delete(Job).where(Job.company_id == company_id))
        await indexer(res, fake_site(JOBS)).run()
        offline = dataclasses.replace(res, http=FakeFetcher())  # pyright: ignore[reportArgumentType]

        first = await process_company(offline, CompanyIn(name=ACME))
        assert (first.jobs_found, first.jobs_accepted) == (2, 2)
        assert sorted(j.title for j in first.jobs) == ["Kế toán", "Lập trình viên"]
        job = first.jobs[0]
        assert job.company_id == company_id
        assert job.locations[0].province == "Hà Nội"
        assert job.canonical_source == FakeBoard.name
        assert job.confidence >= 0.8
        assert "EMPLOYER_MATCH" in job.verification_reasons
        assert job.status is JobStatus.ACTIVE

        rows = await company_jobs(res, company_id)
        assert len(rows) == 2
        async with res.sessionmaker() as s:
            postings = (
                await s.scalars(select(JobPosting).where(JobPosting.source == FakeBoard.name))
            ).all()
        acme = [p for p in postings if p.company_id == company_id]
        assert len(acme) == 2
        assert all(p.accepted and p.job_id in {r.id for r in rows} for p in acme)
        assert all(p.verification_reasons and p.verified_at for p in acme)

        # Chạy lại: không sinh bản ghi mới, cùng id/khoá cụm.
        second = await process_company(offline, CompanyIn(name=ACME))
        assert second.jobs_accepted == 2
        again = await company_jobs(res, company_id)
        assert [(r.id, r.cluster_key) for r in again] == [(r.id, r.cluster_key) for r in rows]
        assert {j.first_seen_at for j in second.jobs} == {j.first_seen_at for j in first.jobs}

        # Export đúng schema JobOut.
        result = await export_service.export_jobs(
            res, fmt="jsonl", out=tmp_path, companies=[str(company_id)]
        )
        lines = (tmp_path / "jobs.jsonl").read_text(encoding="utf-8").splitlines()
        assert result.count == len(lines) == 2
        assert {JobOut.model_validate_json(line).title for line in lines} == {
            "Kế toán",
            "Lập trình viên",
        }
        by_name = await export_service.export_jobs(
            res, fmt="csv", out=tmp_path / "acme.csv", companies=[ACME]
        )
        assert by_name.count == 2

        # Tin bị gỡ khỏi trang (index đánh `removed`): lượt chạy đủ nguồn đánh job `removed`.
        async with res.sessionmaker.begin() as s:
            await s.execute(
                update(JobPosting)
                .where(JobPosting.source == FakeBoard.name, JobPosting.url.like("%ke-toan%"))
                .values(status=JobStatus.REMOVED.value)
            )
        third = await process_company(offline, CompanyIn(name=ACME))
        assert [j.title for j in third.jobs] == ["Lập trình viên"]
        status = {r.title: r.status for r in await company_jobs(res, company_id)}
        assert status == {"Kế toán": "removed", "Lập trình viên": "active"}
        active = await export_service.export_jobs(res, fmt="jsonl", out=tmp_path, companies=[ACME])
        assert active.count == 1
        everything = await export_service.export_jobs(
            res, fmt="jsonl", out=tmp_path, companies=[ACME], include_inactive=True
        )
        assert everything.count == 2


async def test_cluster_key_is_kept_when_canonical_changes(integration_settings: Settings) -> None:
    """Lượt sau tìm thêm trang careers cho cùng job: vẫn là một dòng `jobs`, bản chuẩn đổi."""
    async with open_resources(integration_settings, db_logs=False) as res:
        async with res.sessionmaker.begin() as s:
            company = Company(input_name=f"ACME store {uuid4().hex[:6]}")
            s.add(company)
            await s.flush()
            company_id = company.id
        suffix = uuid4().hex[:8]
        on_board = normalized(
            raw_job(
                source="topcv",
                external_id=f"t-{suffix}",
                url=f"https://www.topcv.vn/viec-lam/ke-toan/{suffix}.html",
            ),
            board=True,
        )
        on_site = normalized(raw_job(external_id=f"c-{suffix}"), board=False)

        async def save(jobs: list, *, complete: bool = True) -> store.SaveResult:
            verdicts = [Verdict(accepted=True, confidence=0.9, reasons=["TEST"]) for _ in jobs]
            async with res.sessionmaker.begin() as s:
                return await store.save_company_jobs(
                    s,
                    company_id=company_id,
                    company_name="ACME",
                    jobs=jobs,
                    verdicts=verdicts,
                    clusters=dedup.cluster_jobs(jobs),
                    now=NOW,
                    complete=complete,
                )

        first = await save([on_board])
        [row] = await company_jobs(res, company_id)
        assert first.jobs[0].canonical_source == "topcv"

        second = await save([on_board, on_site])
        [again] = await company_jobs(res, company_id)
        assert (again.id, again.cluster_key) == (row.id, row.cluster_key)
        assert second.jobs[0].canonical_source == "careers"
        assert second.jobs[0].source_urls == [on_site.url, on_board.url]

        # Lượt `partial` không thấy tin nào: giữ nguyên; lượt đủ nguồn: job bị gỡ.
        await save([], complete=False)
        assert (await company_jobs(res, company_id))[0].status == "active"
        removed = await save([], complete=True)
        assert removed.removed == 1
        assert (await company_jobs(res, company_id))[0].status == "removed"

        # Tin bị loại ở lượt sau (vd. hết hạn): job của nó không còn mở, tin bỏ liên kết.
        third = await save([on_board, on_site])
        assert third.jobs[0].status is JobStatus.ACTIVE
        async with res.sessionmaker.begin() as s:
            rejected = await store.save_company_jobs(
                s,
                company_id=company_id,
                company_name="ACME",
                jobs=[on_site],
                verdicts=[Verdict(accepted=False, confidence=0.9, reasons=["EXPIRED: test"])],
                clusters=[],
                now=NOW + timedelta(days=60),
                complete=False,
            )
        assert rejected.jobs == []
        [final] = await company_jobs(res, company_id)
        assert final.status == "expired"  # expires_at 2026-11-30 < NOW + 60 ngày
        async with res.sessionmaker() as s:
            posting = await s.scalar(
                select(JobPosting).where(JobPosting.external_id == on_site.external_id)
            )
        assert posting is not None
        assert (posting.accepted, posting.job_id, posting.status) == (False, None, "expired")


def test_cli_export(
    integration_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from jobcrawler.entrypoints.cli import app

    monkeypatch.setenv("DATABASE_URL", integration_settings.database_url)
    monkeypatch.setenv("REDIS_PREFIX", f"jctest:{uuid4().hex[:8]}")
    get_settings.cache_clear()
    try:
        runner = CliRunner()
        ok = runner.invoke(app, ["export", "--format", "jsonl", "--out", str(tmp_path), "--all"])
        missing = runner.invoke(app, ["export", "--company", "Không Có Doanh Nghiệp Này 123"])
        bad = runner.invoke(app, ["export", "--format", "xml"])
    finally:
        get_settings.cache_clear()
    assert ok.exit_code == 0, ok.output
    path = tmp_path / "jobs.jsonl"
    assert f"→ {path}" in ok.output
    for line in path.read_text(encoding="utf-8").splitlines():
        JobOut.model_validate(json.loads(line))
    assert missing.exit_code == 2
    assert "không tìm thấy doanh nghiệp" in missing.output
    assert bad.exit_code == 2
