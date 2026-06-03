from __future__ import annotations

import hashlib
import textwrap
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from eightmem.core.paths import ensure_cache_dir


CARD_WIDTH = 1200
CARD_HEIGHT = 900
WEAK_SIGNAL_TEXT = "Memory signal is weak for this prompt."


@dataclass(frozen=True)
class CompareCardContent:
    prompt: str
    standard_answer: str
    memory_answer: str
    basis: list[str]
    weak_signal: bool = False


@dataclass(frozen=True)
class PassportCardContent:
    status: str
    active_entries: str
    corrections_tracked: str
    current_items: list[str]
    trust_items: list[str]


def build_compare_share_card(compare_output: str, *, user_id: str | None = None) -> Path:
    content = parse_compare_output(compare_output)
    if not is_share_card_eligible(compare_output):
        raise ValueError("compare output is not strong enough for a share card")
    digest = hashlib.sha256(compare_output.encode("utf-8")).hexdigest()[:16]
    directory = ensure_cache_dir() / "compare_cards"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"compare_{user_id or 'default'}_{digest}.png"
    render_compare_card(content, path)
    return path


def build_passport_share_card(passport_summary: str, *, user_id: str | None = None) -> Path:
    content = parse_passport_summary(passport_summary)
    if not is_passport_card_eligible(passport_summary):
        raise ValueError("passport summary is not strong enough for a share card")
    digest = hashlib.sha256(passport_summary.encode("utf-8")).hexdigest()[:16]
    directory = ensure_cache_dir() / "passport_cards"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"passport_{user_id or 'default'}_{digest}.png"
    render_passport_card(content, path)
    return path


def parse_passport_summary(passport_summary: str) -> PassportCardContent:
    lines = passport_summary.splitlines()
    status = _line_value(lines, "Status:")
    active_entries = _line_value(lines, "Active entries:")
    corrections = _line_value(lines, "Corrections tracked:")
    current_items = _section_bullets(lines, "Currently using:")
    trust_items = _section_bullets(lines, "Trust basis:")
    return PassportCardContent(
        status=status or "Unknown",
        active_entries=active_entries or "0",
        corrections_tracked=corrections or "0",
        current_items=current_items,
        trust_items=trust_items,
    )


def is_passport_card_eligible(passport_summary: str) -> bool:
    content = parse_passport_summary(passport_summary)
    if not content.current_items:
        return False
    if all("nothing yet" in item.lower() for item in content.current_items):
        return False
    return True


def parse_compare_output(compare_output: str) -> CompareCardContent:
    prompt = _between(compare_output, "Prompt:", "\n\nStandard answer:").strip()
    standard = _between(compare_output, "Standard answer:\n", "\n\nAnswer shaped by what I know about you:").strip()
    memory_section = compare_output.split("Answer shaped by what I know about you:\n", 1)[-1]
    memory_answer = memory_section
    basis: list[str] = []
    weak_signal = WEAK_SIGNAL_TEXT in compare_output

    if "\n\nWhat changed because of memory:\n" in memory_answer:
        memory_answer, basis_section = memory_answer.split("\n\nWhat changed because of memory:\n", 1)
        basis_section = basis_section.split("\n\nMemory signal is weak for this prompt.", 1)[0]
        basis = [line.removeprefix("- ").strip() for line in basis_section.splitlines() if line.strip()]
    elif "\n\nMemory signal is weak for this prompt." in memory_answer:
        memory_answer = memory_answer.split("\n\nMemory signal is weak for this prompt.", 1)[0]

    return CompareCardContent(
        prompt=prompt or "Compare answer",
        standard_answer=standard,
        memory_answer=memory_answer.strip(),
        basis=basis,
        weak_signal=weak_signal,
    )


def render_passport_card(content: PassportCardContent, path: Path) -> Path:
    image = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), "#f7f2e8")
    draw = ImageDraw.Draw(image)

    font_title = _font(48, bold=True)
    font_label = _font(24, bold=True)
    font_body = _font(23)
    font_small = _font(20)
    font_tiny = _font(18)
    font_footer = _font(23, bold=True)

    draw.rounded_rectangle((36, 36, CARD_WIDTH - 36, CARD_HEIGHT - 36), radius=36, fill="#fffaf0", outline="#111111", width=3)
    draw.text((72, 70), "8mem Passport", fill="#111111", font=font_title)
    draw.text((72, 132), "What this AI is using about you right now", fill="#555044", font=font_small)

    metrics = [
        ("Memory status", _passport_status_label(content.status)),
        ("Saved memories", content.active_entries),
        ("Corrections", content.corrections_tracked),
    ]
    metric_x = 72
    for label, value in metrics:
        box = (metric_x, 180, metric_x + 320, 292)
        draw.rounded_rectangle(box, radius=22, fill="#ffffff", outline="#d8cdb9", width=2)
        draw.text((metric_x + 26, 202), label.upper(), fill="#756b5a", font=font_tiny)
        draw.text((metric_x + 26, 238), _truncate(value, 28), fill="#111111", font=font_label)
        metric_x += 348

    current_box = (72, 330, CARD_WIDTH - 72, 610)
    draw.rounded_rectangle(current_box, radius=28, fill="#ffffff", outline="#d8cdb9", width=2)
    draw.text((102, 360), "CURRENTLY HELPING WITH", fill="#111111", font=font_label)
    current_items = _passport_display_items(content.current_items)
    _draw_bounded_bullets(
        draw,
        current_items,
        xy=(102, 410),
        max_width=CARD_WIDTH - 204,
        max_height=168,
        font=font_body,
        fill="#1d1d1b",
        max_items=4,
    )

    trust_box = (72, 638, CARD_WIDTH - 72, 796)
    draw.rounded_rectangle(trust_box, radius=22, fill="#fff7df", outline="#e4d8be", width=1)
    draw.text((102, 660), "TRUST CHECK", fill="#111111", font=font_label)
    _draw_bounded_bullets(
        draw,
        _passport_trust_items(content),
        xy=(102, 704),
        max_width=CARD_WIDTH - 204,
        max_height=86,
        font=font_small,
        fill="#3d392f",
        max_items=3,
        line_height=28,
        item_gap=0,
    )

    footer_y = CARD_HEIGHT - 74
    draw.line((72, CARD_HEIGHT - 92, CARD_WIDTH - 72, CARD_HEIGHT - 92), fill="#d8cdb9", width=2)
    draw.text((72, footer_y), "8mem.com", fill="#111111", font=font_footer)
    tagline = "Portable memory for AI"
    tagline_width = draw.textbbox((0, 0), tagline, font=font_small)[2]
    draw.text((CARD_WIDTH - 72 - tagline_width, footer_y + 2), tagline, fill="#555044", font=font_small)
    image.save(path, format="PNG", optimize=True)
    return path


def _line_value(lines: list[str], prefix: str) -> str:
    for line in lines:
        if line.startswith(prefix):
            return line.removeprefix(prefix).strip()
    return ""


def _section_bullets(lines: list[str], heading: str) -> list[str]:
    if heading not in lines:
        return []
    start = lines.index(heading) + 1
    items: list[str] = []
    for line in lines[start:]:
        if not line.strip():
            break
        if line.startswith("- "):
            items.append(line.removeprefix("- ").strip())
    return items


def is_share_card_eligible(compare_output: str) -> bool:
    content = parse_compare_output(compare_output)
    if content.weak_signal:
        return False
    if not content.standard_answer or not content.memory_answer:
        return False
    if _normalize_for_compare(content.standard_answer) == _normalize_for_compare(content.memory_answer):
        return False
    return _has_share_card_ready_basis(content.basis)


def render_compare_card(content: CompareCardContent, path: Path) -> Path:
    image = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), "#f7f2e8")
    draw = ImageDraw.Draw(image)

    font_title = _font(46, bold=True)
    font_label = _font(24, bold=True)
    font_small = _font(20)
    font_footer = _font(23, bold=True)

    draw.rounded_rectangle((36, 36, CARD_WIDTH - 36, CARD_HEIGHT - 36), radius=36, fill="#fffaf0", outline="#111111", width=3)
    draw.text((72, 70), "8mem Compare", fill="#111111", font=font_title)
    draw.text((72, 132), _truncate(content.prompt, 110), fill="#555044", font=font_small)

    left_box = (72, 185, 560, 625)
    right_box = (640, 185, 1128, 625)
    draw.rounded_rectangle(left_box, radius=28, fill="#ffffff", outline="#d8cdb9", width=2)
    draw.rounded_rectangle(right_box, radius=28, fill="#111111", outline="#111111", width=2)

    draw.text((102, 216), "WITHOUT MEMORY", fill="#756b5a", font=font_label)
    draw.text((670, 216), "WITH 8MEM", fill="#c8ff7a", font=font_label)
    _draw_wrapped_adaptive(draw, content.standard_answer, (102, 270), 430, 315, "#1d1d1b")
    _draw_wrapped_adaptive(draw, content.memory_answer, (670, 270), 430, 315, "#fffaf0")

    basis_box = (72, 650, CARD_WIDTH - 72, 796)
    draw.rounded_rectangle(basis_box, radius=22, fill="#fff7df", outline="#e4d8be", width=1)
    basis_y = 670
    if content.basis:
        draw.text((102, basis_y), "8mem knew", fill="#111111", font=font_label)
        y = basis_y + 42
        for item in build_card_proof_items(content.basis)[:3]:
            draw.text((122, y), f"- {_truncate(item, 92)}", fill="#3d392f", font=font_small)
            y += 28
    elif content.weak_signal:
        draw.text((102, basis_y), "Memory signal is still thin for this prompt.", fill="#6f4d00", font=font_label)

    footer_y = CARD_HEIGHT - 74
    draw.line((72, CARD_HEIGHT - 92, CARD_WIDTH - 72, CARD_HEIGHT - 92), fill="#d8cdb9", width=2)
    draw.text((72, footer_y), "8mem.com", fill="#111111", font=font_footer)
    tagline = "AI that actually knows you"
    tagline_width = draw.textbbox((0, 0), tagline, font=font_small)[2]
    draw.text((CARD_WIDTH - 72 - tagline_width, footer_y + 2), tagline, fill="#555044", font=font_small)
    image.save(path, format="PNG", optimize=True)
    return path


def _between(text: str, start: str, end: str) -> str:
    if start not in text:
        return ""
    value = text.split(start, 1)[1]
    if end in value:
        value = value.split(end, 1)[0]
    return value


def _draw_wrapped(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy: tuple[int, int],
    max_width: int,
    max_height: int,
    font: ImageFont.ImageFont,
    fill: str,
) -> None:
    x, y = xy
    line_height = 34
    max_lines = max_height // line_height
    lines = _wrap_for_pixels(draw, text, max_width, font)
    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + [lines[max_lines - 1].rstrip(" .") + "..."]
    for line in lines:
        draw.text((x, y), line, fill=fill, font=font)
        y += line_height


def _draw_wrapped_adaptive(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy: tuple[int, int],
    max_width: int,
    max_height: int,
    fill: str,
) -> None:
    for size, line_height in ((23, 34), (21, 31), (19, 28), (17, 25)):
        font = _font(size)
        lines = _wrap_for_pixels(draw, text, max_width, font)
        if len(lines) <= max_height // line_height:
            _draw_lines(draw, lines, xy, line_height, font, fill)
            return

    font = _font(17)
    line_height = 25
    lines = _wrap_for_pixels(draw, text, max_width, font)
    max_lines = max_height // line_height
    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + [_truncate_to_width(draw, lines[max_lines - 1], max_width, font)]
    _draw_lines(draw, lines, xy, line_height, font, fill)


def _draw_lines(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    xy: tuple[int, int],
    line_height: int,
    font: ImageFont.ImageFont,
    fill: str,
) -> None:
    x, y = xy
    for line in lines:
        draw.text((x, y), line, fill=fill, font=font)
        y += line_height


def _draw_bounded_bullets(
    draw: ImageDraw.ImageDraw,
    items: list[str],
    *,
    xy: tuple[int, int],
    max_width: int,
    max_height: int,
    font: ImageFont.ImageFont,
    fill: str,
    max_items: int,
    line_height: int = 31,
    item_gap: int = 6,
) -> None:
    x, y = xy
    bottom = y + max_height
    for item in items[:max_items]:
        lines = _wrap_for_pixels(draw, f"- {item}", max_width, font)
        lines = lines[:2]
        required_height = len(lines) * line_height
        if y + required_height > bottom:
            break
        _draw_lines(draw, lines, (x, y), line_height, font, fill)
        y += required_height + item_gap


def _truncate_to_width(draw: ImageDraw.ImageDraw, line: str, max_width: int, font: ImageFont.ImageFont) -> str:
    suffix = "..."
    candidate = line.rstrip(" .")
    while candidate and draw.textbbox((0, 0), candidate + suffix, font=font)[2] > max_width:
        candidate = candidate[:-1].rstrip()
    return (candidate or line[:1]).rstrip(" .") + suffix


def _wrap_for_pixels(draw: ImageDraw.ImageDraw, text: str, max_width: int, font: ImageFont.ImageFont) -> list[str]:
    lines: list[str] = []
    paragraphs = [paragraph.strip() for paragraph in text.splitlines() if paragraph.strip()]
    for paragraph in paragraphs or [""]:
        bullet_prefix = "- " if paragraph.startswith("- ") else ""
        normalized = " ".join(paragraph.removeprefix("- ").split())
        font_size = getattr(font, "size", 20) or 20
        rough_width = max(24, int(max_width / (font_size * 0.55)))
        rough_lines = textwrap.wrap(normalized, width=rough_width) or [""]
        paragraph_lines: list[str] = []
        for rough_line in rough_lines:
            current = ""
            for word in rough_line.split():
                candidate = f"{current} {word}".strip()
                visible_candidate = f"{bullet_prefix if not paragraph_lines else '  '}{candidate}"
                if draw.textbbox((0, 0), visible_candidate, font=font)[2] <= max_width:
                    current = candidate
                else:
                    if current:
                        prefix = bullet_prefix if not paragraph_lines else "  "
                        paragraph_lines.append(f"{prefix}{current}")
                    current = word
            if current:
                prefix = bullet_prefix if not paragraph_lines else "  "
                paragraph_lines.append(f"{prefix}{current}")
        lines.extend(paragraph_lines)
    return lines


def _normalize_for_compare(text: str) -> str:
    return " ".join(text.lower().split())


def _has_share_card_ready_basis(basis: list[str]) -> bool:
    for item in basis:
        normalized = item.lower()
        if "project context:" in normalized or "project guardrail:" in normalized:
            return True
        if "reply length/style: concise" in normalized:
            return True
        if "format: structured bullets" in normalized:
            return True
        if any(
            marker in normalized
            for marker in {
                "progress / risk / next",
                "progress/risk/next",
                "structured",
                "short",
                "direct",
                "concise",
                "execution speed",
                "stability",
                "generic motivational",
                "founder",
                "investor",
            }
        ):
            return True
    return False


def build_card_proof_items(basis: list[str]) -> list[str]:
    proof_items = [_humanize_basis_item(item) for item in _sort_basis_for_proof(basis)]
    proof_items = [item for item in proof_items if item]
    unique_items: list[str] = []
    for item in proof_items:
        if item not in unique_items:
            unique_items.append(item)
    return unique_items[:3]


def _sort_basis_for_proof(basis: list[str]) -> list[str]:
    return sorted(basis, key=_basis_priority)


def _basis_priority(item: str) -> int:
    normalized = item.lower()
    if "execution speed" in normalized and "stability" in normalized:
        return 0
    if "progress / risk / next" in normalized or "progress/risk/next" in normalized:
        return 1
    if "generic motivational" in normalized or "generic founder" in normalized:
        return 2
    if "project context:" in normalized or "project guardrail:" in normalized:
        return 3
    if "structured" in normalized or "concise" in normalized or "short" in normalized or "direct" in normalized:
        return 4
    return 9


def _humanize_basis_item(item: str) -> str:
    normalized = item.strip().rstrip(".")
    lowered = normalized.lower()
    value = _strip_basis_prefix(normalized)
    lowered_value = value.lower()

    if "execution speed" in lowered and "stability" in lowered:
        return "You care about speed without breaking stability"
    if "progress / risk / next" in lowered or "progress/risk/next" in lowered:
        return "You prefer Progress / Risk / Next updates"
    if "generic motivational" in lowered or "generic founder" in lowered:
        return "Avoid generic founder-speak"
    if "8mem" in lowered_value and "memory layer" in lowered_value:
        return "You are building 8mem, a portable AI memory layer"
    if lowered.startswith("reply length/style: concise"):
        return "You prefer concise answers"
    if lowered.startswith("format: structured bullets"):
        return "You prefer structured bullets"
    if "short" in lowered_value and "direct" in lowered_value:
        return "You prefer short, direct replies"
    if lowered.startswith("emoji policy: no emojis"):
        return "Avoid emojis"
    if value:
        return value[0].upper() + value[1:]
    return ""


def _strip_basis_prefix(item: str) -> str:
    prefixes = [
        "Project context: ",
        "Project guardrail: ",
        "Preference: ",
        "Correction: ",
        "Reply length/style: ",
        "Format: ",
        "Emoji policy: ",
    ]
    for prefix in prefixes:
        if item.startswith(prefix):
            value = item[len(prefix) :].strip()
            if value.startswith("Prefers "):
                value = value[len("Prefers ") :].strip()
            return value
    return item


def _passport_status_label(status: str) -> str:
    normalized = " ".join(status.split()).strip()
    if not normalized:
        return "Active"
    if normalized.lower() == "stable":
        return "Active"
    return normalized


def _passport_display_items(items: list[str]) -> list[str]:
    display_items = [_humanize_passport_item(item) for item in items]
    display_items = [item for item in display_items if item]
    if display_items:
        return display_items
    return ["No saved memory yet. Add one preference to personalize replies."]


def _humanize_passport_item(item: str) -> str:
    value = " ".join(item.strip().rstrip(".").split())
    if not value:
        return ""
    lower = value.lower()
    if lower.startswith("identity: timezone:"):
        timezone = value.split(":", 2)[-1].strip()
        return f"Timezone: {timezone}."
    if lower.startswith("identity:"):
        return value.removeprefix("Identity:").strip().capitalize() + "."
    if lower.startswith("current location:"):
        location = value.split(":", 1)[1].strip()
        return f"Current location: {location}."
    if lower.startswith("previous location:"):
        location = value.split(":", 1)[1].strip()
        return f"Previous location: {location}."
    if lower.startswith("preference:"):
        preference = value.split(":", 1)[1].strip()
        return _sentence_from_preference(preference)
    if lower.startswith("correction:"):
        correction = value.split(":", 1)[1].strip()
        return f"Correction followed: {correction}."
    return value[0].upper() + value[1:] + "."


def _sentence_from_preference(preference: str) -> str:
    preference = " ".join(preference.strip().rstrip(".").split())
    lower = preference.lower()
    if lower.startswith("prefers "):
        return f"You prefer {preference[8:]}."
    if lower.startswith("prefer "):
        return f"You prefer {preference[7:]}."
    if lower.startswith("you prefer "):
        return preference[0].upper() + preference[1:] + "."
    return f"Preference: {preference}."


def _passport_trust_items(content: PassportCardContent) -> list[str]:
    trust_items = [
        "Read-only commands do not save new memory.",
        "Corrections override older memories.",
        'Ask "forget <memory>" anytime.',
    ]
    if content.trust_items:
        conflict_line = next((item for item in content.trust_items if "conflict" in item.lower()), "")
        if conflict_line and "nothing" not in conflict_line.lower():
            trust_items[0] = "Some saved memories need review."
    return trust_items


def _truncate(value: str, limit: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "..."


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()
