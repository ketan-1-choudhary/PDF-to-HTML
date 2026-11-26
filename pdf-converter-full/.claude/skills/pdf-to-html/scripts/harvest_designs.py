"""Harvest reusable design components out of a finished page.

Reads $WORK/design_harvest_targets.json (written by design_catalog.py brief),
pulls each target's outerHTML from the final content.html and its rules from
page.css, strips colour decisions out into the four --design-* role variables,
and writes $WORK/harvested_designs.json for design_catalog.py commit.

Gates (a design that fails any of these is simply not harvested, leaving the
catalog field empty so a later page can fill it):
  * page score below --min-score  -> harvest nothing at all
  * no markup or no CSS found     -> skip that design

Usage:
  python3 scripts/harvest_designs.py \
      --html $WORK/content.html --css $WORK/page.css --work-dir $WORK \
      --score 0.93 --page-width 1047 --page-height 1480
"""

import argparse
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

from PIL import ImageColor
import tinycss2

from design_catalog import DESIGN_COLOR_VARS, validate_page_score

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

# Fallbacks are deliberately loud: if the generator forgets to define the
# variable, the component renders visibly wrong and the Step 9 verifier catches
# it, instead of silently reusing the previous page's palette.
COLOR_ROLES = (
    # (role variable, loud fallback, property-name matcher)
    ("--design-bg", "transparent", re.compile(r"^background(?:-.+)?$")),
    ("--design-border", "currentColor", re.compile(r"^(border|border-\w+|outline|outline-color)")),
    ("--design-text", "currentColor", re.compile(r"^color$")),
)
ACCENT_ROLE = ("--design-accent", "currentColor")

NAMED_COLORS = frozenset(ImageColor.colormap)
COLOR_FUNCTIONS = {
    "rgb", "rgba", "hsl", "hsla", "hwb", "lab", "lch", "oklab",
    "oklch", "color", "color-mix",
}
COLOR_PROPERTIES = re.compile(
    r"^(?:color|background(?:-.+)?|border(?:-.+)?|outline(?:-.+)?|"
    r"box-shadow|text-shadow|fill|stroke|caret-color|column-rule(?:-.+)?|"
    r"text-decoration(?:-.+)?|text-emphasis(?:-.+)?|filter|--.+)$"
)
NESTED_RULE_AT_RULES = {
    "media", "supports", "container", "layer", "scope", "document",
    "starting-style", "keyframes", "-webkit-keyframes",
}


def _warn(msg):
    print(f"warning: {msg}", file=sys.stderr)


# --------------------------------------------------------------------------
# HTML: outerHTML extraction by source offset
# --------------------------------------------------------------------------


class _DesignFinder(HTMLParser):
    """Collect (designId, start, end) source offsets for [data-design-id].

    Slicing the original source keeps the harvested markup byte-identical to
    what actually rendered and verified, rather than a re-serialized guess.
    """

    def __init__(self, text):
        super().__init__(convert_charrefs=False)
        self.text = text
        self.line_starts = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                self.line_starts.append(i + 1)
        self.found = []
        self.stack = []  # open non-void tags
        # Designs nest (a callout inside a sidebar panel), so captures are a
        # stack, not a single slot: both get harvested.
        self.captures = []  # [{designId, start, depth}]

    def _offset(self):
        line, col = self.getpos()
        return self.line_starts[line - 1] + col

    def _design_id(self, attrs):
        for key, value in attrs:
            if key == "data-design-id" and value:
                return value.strip().replace(".", "-")
        return None

    def _record_void(self, did):
        start = self._offset()
        end = self.text.find(">", start)
        if end != -1:
            self.found.append((did, start, end + 1))

    def handle_starttag(self, tag, attrs):
        did = self._design_id(attrs)
        if tag in VOID_TAGS:
            if did:
                self._record_void(did)
            return
        start = self._offset()
        self.stack.append(tag)
        if did:
            self.captures.append({"designId": did, "start": start, "depth": len(self.stack)})

    def handle_startendtag(self, tag, attrs):
        did = self._design_id(attrs)
        if did:
            self._record_void(did)

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        if tag in self.stack:
            # Tolerate unclosed inner tags by unwinding to the matching one.
            while self.stack and self.stack.pop() != tag:
                pass
        else:
            return
        close = self.text.find(">", self._offset())
        end = close + 1 if close != -1 else len(self.text)
        while self.captures and self.captures[-1]["depth"] > len(self.stack):
            capture = self.captures.pop()
            self.found.append((capture["designId"], capture["start"], end))

    def close(self):
        super().close()
        # Anything still open at EOF: take it to the end of the document.
        while self.captures:
            capture = self.captures.pop()
            self.found.append((capture["designId"], capture["start"], len(self.text)))


def extract_html_occurrences(html_text):
    """-> {designId: [outerHTML, ...]} in document order."""
    finder = _DesignFinder(html_text)
    finder.feed(html_text)
    finder.close()
    out = {}
    # Nested captures close before their parents, so restore document order.
    for did, start, end in sorted(finder.found, key=lambda f: f[1]):
        out.setdefault(did, []).append(html_text[start:end].strip())
    return out


def has_outer_class(markup, class_name):
    class OuterTag(HTMLParser):
        def __init__(self):
            super().__init__()
            self.classes = None

        def handle_starttag(self, tag, attrs):
            if self.classes is None:
                self.classes = (dict(attrs).get("class") or "").split()

        handle_startendtag = handle_starttag

    parser = OuterTag()
    parser.feed(markup)
    return class_name in (parser.classes or [])


# --------------------------------------------------------------------------
# CSS: sentinel slice, with a selector-scan fallback
# --------------------------------------------------------------------------


def extract_css_sentinel(css_text, design_id):
    """Slice /* @design <id> */ ... /* @end <id> */ (the intended path)."""
    pattern = re.compile(
        r"/\*\s*@design\s+" + re.escape(design_id) + r"\s*\*/(.*?)/\*\s*@end\s+"
        + re.escape(design_id) + r"\s*\*/",
        re.DOTALL,
    )
    match = pattern.search(css_text)
    return match.group(1).strip() if match else ""


CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _top_level_blocks(css_text):
    """Yield (prelude, body, full_text) for each top-level { } block.

    The prelude is comment-stripped so a neighbouring block's trailing
    /* @end ... */ never rides along into a scanned rule.
    """
    depth = 0
    start = 0
    block_start = None
    for i, ch in enumerate(css_text):
        if ch == "{":
            if depth == 0:
                block_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and block_start is not None:
                prelude = CSS_COMMENT.sub("", css_text[start:block_start]).strip()
                body = css_text[block_start + 1:i]
                yield prelude, body, f"{prelude} {{{body}}}"
                start = i + 1
                block_start = None


def extract_css_fallback(css_text, class_name):
    """Best-effort: every rule whose selector mentions the design class.

    Misses :root variables, @keyframes referenced by name, and ancestor-rooted
    selectors -- which is exactly why sentinels are the intended path.
    """
    selector_pattern = re.compile(r"\." + re.escape(class_name) + r"(?![a-zA-Z0-9_-])")
    chunks = []
    for prelude, body, full in _top_level_blocks(css_text):
        if prelude.startswith("@media") or prelude.startswith("@supports"):
            inner = [
                f for selector, _, f in _top_level_blocks(body)
                if selector_pattern.search(selector)
            ]
            if inner:
                chunks.append(prelude + " {\n  " + "\n  ".join(inner) + "\n}")
        elif selector_pattern.search(prelude):
            chunks.append(full)
    return "\n".join(chunks).strip()


# --------------------------------------------------------------------------
# colour tokenization
# --------------------------------------------------------------------------


def _role_for(prop):
    prop = prop.strip().lower()
    for var, fallback, matcher in COLOR_ROLES:
        if matcher.match(prop):
            return var, fallback
    return ACCENT_ROLE


def validate_design_css(css_body, class_name):
    prefix = re.compile(r"^\." + re.escape(class_name) + r"(?=$|[\s.\[#:+~>])")

    def check_rules(rules, keyframes=False):
        for rule in rules:
            if rule.type == "error":
                raise ValueError(f"invalid CSS rule: {rule.message}")
            if rule.type == "qualified-rule" and not keyframes:
                selectors = [[]]
                for token in rule.prelude:
                    if token.type == "literal" and token.value == ",":
                        selectors.append([])
                    else:
                        selectors[-1].append(token)
                for tokens in selectors:
                    selector = tinycss2.serialize(tokens).strip()
                    if not prefix.match(selector):
                        raise ValueError(f"design selector is not self-contained: {selector}")
            elif rule.type == "at-rule":
                if rule.content is None or rule.lower_at_keyword not in NESTED_RULE_AT_RULES:
                    raise ValueError(f"unsupported design CSS at-rule: @{rule.at_keyword}")
                nested = tinycss2.parse_rule_list(
                    rule.content, skip_comments=False, skip_whitespace=False
                )
                check_rules(nested, keyframes=rule.lower_at_keyword.endswith("keyframes"))

    check_rules(tinycss2.parse_stylesheet(css_body, skip_comments=False, skip_whitespace=False))


def _tokenize_value(tokens, prop, replacements):
    var, fallback = _role_for(prop)
    result = []
    for token in tokens:
        is_hex = (
            token.type == "hash"
            and len(token.value) in (3, 4, 6, 8)
            and re.fullmatch(r"[0-9a-fA-F]+", token.value) is not None
        )
        is_named_color = (
            token.type == "ident"
            and COLOR_PROPERTIES.match(prop.lower()) is not None
            and token.value.lower() in NAMED_COLORS
        )
        is_color_function = (
            token.type == "function" and token.name.lower() in COLOR_FUNCTIONS
        )
        if is_hex or is_named_color or is_color_function:
            literal = tinycss2.serialize([token])
            replacements.append({"property": prop, "from": literal, "to": var})
            result.extend(tinycss2.parse_component_value_list(f"var({var}, {fallback})"))
        elif token.type == "function" and token.name.lower() != "url":
            token.arguments = _tokenize_value(token.arguments, prop, replacements)
            result.append(token)
        elif token.type in ("() block", "[] block", "{} block"):
            token.content = _tokenize_value(token.content, prop, replacements)
            result.append(token)
        else:
            result.append(token)
    return result


def _tokenize_declarations(content, replacements):
    declarations = tinycss2.parse_declaration_list(content, skip_comments=False, skip_whitespace=False)
    result = []
    for declaration in declarations:
        if declaration.type == "error":
            raise ValueError(f"invalid CSS declaration: {declaration.message}")
        if declaration.type == "declaration":
            if declaration.name in DESIGN_COLOR_VARS:
                replacements.append({
                    "property": declaration.name,
                    "from": tinycss2.serialize(declaration.value).strip(),
                    "to": "page-specific binding removed",
                })
                continue
            declaration.value = _tokenize_value(declaration.value, declaration.name, replacements)
        elif declaration.type == "at-rule":
            _tokenize_rule(declaration, replacements)
        result.append(declaration)
    return tinycss2.serialize(result)


def _tokenize_rule(rule, replacements):
    if rule.type == "error":
        raise ValueError(f"invalid CSS rule: {rule.message}")
    if rule.type == "qualified-rule":
        body = _tokenize_declarations(rule.content, replacements)
        rule.content = tinycss2.parse_component_value_list(body)
    elif rule.type == "at-rule" and rule.content is not None:
        if rule.lower_at_keyword in NESTED_RULE_AT_RULES:
            nested = tinycss2.parse_rule_list(rule.content, skip_comments=False, skip_whitespace=False)
            for child in nested:
                _tokenize_rule(child, replacements)
            body = tinycss2.serialize(nested)
        else:
            body = _tokenize_declarations(rule.content, replacements)
        rule.content = tinycss2.parse_component_value_list(body)


def tokenize_colors(css_body):
    """Replace CSS colour tokens, leaving URLs, strings and selectors intact."""
    replacements = []
    rules = tinycss2.parse_stylesheet(css_body, skip_comments=False, skip_whitespace=False)
    for rule in rules:
        _tokenize_rule(rule, replacements)
    return tinycss2.serialize(rules), replacements


STYLE_ATTR = re.compile(
    r"(?<![\w:-])(?P<prefix>style\s*=\s*)(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)


def tokenize_inline_styles(html_snippet):
    """Tokenize inline CSS and remove page-specific role bindings."""
    replacements = []

    def sub(match):
        fixed = _tokenize_declarations(match.group("value"), replacements)
        quote = match.group("quote")
        return f'{match.group("prefix")}{quote}{fixed}{quote}'

    return STYLE_ATTR.sub(sub, html_snippet), replacements


# --------------------------------------------------------------------------
# occurrence selection
# --------------------------------------------------------------------------


def _touches_edge(bbox, page_width, page_height, tolerance=2):
    try:
        x = float(bbox.get("x", 0))
        y = float(bbox.get("y", 0))
        w = float(bbox.get("width", 0))
        h = float(bbox.get("height", 0))
    except (AttributeError, TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    hit = x <= tolerance or y <= tolerance
    if page_width:
        hit = hit or (x + w) >= (page_width - tolerance)
    if page_height:
        hit = hit or (y + h) >= (page_height - tolerance)
    return hit


def pick_occurrence(instances, page_width, page_height, tolerance=2):
    """Index of the occurrence to use as the permanent reference.

    Reading order decides it (first wins, per the immutable-catalog rule), with
    one preference: if some occurrence does not touch a page edge, prefer it,
    since an edge-touching box may be a component truncated by the margin.
    This is only a preference, never a skip -- running heads, bleed banners and
    outer sidebars touch an edge by design, and on a single page a deliberate
    bleed is indistinguishable from a truncation.
    """
    instances = instances or []
    if not instances:
        return 0, "no occurrence bbox"
    for index, bbox in enumerate(instances):
        if _touches_edge(bbox, page_width, page_height, tolerance) is False:
            return index, None
    note = "every occurrence touches a page edge" if len(instances) > 1 else None
    return 0, note


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--html", required=True, help="final content.html")
    parser.add_argument("--css", required=True, help="final page.css")
    parser.add_argument("--work-dir", required=True)
    score_source = parser.add_mutually_exclusive_group(required=True)
    score_source.add_argument("--score", type=float, help="final page score from verify_layered")
    score_source.add_argument("--score-file", help="final verify_layered JSON output")
    parser.add_argument("--page", type=int, help="require the score file to match this page")
    parser.add_argument("--min-score", type=float, default=0.85)
    parser.add_argument("--page-width", type=int)
    parser.add_argument("--page-height", type=int)
    args = parser.parse_args()

    try:
        min_score = validate_page_score(args.min_score, "minimum score")
        if args.score_file:
            verification = json.loads(Path(args.score_file).read_text(encoding="utf-8"))
            if not isinstance(verification, dict):
                raise ValueError("verification output must be an object")
            if args.page is not None and verification.get("page") != args.page:
                raise ValueError("verification page does not match the requested page")
            score = validate_page_score(verification.get("score"))
        else:
            score = validate_page_score(args.score)
        if args.page is not None and args.page < 1:
            raise ValueError("page must be a positive integer")
        if any(value is not None and value <= 0 for value in (args.page_width, args.page_height)):
            raise ValueError("page dimensions must be positive")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    work = Path(args.work_dir)
    out_path = work / "harvested_designs.json"
    targets_path = work / "design_harvest_targets.json"

    def finish(payload, harvested=()):
        out_path.write_text(
            json.dumps(list(harvested), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    if not targets_path.exists():
        parser.error(f"harvest targets not found at {targets_path}")
    targets = json.loads(targets_path.read_text(encoding="utf-8"))
    if not isinstance(targets, list):
        parser.error("harvest targets must be an array")
    if not targets:
        return finish({"ok": True, "harvested": [], "note": "no harvest targets"})

    if score < min_score:
        return finish(
            {
                "ok": True,
                "harvested": [],
                "skipped": [
                    {
                        "designId": target["designId"],
                        "reason": f"page score {score} below floor {min_score}",
                    }
                    for target in targets
                ],
                "note": f"page score {score} below floor {min_score}",
            }
        )

    html_text = Path(args.html).read_text(encoding="utf-8")
    css_text = Path(args.css).read_text(encoding="utf-8")
    occurrences = extract_html_occurrences(html_text)

    page_w, page_h = args.page_width, args.page_height
    brief_path = work / "design_brief.json"
    if (page_w is None or page_h is None) and brief_path.exists():
        brief = json.loads(brief_path.read_text(encoding="utf-8"))
        page_w = page_w or brief.get("pageWidth")
        page_h = page_h or brief.get("pageHeight")

    harvested, skipped, warnings = [], [], []
    for target in targets:
        did = target["designId"]
        class_name = target.get("className") or ("d-" + did)

        markup_list = occurrences.get(did) or []
        if not markup_list:
            skipped.append({"designId": did, "reason": "no [data-design-id] in page"})
            continue

        instances = target.get("instances") or []
        index, note = pick_occurrence(instances, page_w, page_h)
        if note:
            warnings.append(f"{did}: {note}, using occurrence {index}")
        if instances and len(instances) != len(markup_list):
            warnings.append(
                f"{did}: {len(instances)} bboxes vs "
                f"{len(markup_list)} rendered, using first occurrence"
            )
            index = 0
        markup = markup_list[index]
        if not has_outer_class(markup, class_name):
            skipped.append({"designId": did, "reason": f"missing {class_name} class on design root"})
            continue

        css = extract_css_sentinel(css_text, did)
        source = "sentinel"
        if not css:
            css = extract_css_fallback(css_text, class_name)
            source = "selector-scan"
            if css:
                warnings.append(f"{did}: no /* @design */ sentinel, scanned selectors")
        if not css:
            skipped.append({"designId": did, "reason": "no CSS found"})
            continue

        try:
            validate_design_css(css, class_name)
            css, css_reps = tokenize_colors(css)
            markup, html_reps = tokenize_inline_styles(markup)
            replacements = css_reps + html_reps
        except ValueError as exc:
            reason = f"invalid design CSS: {exc}"
            skipped.append({"designId": did, "reason": reason})
            warnings.append(f"{did}: {reason}")
            continue

        harvested.append(
            {
                "designId": did,
                "html": markup,
                "css": css.strip(),
                "score": score,
                "occurrenceIndex": index,
                "cssSource": source,
                "colorsTokenized": len(replacements),
            }
        )

    for msg in warnings:
        _warn(msg)

    return finish(
        {
            "ok": True,
            "harvested": [h["designId"] for h in harvested],
            "skipped": skipped,
            "warnings": warnings,
            "out": str(out_path),
        },
        harvested,
    )


if __name__ == "__main__":
    raise SystemExit(main())
