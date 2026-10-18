"""M5 trên Postgres thật: chuẩn hoá → xác minh → gộp → lưu, chạy lại idempotent, export."""

from __future__ import annotations

import dataclasses
import json
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from jobcrawler.config import Settings, get_settings
from jobcrawler.db.models import Company, Job, JobPosting
from jobcrawler.fakes import FakeFetcher
from jobcrawler.pipeline import dedup, store
from jobcrawler.pipeline.verify import Verdict
from jobcrawler.resources import Resources, open_resources
from jobcrawler.schemas import CompanyIn, JobOut, JobStatus
from jobcrawler.services import export as export_service
from jobcrawler.services import maintenance
from jobcrawler.services.companies import process_company
from jobcrawler.sources import boards

from ..boards_fake import FakeBoard, fake_site
from ..jobs_fixtures import NOW, normalized, raw_job
from .test_board_index_db import ACME, JOBS, indexer, reset, settings_for

pytestmark = pytest.mark.integration


async def company_jobs(res: Resources, company_id: UUID) -> list[Job]:
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


async def test_reclean_jobs_applies_new_cleaning_rules(
    integration_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(boards, "registry", lambda: {FakeBoard.name: FakeBoard()})
    async with open_resources(settings_for(integration_settings), db_logs=False) as res:
        company_id = await reset(res)
        async with res.sessionmaker.begin() as s:
            await s.execute(delete(Job).where(Job.company_id == company_id))
        await indexer(res, fake_site(JOBS)).run()
        offline = dataclasses.replace(res, http=FakeFetcher())  # pyright: ignore[reportArgumentType]
        await process_company(offline, CompanyIn(name=ACME))
        job_id = (await company_jobs(res, company_id))[0].id
        # Nội dung lưu từ trước khi có luật làm sạch mới.
        dirty = "- **Tuổi****:** Từ 18\n\n- Tên / Chức danh: Vũ Tuấn - HRBP"
        async with res.sessionmaker.begin() as s:
            await s.execute(update(Job).where(Job.id == job_id).values(description_md=dirty))

        dry = await maintenance.reclean_jobs(res.sessionmaker, dry_run=True)
        assert dry.changed >= 1
        async with res.sessionmaker() as s:
            assert await s.scalar(select(Job.description_md).where(Job.id == job_id)) == dirty

        done = await maintenance.reclean_jobs(res.sessionmaker)
        assert done.changed == dry.changed
        async with res.sessionmaker() as s:
            cleaned = await s.scalar(select(Job.description_md).where(Job.id == job_id))
        assert cleaned == "- **Tuổi:** Từ 18\n\n- Tên / Chức danh: [liên hệ đã ẩn] - HRBP"
        # Chạy lại: nội dung đã sạch không đổi.
        assert (await maintenance.reclean_jobs(res.sessionmaker)).changed == 0


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


CAREERS = Path(__file__).parents[1] / "fixtures" / "careers"
BASE_JOB = (
    "https://careers.base.vn/job/hn-business-development-executive-junior-b2b-sales-1yoe-29675"
)


async def test_careers_details_are_reused_on_the_next_run(
    integration_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lượt sau dùng lại chi tiết tin đã tải (JOB_REUSE_DAYS), không mở lại trang tin."""
    monkeypatch.setattr(boards, "registry", dict)
    fetcher = FakeFetcher()
    for url, name in [
        ("https://base.vn/", "base_vn/home.html"),
        ("https://careers.base.vn/", "base_vn/careers.html"),
        ("https://careers.base.vn/alljobs", "base_vn/alljobs.html"),
        (BASE_JOB, "base_vn/job_29675.html"),
    ]:
        fetcher.add_file(url, CAREERS / name)
    company = CompanyIn(name=f"Base Reuse {uuid4().hex[:6]}", website="https://careers.base.vn/")
    async with open_resources(settings_for(integration_settings), db_logs=False) as res:
        live = dataclasses.replace(res, http=fetcher)  # pyright: ignore[reportArgumentType]

        async def posting() -> JobPosting:
            async with res.sessionmaker() as s:
                row = await s.scalar(
                    select(JobPosting).where(
                        JobPosting.url == BASE_JOB, JobPosting.company_id == first.company_id
                    )
                )
            assert row is not None
            return row

        first = await process_company(live, company)
        assert first.jobs_accepted == 1
        fetched_at = (await posting()).fetched_at
        assert fetched_at is not None
        assert [u for _, u, _ in fetcher.calls].count(BASE_JOB) == 1

        second = await process_company(live, company)
        assert second.jobs_accepted == 1
        assert [u for _, u, _ in fetcher.calls].count(BASE_JOB) == 1  # không mở lại
        again = await posting()
        assert again.fetched_at == fetched_at  # mốc tải giữ nguyên: hết hạn dùng lại thì tải lại
        assert again.last_seen_at > fetched_at


async def test_deferred_posting_keeps_its_job_open(integration_settings: Settings) -> None:
    """Tin LLM chưa kịp chấm (hoãn): không ghi là bị loại, job cũ của nó vẫn mở."""
    async with open_resources(integration_settings, db_logs=False) as res:
        async with res.sessionmaker.begin() as s:
            company = Company(input_name=f"ACME deferred {uuid4().hex[:6]}")
            s.add(company)
            await s.flush()
            company_id = company.id
        job = normalized(raw_job(external_id=f"d-{uuid4().hex[:8]}"), board=False)

        async def save(verdict: Verdict, *, complete: bool) -> store.SaveResult:
            clusters = dedup.cluster_jobs([job]) if verdict.accepted else []
            async with res.sessionmaker.begin() as s:
                return await store.save_company_jobs(
                    s,
                    company_id=company_id,
                    company_name="ACME",
                    jobs=[job],
                    verdicts=[verdict],
                    clusters=clusters,
                    now=NOW,
                    complete=complete,
                )

        await save(Verdict(accepted=True, confidence=0.9, reasons=["TEST"]), complete=True)
        [row] = await company_jobs(res, company_id)
        deferred = Verdict(accepted=True, confidence=0.6)
        deferred.defer("không kịp / vượt giới hạn LLM")
        saved = await save(deferred, complete=False)
        assert saved.postings == 0
        assert saved.removed == 0
        [again] = await company_jobs(res, company_id)
        assert (again.id, again.status) == (row.id, "active")
        async with res.sessionmaker() as s:
            posting = await s.scalar(
                select(JobPosting).where(JobPosting.external_id == job.external_id)
            )
        assert posting is not None
        assert (posting.accepted, posting.job_id) == (True, row.id)


async def test_reposted_job_reuses_existing_job_and_duplicates_are_merged(
    integration_settings: Settings,
) -> None:
    """Không có job trùng: tin đăng lại với mã mới ở lượt sau gắn vào job đã có; job trùng có từ
    trước (dữ liệu trước 0006) được `dedupe_jobs` gộp; DB chặn hai job y hệt cùng đang mở."""
    async with open_resources(integration_settings, db_logs=False) as res:
        async with res.sessionmaker.begin() as s:
            company = Company(input_name=f"ACME dedup {uuid4().hex[:6]}")
            s.add(company)
            await s.flush()
            company_id = company.id
        suffix = uuid4().hex[:8]
        first = normalized(raw_job(external_id=f"5883-{suffix}", locations=[]), board=False)
        repost = normalized(
            raw_job(
                external_id=f"5920-{suffix}",
                url=f"https://careers.acme.vn/jobs/5920-{suffix}",
                locations=[],
            ),
            board=False,
        )

        async def save(jobs: list, *, complete: bool = False) -> store.SaveResult:
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

        await save([first])
        [job] = await company_jobs(res, company_id)
        assert job.fingerprint
        # Lượt sau chỉ còn bản đăng lại (mã mới), lượt partial: vẫn một job, cùng id.
        await save([repost])
        rows = await company_jobs(res, company_id)
        assert [(r.id, r.status) for r in rows] == [(job.id, "active")]
        async with res.sessionmaker() as s:
            active = await s.scalar(select(Company.active_jobs).where(Company.id == company_id))
        assert active == 1

        # DB chặn job thứ hai y hệt đang mở.
        async with res.sessionmaker() as s:
            s.add(
                Job(
                    company_id=company_id,
                    cluster_key=f"dup-{suffix}",
                    fingerprint=job.fingerprint,
                    title=job.title,
                    title_normalized=job.title_normalized,
                    company_name="ACME",
                    description_md=job.description_md,
                    canonical_source="careers",
                    confidence=0.9,
                )
            )
            with pytest.raises(IntegrityError):
                await s.commit()

        # Dữ liệu cũ chưa có dấu vân tay: một bản sao → gộp vào job cũ nhất.
        async with res.sessionmaker.begin() as s:
            await s.execute(update(Job).where(Job.id == job.id).values(fingerprint=None))
            s.add(
                Job(
                    company_id=company_id,
                    cluster_key=f"old-{suffix}",
                    title=job.title,
                    title_normalized=job.title_normalized,
                    company_name="ACME",
                    description_md=job.description_md,
                    requirements_md=job.requirements_md,
                    locations=job.locations,
                    canonical_source="careers",
                    confidence=0.9,
                    source_urls=["https://careers.acme.vn/jobs/old"],
                )
            )
        report = await maintenance.dedupe_jobs(res.sessionmaker)
        assert report.merged >= 1
        statuses = {r.cluster_key: r.status for r in await company_jobs(res, company_id)}
        assert statuses[job.cluster_key] == "active"
        assert statuses[f"old-{suffix}"] == "removed"
        [kept] = [r for r in await company_jobs(res, company_id) if r.id == job.id]
        assert "https://careers.acme.vn/jobs/old" in kept.source_urls


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
