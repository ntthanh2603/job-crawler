"""API đánh giá trên golden set: `/api/eval…`."""

from __future__ import annotations

import dataclasses
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from jobcrawler.common.api import actor_of, res_of
from jobcrawler.modules.audit import audit
from jobcrawler.modules.companies import batch
from jobcrawler.modules.evaluate import evaluate
from jobcrawler.modules.evaluate.schemas import (
    CompanyEvalOut,
    EvalReportOut,
    EvalServerOut,
    SourceEvalOut,
)

router = APIRouter(prefix="/api", tags=["api"])

GOLDEN_DIR = Path("golden")


@router.get("/eval/server")
async def get_eval_server() -> EvalServerOut:
    """Có golden set trên máy chủ (thư mục golden/) không."""
    ok = (GOLDEN_DIR / "companies.csv").is_file() and (GOLDEN_DIR / "jobs.csv").is_file()
    return EvalServerOut(available=ok, path=str(GOLDEN_DIR))


@router.post("/eval")
async def post_eval(
    request: Request,
    companies: Annotated[UploadFile | None, File()] = None,
    jobs: Annotated[UploadFile | None, File()] = None,
    include_inactive: Annotated[bool, Form()] = False,
) -> EvalReportOut:
    """Precision/recall trên golden set tải lên (companies.csv + jobs.csv) hoặc thư mục golden/
    trên máy chủ."""
    res = res_of(request)
    uploaded = False
    with tempfile.TemporaryDirectory() as tmp:
        directory = GOLDEN_DIR
        if companies is not None and companies.filename and jobs is not None and jobs.filename:
            uploaded = True
            directory = Path(tmp)
            (directory / "companies.csv").write_bytes(await companies.read())
            (directory / "jobs.csv").write_bytes(await jobs.read())
        try:
            golden = evaluate.load_golden(directory)
        except (evaluate.GoldenError, batch.CsvFormatError, UnicodeDecodeError) as exc:
            raise HTTPException(400, f"Golden set không đọc được: {exc}") from None
    report = await evaluate.evaluate(res, golden, include_inactive=include_inactive)
    ids = [c.company_id for c in report.companies if c.company_id is not None]
    async with res.sessionmaker() as session:
        perf = await evaluate.latest_run_perf(session, ids)
    await audit.record_safely(
        res.sessionmaker,
        action=audit.EVAL_RUN,
        actor=actor_of(request),
        metadata={
            "golden": "upload" if uploaded else "server",
            "include_inactive": include_inactive,
            "companies": len(report.companies),
            "precision": report.precision,
            "recall": report.recall,
        },
    )
    return EvalReportOut(
        precision=report.precision,
        recall=report.recall,
        golden=report.golden,
        predicted=report.predicted,
        matched=report.matched,
        target_precision=evaluate.TARGET_PRECISION,
        target_recall=evaluate.TARGET_RECALL,
        perf=perf,
        companies=[
            CompanyEvalOut(**dataclasses.asdict(c), precision=c.precision, recall=c.recall)
            for c in report.companies
        ],
        sources=[
            SourceEvalOut(**dataclasses.asdict(s), precision=s.precision, recall=s.recall)
            for s in report.sources
        ],
        warnings=report.warnings,
        markdown=evaluate.markdown_report(report, perf=perf),
    )
