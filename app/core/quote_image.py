"""
Renders "quote card" images used by the /quote command and the "Make Quote"
context menu in commands/misc_cmds.py: an avatar with a fading edge on the
left, quoted text on the right, with CJK-aware font fallback since the
regular DejaVu fonts don't cover CJK glyphs.
"""

import io
import os
from PIL import Image, ImageDraw, ImageFont

BASE_DIR       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS_DIR      = os.path.join(BASE_DIR, "assets", "fonts")
FONT_REG_PATH  = os.path.join(FONTS_DIR, "DejaVuSans.ttf")
FONT_BOLD_PATH = os.path.join(FONTS_DIR, "DejaVuSans-Bold.ttf")
NOTO_CJK_PATH  = os.path.join(FONTS_DIR, "NotoSansCJK-Regular.ttc")

AV = 350
W  = AV + 500
H  = AV

PAD_Y     = 40
TEXT_X    = AV + 40
ACCENT_X  = TEXT_X - 16
MAX_WIDTH = W - TEXT_X - 30

MIN_SIZE = 18


def _is_cjk_char(cp):
    return (
        0x1100  <= cp <= 0x11FF  or
        0x2E80  <= cp <= 0x2EFF  or
        0x2F00  <= cp <= 0x2FDF  or
        0x3000  <= cp <= 0x9FFF  or
        0xA000  <= cp <= 0xA4CF  or
        0xAC00  <= cp <= 0xD7AF  or
        0xF900  <= cp <= 0xFAFF  or
        0xFE10  <= cp <= 0xFE1F  or
        0xFE30  <= cp <= 0xFE4F  or
        0xFF00  <= cp <= 0xFFEF  or
        0x20000 <= cp <= 0x2FA1F
    )


def _build_cmap(path, index=0):
    try:
        from fontTools.ttLib import TTFont
        tt = TTFont(path, fontNumber=index)
        cmap = tt.getBestCmap()
        if not cmap:
            return set()
        glyph_order = tt.getGlyphOrder()
        notdef = glyph_order[0] if glyph_order else '.notdef'
        return set(cp for cp, name in cmap.items() if name != notdef)
    except Exception as e:
        print(f"[quote_image] build_cmap failed for {path} index={index}: {e}", flush=True)
        return set()


def _make_fonts(size_quote, size_name, size_handle):
    fq  = ImageFont.truetype(FONT_REG_PATH,  size_quote)
    fn  = ImageFont.truetype(FONT_BOLD_PATH, size_name)
    fh  = ImageFont.truetype(FONT_REG_PATH,  size_handle)
    fc  = ImageFont.truetype(NOTO_CJK_PATH,  size_quote,  index=0)
    fcn = ImageFont.truetype(NOTO_CJK_PATH,  size_name,   index=0)
    fch = ImageFont.truetype(NOTO_CJK_PATH,  size_handle, index=0)
    return fq, fn, fh, fc, fcn, fch


def _build_fallback_list(font_quote, font_cjk, size_quote):
    fl = [(font_quote, _build_cmap(FONT_REG_PATH))]
    for fname in ["DejaVuSerif.ttf", "DejaVuSansMono.ttf"]:
        p = os.path.join(FONTS_DIR, fname)
        if os.path.exists(p):
            fl.append((ImageFont.truetype(p, size_quote), _build_cmap(p)))
    fl.append((font_cjk, _build_cmap(NOTO_CJK_PATH, index=0)))
    return fl


def _best_font_for(ch, font_quote, font_cjk, fallback_list):
    cp = ord(ch)
    if _is_cjk_char(cp):
        return font_cjk
    for pil_font, cmap_set in fallback_list[:-1]:
        if cp in cmap_set:
            return pil_font
    return font_cjk


def _line_width(text, font_quote, font_cjk, fallback_list):
    w = 0
    for ch in text:
        f = _best_font_for(ch, font_quote, font_cjk, fallback_list)
        bbox = f.getbbox(ch)
        w += bbox[2] - bbox[0]
    return w


def _wrap_text(text, font_quote, font_cjk, fallback_list, max_w, max_lines=4):
    words = text.split()
    lines, cur = [], ""
    for word in words:
        test = (cur + " " + word).strip()
        if _line_width(test, font_quote, font_cjk, fallback_list) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = word
            if len(lines) == max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and _line_width(lines[-1], font_quote, font_cjk, fallback_list) > max_w:
        while _line_width(lines[-1] + "…", font_quote, font_cjk, fallback_list) > max_w:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return lines


def _get_baseline_offset(font_a, font_b):
    return font_a.getbbox("A")[1] - font_b.getbbox("A")[1]


def _fit_text(text):
    """Shrink font sizes until the wrapped text fits within MAX_WIDTH, or MIN_SIZE is hit."""
    size_quote, size_name, size_handle = 36, 22, 17
    while size_quote >= MIN_SIZE:
        font_quote, font_name, font_handle, font_cjk, font_cjk_name, font_cjk_handle = _make_fonts(
            size_quote, size_name, size_handle
        )
        fallback_list = _build_fallback_list(font_quote, font_cjk, size_quote)
        wrapped_lines = _wrap_text(text, font_quote, font_cjk, fallback_list, MAX_WIDTH)
        if all(_line_width(l, font_quote, font_cjk, fallback_list) <= MAX_WIDTH for l in wrapped_lines):
            break
        size_quote  -= 2
        size_name   = max(12, size_name  - 1)
        size_handle = max(10, size_handle - 1)
    return wrapped_lines, font_quote, font_name, font_handle, font_cjk, font_cjk_name, font_cjk_handle, fallback_list


def render_quote(text: str, display_name: str, username: str, recorded_at: str,
                  avatar_data: bytes | None = None) -> io.BytesIO:
    """
    Build a quote card PNG and return it as an in-memory buffer, seeked to 0.

    text: the quoted message content
    display_name / username: author's display name and @handle
    recorded_at: pre-formatted timestamp string
    avatar_data: raw bytes of the author's avatar image, or None to omit it
    """
    (wrapped_lines, font_quote, font_name, font_handle,
     font_cjk, font_cjk_name, font_cjk_handle, fallback_list) = _fit_text(text)

    img  = Image.new("RGB", (W, H), (10, 10, 15))
    draw = ImageDraw.Draw(img)

    if avatar_data:
        avatar = Image.open(io.BytesIO(avatar_data)).convert("RGBA").resize((AV, AV))
        fade = Image.new("L", (AV, AV), 0)
        fd = ImageDraw.Draw(fade)
        for x in range(AV):
            t = x / AV
            alpha = int(255 * max(0.0, 1.0 - t ** 1.4))
            fd.line([(x, 0), (x, AV - 1)], fill=alpha)
        avatar.putalpha(fade)
        img.paste(avatar, (0, 0), avatar)

    draw.rectangle([ACCENT_X, PAD_Y, ACCENT_X + 3, H - PAD_Y], fill=(255, 255, 255))

    line_h     = font_quote.getbbox("Ag")[3] + 10
    name_h     = font_name.getbbox("Ag")[3] + 4
    recorded_h = font_handle.getbbox("Ag")[3] + 6
    handle_h   = font_handle.getbbox("Ag")[3]
    gap        = 14
    block_h    = line_h * len(wrapped_lines) + gap + name_h + handle_h + recorded_h
    text_y     = (H - block_h) // 2

    cjk_offset_quote  = _get_baseline_offset(font_quote,  font_cjk)
    cjk_offset_name   = _get_baseline_offset(font_name,   font_cjk_name)
    cjk_offset_handle = _get_baseline_offset(font_handle, font_cjk_handle)

    def draw_mixed_line(x, y, line, fill):
        cx = x
        for ch in line:
            f = _best_font_for(ch, font_quote, font_cjk, fallback_list)
            offset = cjk_offset_quote if f is font_cjk else 0
            draw.text((cx, y + offset), ch, font=f, fill=fill)
            bbox = f.getbbox(ch)
            cx += bbox[2] - bbox[0]

    def draw_mixed_line_sized(x, y, line, fill, font_default, cjk_font, cjk_offset):
        cx = x
        for ch in line:
            cp = ord(ch)
            if _is_cjk_char(cp):
                chosen, offset = cjk_font, cjk_offset
            else:
                chosen, offset = font_default, 0
                for pil_font, cmap_set in fallback_list[:-1]:
                    if cp in cmap_set:
                        break
                else:
                    chosen, offset = cjk_font, cjk_offset
            draw.text((cx, y + offset), ch, font=chosen, fill=fill)
            bbox = chosen.getbbox(ch)
            cx += bbox[2] - bbox[0]

    for i, line in enumerate(wrapped_lines):
        draw_mixed_line(TEXT_X, text_y + i * line_h, line, (255, 255, 255))

    author_y = text_y + line_h * len(wrapped_lines) + gap
    draw_mixed_line_sized(TEXT_X, author_y, f"— {display_name}",
                          (210, 210, 215), font_name, font_cjk_name, cjk_offset_name)
    draw_mixed_line_sized(TEXT_X, author_y + name_h, f"@{username}",
                          (130, 130, 140), font_handle, font_cjk_handle, cjk_offset_handle)
    draw_mixed_line_sized(TEXT_X, author_y + name_h + handle_h + 4, recorded_at,
                          (90, 90, 100), font_handle, font_cjk_handle, cjk_offset_handle)

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer
