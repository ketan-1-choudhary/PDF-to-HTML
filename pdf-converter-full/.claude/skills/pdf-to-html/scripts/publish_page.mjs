#!/usr/bin/env node
/**
 * publish_page.mjs — production publish step for pdf-converter-full.
 *
 * Replaces the local-folder save (save_page.mjs) when the skill runs
 * unattended inside an ephemeral container. It:
 *
 *   1. Uploads ONLY the true media assets referenced by <img> tags in the
 *      final content.html (raster files that resolve to a real file inside
 *      the work dir — e.g. assets/ or crops/) to S3 under the key:
 *          {S3_PREFIX?}{pdfId}/pages/{page}/assets/files/{asset-filename}
 *      and rewrites each uploaded <img src> to that key (optionally prefixed
 *      with ASSET_BASE_URL) so the stored HTML resolves against S3.
 *   2. Writes the final HTML + CSS to a single-table DynamoDB layout
 *      (PK=user_id, SK=type) touching two items in one transaction:
 *        - PAGE item  type=`PDFDATA#{pdfId}#{page}`  → html, css, status
 *        - PDF  item  type=`PDFDATA#{pdfId}`         → completed_pages += 1
 *      The counter only increments when the page first becomes "successful",
 *      so SQS redelivery / re-runs never double-count.
 *   3. Deletes the entire work dir (and any --extra-cleanup dirs) so nothing
 *      except the S3 assets and the DynamoDB items persists.
 *
 * All AWS names come from the environment so the container stays portable:
 *   Required:  S3_BUCKET
 *              DDB_TABLE_NAME
 *   Optional:  S3_PREFIX          key prefix (e.g. "renders/")
 *              S3_PUBLIC_BASE_URL / ASSET_BASE_URL   prefix for rewritten <img src>
 *              AWS_REGION         standard AWS SDK region resolution
 *
 * Usage:
 *   node publish_page.mjs --html <content.html>
 *                         --css <page.css>
 *                         --user-id <userId>
 *                         --pdf-id <pdfId>
 *                         --page <N>
 *                         --work-dir <work-dir>
 *                         [--extra-cleanup <dir> ...]
 *                         [--keep-work]        (skip cleanup; debugging only)
 *
 * Exit codes: 0 success; 2 bad/missing input or env; 1 unexpected failure.
 * On any failure it prints a JSON error object to stderr before exiting.
 */
import { readFile, writeFile, rm, stat } from "node:fs/promises";
import path from "node:path";
import { S3Client, PutObjectCommand } from "@aws-sdk/client-s3";
import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import { DynamoDBDocumentClient, TransactWriteCommand } from "@aws-sdk/lib-dynamodb";

const MIME_BY_EXT = {
  ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
  ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml",
};

function parseArgs(argv) {
  const out = { _extraCleanup: [] };
  for (let i = 2; i < argv.length; i++) {
    const k = argv[i];
    if (!k.startsWith("--")) continue;
    const key = k.slice(2);
    if (key === "keep-work") { out[key] = true; continue; }
    const v = argv[i + 1];
    if (key === "extra-cleanup") out._extraCleanup.push(v);
    else out[key] = v;
    i++;
  }
  return out;
}

function fail(error, code = 1, extra = {}) {
  console.error(JSON.stringify({ ok: false, error, ...extra }));
  process.exit(code);
}

function requireEnv(names) {
  const missing = names.filter((n) => !process.env[n] || !String(process.env[n]).trim());
  if (missing.length) {
    fail(`Missing required environment variable(s): ${missing.join(", ")}`, 2, { missingEnv: missing });
  }
}

async function fileExists(p) {
  try { return (await stat(p)).isFile(); } catch { return false; }
}

async function main() {
  const args = parseArgs(process.argv);
  for (const req of ["html", "css", "user-id", "pdf-id", "page", "work-dir"]) {
    if (!args[req]) fail(`Required: --${req}`, 2);
  }
  const page = parseInt(args.page, 10);
  if (!Number.isFinite(page) || page < 1) {
    fail("--page must be a positive integer", 2);
  }

  requireEnv(["S3_BUCKET", "DDB_TABLE_NAME"]);

  const bucket = process.env.S3_BUCKET;
  const table = process.env.DDB_TABLE_NAME;
  const keyPrefix = (process.env.S3_PREFIX || "").replace(/^\/+/, "");
  const assetBase = (process.env.S3_PUBLIC_BASE_URL || process.env.ASSET_BASE_URL || "").replace(/\/+$/, "");
  const userId = args["user-id"];
  const pdfId = args["pdf-id"];

  const htmlPath = path.resolve(args.html);
  const cssPath = path.resolve(args.css);
  const workDir = path.resolve(args["work-dir"]);
  const htmlDir = path.dirname(htmlPath);

  let html = await readFile(htmlPath, "utf8");
  const css = await readFile(cssPath, "utf8");

  // 1) Upload true media assets referenced by <img> and rewrite their src.
  const s3 = new S3Client({});
  const srcRe = /(<img[^>]*\bsrc=")([^"]+)("[^>]*>)/gi;
  const uploaded = [];
  const skipped = [];
  const matches = [...html.matchAll(srcRe)];
  for (const m of matches) {
    const src = m[2];
    if (/^(data:|https?:|\/\/)/i.test(src)) { skipped.push({ src, reason: "remote-or-inline" }); continue; }
    const ext = path.extname(src).toLowerCase();
    const mime = MIME_BY_EXT[ext];
    if (!mime) { skipped.push({ src, reason: "not-a-media-ext" }); continue; }
    const abs = path.resolve(htmlDir, src);
    // Only upload files that live inside the work dir (true run artifacts).
    if (!abs.startsWith(workDir + path.sep) && abs !== workDir) {
      skipped.push({ src, reason: "outside-work-dir" }); continue;
    }
    if (!(await fileExists(abs))) { skipped.push({ src, reason: "missing-file" }); continue; }

    const filename = path.basename(abs);
    const key = `${keyPrefix}${pdfId}/pages/${page}/assets/files/${filename}`;
    const body = await readFile(abs);
    await s3.send(new PutObjectCommand({ Bucket: bucket, Key: key, Body: body, ContentType: mime }));
    const newSrc = assetBase ? `${assetBase}/${key}` : key;
    html = html.split(src).join(newSrc);
    uploaded.push({ src, key, newSrc, bytes: body.length });
  }

  // 2) Write page html/css + bump the PDF completed_pages counter atomically.
  //    Single-table: PK=user_id, SK=type. The counter increments only when
  //    this page first transitions INTO "successful", so redelivery is safe.
  const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));
  const pdfSk = `PDFDATA#${pdfId}`;
  const pageSk = `PDFDATA#${pdfId}#${page}`;
  let counterIncremented = true;
  try {
    await ddb.send(new TransactWriteCommand({
      TransactItems: [
        {
          Update: {
            TableName: table,
            Key: { user_id: userId, type: pageSk },
            UpdateExpression: "SET html = :html, css = :css, #progress = :completed",
            ConditionExpression: "attribute_not_exists(#progress) OR #progress <> :completed",
            ExpressionAttributeNames: { "#progress": "progress" },
            ExpressionAttributeValues: { ":html": html, ":css": css, ":completed": "completed" },
          },
        },
        {
          Update: {
            TableName: table,
            Key: { user_id: userId, type: pdfSk },
            UpdateExpression: "ADD completed_pages :one",
            ExpressionAttributeValues: { ":one": 1 },
          },
        },
      ],
    }));
  } catch (err) {
    // Page already "successful" → whole transaction cancels → idempotent no-op.
    const cancelled = err?.name === "TransactionCanceledException";
    const pageCondFailed = cancelled &&
      Array.isArray(err.CancellationReasons) &&
      err.CancellationReasons[0]?.Code === "ConditionalCheckFailed";
    if (!pageCondFailed) throw err;
    counterIncremented = false;
  }

  // 3) Cleanup — nothing except S3 assets + the DynamoDB item may persist.
  const cleaned = [];
  const cleanupFailures = [];
  if (!args["keep-work"]) {
    const targets = [workDir, ...args._extraCleanup.filter(Boolean).map((d) => path.resolve(d))];
    for (const t of targets) {
      try { await rm(t, { recursive: true, force: true }); cleaned.push(t); }
      catch (e) { cleanupFailures.push({ dir: t, error: e?.message ?? String(e) }); }
    }
  }

  const result = {
    ok: cleanupFailures.length === 0,
    userId,
    pdfId,
    page,
    s3: { bucket, uploaded, skipped },
    dynamodb: {
      table,
      pdfItem: { user_id: userId, type: pdfSk },
      pageItem: { user_id: userId, type: pageSk },
      status: "successful",
      counterIncremented,
    },
    cleaned,
    cleanupFailures,
  };
  if (cleanupFailures.length) {
    console.error(JSON.stringify(result));
    process.exit(1);
  }
  console.log(JSON.stringify(result));
}

main().catch((err) => fail(err?.message ?? String(err), 1));
