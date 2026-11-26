#!/usr/bin/env node
/**
 * responsive_dom_checks.mjs
 *
 * Deterministic responsive QA for final HTML output.
 * - Runs DOM/layout checks at desktop/tablet/phone viewports.
 * - Treats desktop as structural baseline.
 * - fluid (default): horizontal page overflow, horizontally clipped content and
 *   crushed text columns fail at every width. Only shape-preserving blocks
 *   ([data-layout="diagram"|"table"] inside a .scroll-x scroller) may scroll.
 * - fidelity-first (legacy): horizontal scroll on small screens is allowed.
 *
 * Usage:
 *   node responsive_dom_checks.mjs \
 *     --html <path-to-content.html> \
 *     --work-dir <work-dir> \
 *     [--mode fluid|fidelity-first] \
 *     [--out-json <path>]
 */
import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import puppeteer from "puppeteer";

function parseArgs(argv) {
  const out = {};
  for (let i = 2; i < argv.length; i++) {
    const k = argv[i];
    if (!k.startsWith("--")) continue;
    const v = argv[i + 1];
    out[k.slice(2)] = v;
    i++;
  }
  return out;
}

const DEFAULT_VIEWPORTS = [
  { id: "desktop", width: 1366, height: 900, minFontPx: 11 },
  { id: "tablet", width: 768, height: 1024, minFontPx: 10 },
  { id: "phone", width: 390, height: 844, minFontPx: 10 },
];

function clamp01(v) {
  return Math.max(0, Math.min(1, v));
}

function countGridColumns(gridTemplateColumns) {
  const raw = String(gridTemplateColumns || "").trim();
  if (!raw || raw === "none") return 1;
  // Computed styles usually expand repeat() into explicit tracks.
  return raw.split(/\s+/).filter(Boolean).length;
}

function summarizeViewport(vp, mode) {
  const issues = [];
  const warnings = [];
  const fluid = mode !== "fidelity-first";

  const overflowCount = vp.overflowOffenders.length;
  const clippedCount = vp.clippedTextBlocks.length;
  const clippedHCount = vp.clippedHorizontally.length;
  const crampedCount = vp.crampedTextColumns.length;
  const distortedCount = vp.distortedImages.length;
  const overlapCount = vp.overlapPairs.length;
  const tinyTextCount = vp.tinyTextNodes.length;
  const structuralCount = vp.structuralIssues.length;
  const globalScroll = Boolean(vp.globalHorizontalScroll);

  if (clippedCount > 0) issues.push(`clipped text blocks: ${clippedCount}`);
  if (distortedCount > 0) issues.push(`distorted images: ${distortedCount}`);
  if (overlapCount > 0) issues.push(`overlapping blocks: ${overlapCount}`);
  if (structuralCount > 0) issues.push(`structural mismatches: ${structuralCount}`);

  // In fidelity-first mode, global horizontal scroll can be intentional.
  if (overflowCount > 0) {
    if (!fluid && vp.id !== "desktop") {
      warnings.push(`horizontal overflow offenders: ${overflowCount} (allowed in fidelity-first if intentional)`);
    } else {
      issues.push(`horizontal overflow offenders: ${overflowCount}`);
    }
  }

  if (globalScroll) {
    (fluid ? issues : warnings).push("page scrolls horizontally");
  }
  if (clippedHCount > 0) {
    (fluid ? issues : warnings).push(`content clipped past viewport edge: ${clippedHCount}`);
  }
  if (crampedCount > 0) {
    (fluid ? issues : warnings).push(`cramped text columns (layout did not stack): ${crampedCount}`);
  }

  if (tinyTextCount > 0) {
    warnings.push(`tiny text nodes: ${tinyTextCount}`);
  }

  const penalty =
    clippedCount * 0.08 +
    distortedCount * 0.1 +
    overlapCount * 0.08 +
    structuralCount * 0.1 +
    (!fluid && vp.id !== "desktop" ? 0 : overflowCount * 0.04) +
    (fluid ? clippedHCount * 0.06 + crampedCount * 0.04 + (globalScroll ? 0.15 : 0) : 0) +
    tinyTextCount * 0.01;

  return {
    pass: issues.length === 0,
    issues,
    warnings,
    score: clamp01(1 - penalty),
  };
}

async function collectMetrics(page, vp, mode) {
  return page.evaluate((ctx) => {
    const { viewportId, minFontPx, fluid } = ctx;
    const root = document.querySelector(".page-root") || document.body;
    const flowchartHintRe = /(flow(chart)?|tree|diagram|schema|mind[-_ ]?map|concept[-_ ]?map)/i;
    const viewportWidth = window.innerWidth;
    const SHAPE_SELECTOR = '[data-layout="diagram"],[data-layout="table"],table';

    // A horizontal scroller is only legitimate when it hosts a shape-preserving block.
    const isAllowedScroller = (el) => {
      if (!(el instanceof HTMLElement)) return false;
      const ox = window.getComputedStyle(el).overflowX;
      if (ox !== "auto" && ox !== "scroll") return false;
      return el.classList.contains("scroll-x") || Boolean(el.querySelector(SHAPE_SELECTOR));
    };
    const insideAllowedScroller = (el) => {
      for (let cur = el.parentElement; cur && cur !== document.body; cur = cur.parentElement) {
        if (isAllowedScroller(cur)) return true;
      }
      return false;
    };
    const isDecoration = (el) => {
      if (el.closest('[aria-hidden="true"]')) return true;
      const pos = window.getComputedStyle(el).position;
      return (pos === "absolute" || pos === "fixed") && !(el.innerText || "").trim();
    };

    const isVisible = (el) => {
      const style = window.getComputedStyle(el);
      if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity || "1") === 0) {
        return false;
      }
      const r = el.getBoundingClientRect();
      return r.width > 0.5 && r.height > 0.5;
    };

    const cssPath = (el) => {
      if (!(el instanceof Element)) return "";
      const parts = [];
      let cur = el;
      let depth = 0;
      while (cur && cur.nodeType === 1 && depth < 5) {
        let part = cur.tagName.toLowerCase();
        if (cur.id) {
          part += `#${cur.id}`;
          parts.unshift(part);
          break;
        }
        const cls = [...cur.classList].slice(0, 2).join(".");
        if (cls) part += `.${cls}`;
        const parent = cur.parentElement;
        if (parent) {
          const siblings = [...parent.children].filter((n) => n.tagName === cur.tagName);
          if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(cur) + 1})`;
        }
        parts.unshift(part);
        cur = cur.parentElement;
        depth++;
      }
      return parts.join(" > ");
    };

    const overflowOffenders = [];
    for (const el of document.querySelectorAll("body *")) {
      if (!(el instanceof HTMLElement)) continue;
      if (!isVisible(el)) continue;
      const tag = el.tagName;
      if (tag === "HTML" || tag === "BODY") continue;
      if (el.classList.contains("page-root")) continue;
      if (el.clientWidth <= 0) continue;
      if (isDecoration(el) || isAllowedScroller(el) || insideAllowedScroller(el)) continue;
      const overBy = el.scrollWidth - el.clientWidth;
      if (overBy > 8) {
        overflowOffenders.push({
          selector: cssPath(el),
          overBy: Math.round(overBy),
          clientWidth: Math.round(el.clientWidth),
          scrollWidth: Math.round(el.scrollWidth),
        });
        if (overflowOffenders.length >= 30) break;
      }
    }

    // .page-root { overflow-x: hidden } silently cuts off fixed-width content on narrow screens.
    const clippedHorizontally = [];
    for (const el of root.querySelectorAll("p,li,h1,h2,h3,h4,h5,h6,td,th,figcaption,img,figure,table,pre,blockquote")) {
      if (!(el instanceof HTMLElement)) continue;
      if (!isVisible(el) || isDecoration(el) || insideAllowedScroller(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.right > viewportWidth + 2 || r.left < -2) {
        clippedHorizontally.push({
          selector: cssPath(el),
          left: Math.round(r.left),
          right: Math.round(r.right),
          viewportWidth,
        });
        if (clippedHorizontally.length >= 30) break;
      }
    }

    // Long text squeezed into a narrow column below desktop means a multi-column layout failed to stack.
    const crampedTextColumns = [];
    if (viewportWidth < 1024) {
      const minColumnPx = Math.min(200, viewportWidth * 0.5);
      for (const el of root.querySelectorAll("p,li,dd,blockquote")) {
        if (!(el instanceof HTMLElement)) continue;
        if (!isVisible(el) || insideAllowedScroller(el) || el.closest("[data-layout]")) continue;
        const txt = (el.innerText || "").trim();
        if (txt.length < 60) continue;
        const r = el.getBoundingClientRect();
        if (r.width < minColumnPx) {
          crampedTextColumns.push({ selector: cssPath(el), widthPx: Math.round(r.width), sample: txt.slice(0, 60) });
          if (crampedTextColumns.length >= 30) break;
        }
      }
    }

    const clippedTextBlocks = [];
    for (const el of root.querySelectorAll("*")) {
      if (!(el instanceof HTMLElement)) continue;
      if (!isVisible(el)) continue;
      const txt = (el.innerText || "").trim();
      if (txt.length < 20) continue;
      const style = window.getComputedStyle(el);
      const overflowY = style.overflowY;
      const clips = overflowY === "hidden" || overflowY === "clip";
      const fixedHeight = style.height !== "auto" || style.maxHeight !== "none";
      if (clips && fixedHeight && el.scrollHeight - el.clientHeight > 4) {
        clippedTextBlocks.push({
          selector: cssPath(el),
          clippedBy: Math.round(el.scrollHeight - el.clientHeight),
          overflowY,
        });
        if (clippedTextBlocks.length >= 30) break;
      }
    }

    const tinyTextNodes = [];
    for (const el of root.querySelectorAll("p,li,td,th,figcaption,span,div,h1,h2,h3,h4,h5,h6")) {
      if (!(el instanceof HTMLElement)) continue;
      if (!isVisible(el)) continue;
      const txt = (el.innerText || "").trim();
      if (!txt) continue;
      const fs = parseFloat(window.getComputedStyle(el).fontSize || "0");
      if (Number.isFinite(fs) && fs > 0 && fs < minFontPx) {
        tinyTextNodes.push({ selector: cssPath(el), fontPx: Number(fs.toFixed(2)), sample: txt.slice(0, 80) });
        if (tinyTextNodes.length >= 40) break;
      }
    }

    const distortedImages = [];
    for (const el of root.querySelectorAll("img")) {
      if (!(el instanceof HTMLImageElement)) continue;
      if (!isVisible(el)) continue;
      if (!el.complete || !el.naturalWidth || !el.naturalHeight) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width < 24 || rect.height < 24) continue;
      const nat = el.naturalWidth / el.naturalHeight;
      const disp = rect.width / rect.height;
      const drift = Math.abs((disp - nat) / nat);
      if (drift > 0.15) {
        distortedImages.push({
          selector: cssPath(el),
          naturalAspect: Number(nat.toFixed(4)),
          displayAspect: Number(disp.toFixed(4)),
          drift: Number(drift.toFixed(4)),
        });
      }
      if (distortedImages.length >= 20) break;
    }

    const blocks = [];
    for (const el of root.querySelectorAll("section,article,figure,table,ul,ol,div,p,h1,h2,h3,h4,h5,h6")) {
      if (!(el instanceof HTMLElement)) continue;
      if (!isVisible(el)) continue;
      const style = window.getComputedStyle(el);
      if (style.display === "inline") continue;
      const r = el.getBoundingClientRect();
      if (r.width * r.height < 900) continue;
      blocks.push({
        el,
        selector: cssPath(el),
        left: r.left,
        top: r.top,
        right: r.right,
        bottom: r.bottom,
      });
      if (blocks.length >= 160) break;
    }

    const overlapPairs = [];
    for (let i = 0; i < blocks.length; i++) {
      const a = blocks[i];
      for (let j = i + 1; j < blocks.length; j++) {
        const b = blocks[j];
        if (a.el.contains(b.el) || b.el.contains(a.el)) continue;
        const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
        const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        if (w > 8 && h > 8) {
          overlapPairs.push({
            a: a.selector,
            b: b.selector,
            overlapW: Math.round(w),
            overlapH: Math.round(h),
          });
          if (overlapPairs.length >= 30) break;
        }
      }
      if (overlapPairs.length >= 30) break;
    }

    const headingSeq = Array.from(root.querySelectorAll("h1,h2,h3,h4,h5,h6"))
      .map((h) => h.tagName.toLowerCase())
      .slice(0, 80);

    const flowchartCandidates = [];
    for (const el of root.querySelectorAll("section,article,div,figure")) {
      if (!(el instanceof HTMLElement)) continue;
      if (!isVisible(el)) continue;
      const style = window.getComputedStyle(el);
      const display = style.display;
      const directChildren = Array.from(el.children).filter((x) => x instanceof HTMLElement);
      const declared = el.getAttribute("data-layout") === "diagram";
      if (!declared && directChildren.length < 3) continue;
      const hasHint = flowchartHintRe.test(
        [el.id, el.className, el.getAttribute("data-role"), el.getAttribute("data-kind"), el.getAttribute("data-layout")]
          .filter(Boolean)
          .join(" "),
      );
      const headingCount = el.querySelectorAll("h1,h2,h3,h4,h5,h6").length;
      // In fluid mode undeclared grids (e.g. card rows) are expected to stack, so only
      // declared/hinted diagrams must keep their shape.
      const isLikelyTree = declared ||
        (hasHint && (display === "grid" || display === "flex")) ||
        (!fluid && display === "grid" && directChildren.length >= 3 && headingCount >= 3);
      if (!isLikelyTree) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width < 220 || rect.height < 120) continue;
      // Widest visual row of direct children = branch count, for both grid and flex.
      const tops = directChildren.filter(isVisible).map((c) => Math.round(c.getBoundingClientRect().top)).sort((a, b) => a - b);
      let maxRow = tops.length ? 1 : 0;
      for (let i = 0, start = 0; i < tops.length; i++) {
        if (tops[i] - tops[start] > 6) start = i;
        maxRow = Math.max(maxRow, i - start + 1);
      }
      flowchartCandidates.push({
        selector: cssPath(el),
        display,
        declared,
        columns: display === "grid" ? Math.max(countGridColumns(style.gridTemplateColumns), maxRow) : maxRow,
        childCount: directChildren.length,
        headingCount,
        aspect: Number((rect.width / Math.max(1, rect.height)).toFixed(4)),
        width: Number(rect.width.toFixed(2)),
        height: Number(rect.height.toFixed(2)),
      });
      if (flowchartCandidates.length >= 20) break;
    }

    const sectionCount = root.querySelectorAll("[data-section-id], section, article, .section").length;
    const listCount = root.querySelectorAll("ul,ol").length;
    const tableCount = root.querySelectorAll("table").length;
    const textLength = (root.innerText || "").replace(/\s+/g, " ").trim().length;

    return {
      viewportId,
      viewport: { width: window.innerWidth, height: window.innerHeight },
      pageRootPresent: Boolean(document.querySelector(".page-root")),
      globalHorizontalScroll: document.documentElement.scrollWidth > window.innerWidth + 1,
      overflowOffenders,
      clippedHorizontally,
      crampedTextColumns,
      clippedTextBlocks,
      tinyTextNodes,
      distortedImages,
      overlapPairs,
      structure: {
        headingSeq,
        headingCount: headingSeq.length,
        sectionCount,
        listCount,
        tableCount,
        textLength,
      },
      flowchartCandidates,
    };
  }, { viewportId: vp.id, minFontPx: vp.minFontPx, fluid: mode !== "fidelity-first" });
}

function addStructuralDiff(viewMetric, desktopMetric) {
  const issues = [];
  const d = desktopMetric.structure;
  const v = viewMetric.structure;

  if (!desktopMetric.pageRootPresent && viewMetric.pageRootPresent) {
    // no-op, still valid
  }

  const textCoverage = d.textLength > 0 ? v.textLength / d.textLength : 1;
  if (textCoverage < 0.88) {
    issues.push({ type: "textCoverageLow", textCoverage: Number(textCoverage.toFixed(4)) });
  }

  if (v.headingCount < Math.max(1, Math.floor(d.headingCount * 0.85))) {
    issues.push({ type: "headingLoss", desktop: d.headingCount, current: v.headingCount });
  }

  if (d.headingSeq.length > 0) {
    const prefixLen = Math.min(d.headingSeq.length, v.headingSeq.length, 25);
    let mismatches = 0;
    for (let i = 0; i < prefixLen; i++) {
      if (d.headingSeq[i] !== v.headingSeq[i]) mismatches++;
    }
    if (prefixLen >= 3 && mismatches / prefixLen > 0.35) {
      issues.push({ type: "headingOrderDrift", mismatchRatio: Number((mismatches / prefixLen).toFixed(4)) });
    }
  }

  if (v.sectionCount < Math.max(1, Math.floor(d.sectionCount * 0.75))) {
    issues.push({ type: "sectionCountDrop", desktop: d.sectionCount, current: v.sectionCount });
  }

  if (v.tableCount < Math.floor(d.tableCount * 0.8)) {
    issues.push({ type: "tableCountDrop", desktop: d.tableCount, current: v.tableCount });
  }

  if (v.listCount < Math.floor(d.listCount * 0.8)) {
    issues.push({ type: "listCountDrop", desktop: d.listCount, current: v.listCount });
  }

  const desktopFlow = desktopMetric.flowchartCandidates || [];
  const currentFlow = new Map((viewMetric.flowchartCandidates || []).map((x) => [x.selector, x]));
  for (const base of desktopFlow) {
    const cur = currentFlow.get(base.selector);
    if (!cur) {
      issues.push({ type: "flowchartMissing", selector: base.selector });
      continue;
    }
    if (base.columns >= 2 && cur.columns < base.columns) {
      issues.push({
        type: "flowchartColumnCollapse",
        selector: base.selector,
        desktopColumns: base.columns,
        currentColumns: cur.columns,
      });
    }
    const baseAspect = base.aspect || 1;
    const curAspect = cur.aspect || 1;
    const drift = Math.abs((curAspect - baseAspect) / Math.max(0.01, baseAspect));
    if (drift > 0.35) {
      issues.push({
        type: "flowchartAspectDrift",
        selector: base.selector,
        desktopAspect: Number(baseAspect.toFixed(4)),
        currentAspect: Number(curAspect.toFixed(4)),
        drift: Number(drift.toFixed(4)),
      });
    }
  }

  return issues;
}

async function main() {
  const args = parseArgs(process.argv);
  const mode = args.mode || "fluid";
  const htmlPath = args.html ? path.resolve(args.html) : "";
  const workDir = args["work-dir"] ? path.resolve(args["work-dir"]) : "";

  if (!htmlPath || !workDir) {
    console.error(JSON.stringify({ ok: false, error: "Required: --html and --work-dir" }));
    process.exit(2);
  }

  const outJson = args["out-json"]
    ? path.resolve(args["out-json"])
    : path.join(workDir, "responsive_dom_report.json");

  const fileUrl = pathToFileURL(htmlPath).href;
  const browser = await puppeteer.launch({ headless: true });

  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(20000);

    const rawMetrics = [];
    for (const vp of DEFAULT_VIEWPORTS) {
      await page.setViewport({ width: vp.width, height: vp.height, deviceScaleFactor: 1 });
      await page.goto(fileUrl, { waitUntil: "networkidle0" });
      const m = await collectMetrics(page, vp, mode);
      rawMetrics.push({ ...m, config: vp });
    }

    const desktop = rawMetrics.find((x) => x.viewportId === "desktop") || rawMetrics[0];

    const perViewport = rawMetrics.map((m) => {
      const structuralIssues = m.viewportId === desktop.viewportId ? [] : addStructuralDiff(m, desktop);
      const merged = { ...m, structuralIssues };
      const summary = summarizeViewport(merged, mode);
      return {
        viewportId: m.viewportId,
        viewport: m.viewport,
        globalHorizontalScroll: m.globalHorizontalScroll,
        checks: {
          overflowOffenders: m.overflowOffenders,
          clippedHorizontally: m.clippedHorizontally,
          crampedTextColumns: m.crampedTextColumns,
          clippedTextBlocks: m.clippedTextBlocks,
          tinyTextNodes: m.tinyTextNodes,
          distortedImages: m.distortedImages,
          overlapPairs: m.overlapPairs,
          structuralIssues,
        },
        structure: m.structure,
        pass: summary.pass,
        score: summary.score,
        issues: summary.issues,
        warnings: summary.warnings,
      };
    });

    const allIssues = perViewport.flatMap((v) => v.issues.map((msg) => `${v.viewportId}: ${msg}`));
    const allWarnings = perViewport.flatMap((v) => v.warnings.map((msg) => `${v.viewportId}: ${msg}`));
    const score = clamp01(perViewport.reduce((a, b) => a + b.score, 0) / Math.max(1, perViewport.length));

    const result = {
      ok: true,
      mode,
      pass: allIssues.length === 0,
      score,
      thresholds: {
        textCoverageMin: 0.88,
        headingCountFloorRatio: 0.85,
        sectionCountFloorRatio: 0.75,
        overlapMinPx: 8,
        flowchartAspectDriftMax: 0.35,
        crampedColumnMaxPx: "min(200, viewportWidth * 0.5) below 1024px",
      },
      perViewport,
      summary: {
        issueCount: allIssues.length,
        warningCount: allWarnings.length,
        issues: allIssues,
        warnings: allWarnings,
      },
      outputPath: outJson,
    };

    await fs.writeFile(outJson, JSON.stringify(result, null, 2), "utf8");
    console.log(JSON.stringify(result));
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  console.error(JSON.stringify({ ok: false, error: err?.message ?? String(err) }));
  process.exit(1);
});
