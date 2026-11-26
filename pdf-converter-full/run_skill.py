#!/usr/bin/env python3
"""Lambda handler: consumes page-conversion jobs from Queue 2 (SQS event
source mapping) and runs pdf-converter-full via the Claude Agent SDK on
Bedrock for each one.

Required env vars (same contract as entrypoint.sh / SKILL.md):
  S3_BUCKET, DDB_TABLE_NAME          - consumed by scripts/publish_page.mjs
  AWS_REGION                        - Bedrock + AWS SDK region
Optional:
  PROJECT_ROOT                      - project directory containing .claude/skills
  ANTHROPIC_BEDROCK_MODEL_ID        - Bedrock model id (see DEFAULT_MODEL_ID)
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query

# Container image / layer mount point for SKILL.md + scripts/ + references/.
PROJECT_ROOT = Path(os.environ.get("PROJECT_ROOT", "/app"))
SKILL_ROOT = PROJECT_ROOT / ".claude" / "skills" / "pdf-to-html"
DEFAULT_MODEL_ID = "anthropic.claude-sonnet-4-5-20250929-v1:0"

REQUIRED_ENV = ("S3_BUCKET", "DDB_TABLE_NAME")


def build_prompt(pdf_path: str, page: int, user_id: str, pdf_id: str, bucket: str, work_dir: str) -> str:
    return (
        "Follow ./.claude/skills/pdf-to-html/SKILL.md exactly to convert one PDF page. "
        "There is no human — never ask questions.\n"
        "Fixed inputs:\n"
        f"- PDF path: {pdf_path}\n"
        f"- Page number (1-based): {page}\n"
        f"- User ID: {user_id}\n"
        f"- PDF ID: {pdf_id}\n"
        f"- S3 bucket: {bucket}\n"
        f"- Work directory: {work_dir}\n"
        "Run every command from the skill root, ./.claude/skills/pdf-to-html relative to the project root, "
        "so scripts/ and references/ resolve there. Use the supplied work directory exactly. "
        "The whole-book profile must be complete, and conversions for this PDF must run sequentially. "
        "The output must be fluid, reflowable HTML per references/output-contract.md "
        "('Fluid layout contract'): pixel-faithful at the source width, no horizontal page scroll down to 320px, "
        "no layout tables; flowcharts/trees keep their branches inside a .scroll-x wrapper. "
        "Finish by publishing with scripts/publish_page.mjs and cleaning up per Step 11, then stop."
    )


def require_env() -> None:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"missing env: {', '.join(missing)}")


def preflight_design_catalog(pdf_id: str, page: int, bucket: str, work_dir: str) -> None:
    result = subprocess.run(
        [
            sys.executable, str(SKILL_ROOT / "scripts" / "design_catalog.py"), "brief",
            "--pdf-id", pdf_id, "--page", str(page), "--work-dir", work_dir,
        ],
        cwd=str(SKILL_ROOT),
        env={**os.environ, "S3_BUCKET": bucket},
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"design catalog preflight failed for page {page}: {detail[:1000]}")


async def convert_page(pdf_path: str, page: int, user_id: str, pdf_id: str, bucket: str, work_dir: str) -> dict:
    require_env()
    if not bucket:
        raise ValueError("job requires bucket")
    preflight_design_catalog(pdf_id, page, bucket, work_dir)

    options = ClaudeAgentOptions(
        cwd=str(PROJECT_ROOT),
        setting_sources=["project"],
        skills=["pdf-to-html"],
        allowed_tools=["Read", "Write", "Bash"],
        permission_mode="bypassPermissions",
        # Read on a PNG returns it as one base64 JSON line; default 1 MB is too small.
        max_buffer_size=int(os.environ.get("SDK_MAX_BUFFER_BYTES", str(20 * 1024 * 1024))),
        env={
            **os.environ,
            "S3_BUCKET": bucket,
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "ANTHROPIC_MODEL": os.environ.get("ANTHROPIC_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID),
        },
    )

    prompt = build_prompt(pdf_path, page, user_id, pdf_id, bucket, work_dir)
    message_count = 0
    tool_call_count = 0
    received_result = False
    async for message in query(prompt=prompt, options=options):
        message_count += 1
        message_type = type(message).__name__
        content = getattr(message, "content", ())
        tool_names = [
            name for block in content
            if (name := getattr(block, "name", None))
        ]
        if tool_names:
            tool_call_count += len(tool_names)
            print(json.dumps({
                "event": "sdk_tool_use",
                "pdfId": pdf_id,
                "page": page,
                "tools": tool_names,
            }), file=sys.stderr)

        error = getattr(message, "error", None)
        is_error = getattr(message, "is_error", False)
        subtype = getattr(message, "subtype", None)
        if message_type == "ResultMessage":
            received_result = True
        if error or is_error:
            print(json.dumps({
                "event": "sdk_error",
                "pdfId": pdf_id,
                "page": page,
                "messageType": message_type,
                "error": str(error)[:1000] if error else None,
                "subtype": subtype,
            }), file=sys.stderr)
        elif message_type == "ResultMessage":
            print(json.dumps({
                "event": "sdk_result",
                "pdfId": pdf_id,
                "page": page,
                "subtype": subtype,
                "durationMs": getattr(message, "duration_ms", None),
                "numTurns": getattr(message, "num_turns", None),
            }), file=sys.stderr)
        if message_type == "ResultMessage" and (
            error or is_error or (isinstance(subtype, str) and subtype.startswith("error"))
        ):
            raise RuntimeError(f"conversion agent failed: {error or subtype or 'unknown error'}")

    if not received_result:
        raise RuntimeError("conversion agent ended without a final result")

    return {
        "ok": True,
        "userId": user_id,
        "pdfId": pdf_id,
        "page": page,
        "messages": message_count,
        "toolCalls": tool_call_count,
    }


async def process_job(user_id: str, pdf_id: str, page: int, bucket: str, pdf_uri: str) -> dict:
    if not (user_id and pdf_id and page and bucket and pdf_uri):
        raise ValueError("job requires user_id, pdf_id, page, bucket, and a PDF source")

    job_root = Path(tempfile.mkdtemp(prefix="pdfjob."))
    work_dir = job_root / f"page_{page}"
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        if pdf_uri.startswith("s3://"):
            pdf_path = job_root / "input.pdf"
            _download_from_s3(pdf_uri, pdf_path)
        else:
            pdf_path = Path(pdf_uri)

        return await convert_page(str(pdf_path), page, user_id, pdf_id, bucket, str(work_dir))
    finally:
        shutil.rmtree(job_root, ignore_errors=True)


def _download_from_s3(s3_uri: str, dest: Path) -> None:
    import boto3

    _, _, rest = s3_uri.partition("s3://")
    bucket, _, key = rest.partition("/")
    boto3.client("s3").download_file(bucket, key, str(dest))


def _job_from_record(record: dict) -> dict:
    try:
        return json.loads(record["body"])
    except (KeyError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid SQS record body: {record.get('body')}") from exc


def _sqs_client_and_queue_url():
    import boto3

    queue_url = os.environ.get("SQS_QUEUE_URL") or os.environ.get("SQS_PAGE_CONVERSION_QUEUE_URL")
    if not queue_url:
        raise RuntimeError("missing env: SQS_QUEUE_URL")
    return boto3.client("sqs"), queue_url


def _process_sqs_message(message: dict) -> None:
    job = json.loads(message["Body"])
    user_id = job.get("user_id") or job.get("userId")
    pdf_id = job.get("pdf_id") or job.get("pdfId")
    page = job.get("page")
    bucket = job.get("bucket")
    pdf_uri = job.get("pdfS3Uri") or job.get("pdfPath")
    asyncio.run(process_job(user_id, pdf_id, page, bucket, pdf_uri))


def main() -> None:
    """Run the long-lived ECS Queue 2 worker."""
    require_env()
    sqs, queue_url = _sqs_client_and_queue_url()
    idle_wait = 5

    print(f"Queue 2 worker started: {queue_url}", file=sys.stderr)
    while True:
        response = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20,
            VisibilityTimeout=int(os.environ.get("SQS_VISIBILITY_TIMEOUT", "1800")),
        )
        messages = response.get("Messages", [])
        if not messages:
            time.sleep(idle_wait)
            idle_wait = min(idle_wait * 2, 300)
            continue

        idle_wait = 5
        message = messages[0]
        receipt_handle = message["ReceiptHandle"]
        try:
            _process_sqs_message(message)
        except Exception as exc:
            print(f"job failed; leaving message for retry/DLQ: {exc}", file=sys.stderr)
            continue

        sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)
        print("message deleted", file=sys.stderr)


if __name__ == "__main__":
    main()
