"""Telegram output formatting: LaTeX cleanup, MarkdownV2 escaping, chunking.

Pipeline used by the handlers is :func:`prepare_for_telegram`:

1. :func:`latex_to_unicode` - convert leftover LaTeX to readable Unicode math.
2. :func:`escape_markdown_v2` - escape every MarkdownV2 special character.
3. Re-enable the four structured section headers (allowlist un-escaping) so
   ``📥 *Input*`` still renders bold.
4. :func:`split_message` - chunk below Telegram's 4096 character limit.

Escaping happens AFTER the LaTeX conversion, otherwise the conversion would
have to deal with backslash-escaped braces.
"""

from __future__ import annotations

import re

TELEGRAM_MAX_MESSAGE_LEN = 4096
DEFAULT_CHUNK_LEN = 4000

# Characters Telegram requires to be escaped in MarkdownV2, plus the backslash
# itself (an unescaped trailing backslash also breaks entity parsing).
_MARKDOWN_V2_SPECIALS = r"_*[]()~`>#+-=|{}.!\\"
_ESCAPE_RE = re.compile(r"([%s])" % re.escape(_MARKDOWN_V2_SPECIALS))
_UNESCAPE_RE = re.compile(r"\\([%s])" % re.escape(_MARKDOWN_V2_SPECIALS))

# Section headers we intentionally keep as bold markdown.
_SECTION_NAMES = ("Input", "Result", "Details", "Notes")
_HEADER_RE = re.compile(
    r"(?m)^([ \t]*)((?:📥|✅|📊|💡)?[ \t]*)\\\*(%s)\\\*" % "|".join(_SECTION_NAMES)
)

# --------------------------------------------------------------------- LaTeX

_SYMBOLS: dict[str, str] = {
    # Greek
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ",
    r"\epsilon": "ε", r"\varepsilon": "ε", r"\zeta": "ζ", r"\eta": "η",
    r"\theta": "θ", r"\vartheta": "θ", r"\iota": "ι", r"\kappa": "κ",
    r"\lambda": "λ", r"\mu": "μ", r"\nu": "ν", r"\xi": "ξ", r"\pi": "π",
    r"\rho": "ρ", r"\sigma": "σ", r"\tau": "τ", r"\upsilon": "υ",
    r"\phi": "φ", r"\varphi": "φ", r"\chi": "χ", r"\psi": "ψ", r"\omega": "ω",
    r"\Gamma": "Γ", r"\Delta": "∆", r"\Theta": "Θ", r"\Lambda": "Λ",
    r"\Xi": "Ξ", r"\Pi": "Π", r"\Sigma": "Σ", r"\Phi": "Φ", r"\Psi": "Ψ",
    r"\Omega": "Ω",
    # Operators and relations
    r"\times": "×", r"\div": "÷", r"\pm": "±", r"\mp": "∓", r"\cdot": "·",
    r"\leq": "≤", r"\le": "≤", r"\geq": "≥", r"\ge": "≥",
    r"\neq": "≠", r"\ne": "≠", r"\approx": "≈", r"\equiv": "≡",
    r"\sim": "∼", r"\propto": "∝", r"\ll": "≪", r"\gg": "≫",
    # Big operators / calculus
    r"\sum": "∑", r"\prod": "∏", r"\int": "∫", r"\iint": "∬",
    r"\oint": "∮", r"\partial": "∂", r"\nabla": "∇", r"\infty": "∞",
    r"\lim": "lim",
    # Sets and logic
    r"\in": "∈", r"\notin": "∉", r"\subset": "⊂", r"\subseteq": "⊆",
    r"\cup": "∪", r"\cap": "∩", r"\emptyset": "∅", r"\varnothing": "∅",
    r"\forall": "∀", r"\exists": "∃", r"\neg": "¬", r"\land": "∧",
    r"\lor": "∨", r"\therefore": "∴", r"\because": "∵",
    r"\mathbb{R}": "ℝ", r"\mathbb{N}": "ℕ", r"\mathbb{Z}": "ℤ",
    r"\mathbb{Q}": "ℚ", r"\mathbb{C}": "ℂ",
    # Arrows and misc
    r"\rightarrow": "→", r"\to": "→", r"\leftarrow": "←",
    r"\Rightarrow": "⇒", r"\Leftarrow": "⇐", r"\leftrightarrow": "↔",
    r"\Leftrightarrow": "⇔", r"\mapsto": "↦",
    r"\degree": "°", r"\circ": "°", r"\angle": "∠", r"\perp": "⊥",
    r"\parallel": "∥", r"\prime": "′", r"\ldots": "…", r"\dots": "…",
    r"\cdots": "⋯",
}
# Longest first so \varepsilon wins over \var... and \le never eats \leq.
_SYMBOL_RE = re.compile(
    "|".join(re.escape(key) for key in sorted(_SYMBOLS, key=len, reverse=True))
)

_SUPERSCRIPTS = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵",
    "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹", "+": "⁺", "-": "⁻",
    "=": "⁼", "(": "⁽", ")": "⁾", "n": "ⁿ", "i": "ⁱ",
}
_SUBSCRIPTS = {
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅",
    "6": "₆", "7": "₇", "8": "₈", "9": "₉", "+": "₊", "-": "₋",
    "=": "₌", "(": "₍", ")": "₎", "a": "ₐ", "e": "ₑ", "o": "ₒ",
    "x": "ₓ", "i": "ᵢ", "j": "ⱼ", "n": "ₙ", "m": "ₘ", "k": "ₖ", "t": "ₜ",
}

_FRAC_RE = re.compile(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_SQRT_N_RE = re.compile(r"\\sqrt\s*\[\s*([^\]]{1,4})\s*\]\s*\{([^{}]*)\}")
_SQRT_RE = re.compile(r"\\sqrt\s*\{([^{}]*)\}")
_TEXT_WRAPPER_RE = re.compile(
    r"\\(?:text|textrm|textbf|textit|mathrm|mathbf|mathit|mathsf|operatorname)\s*\{([^{}]*)\}"
)
_ENVIRONMENT_RE = re.compile(r"\\(?:begin|end)\s*\{[^{}]*\}")
# Inline math must not start or end with whitespace, so "$5 and $7" (currency)
# is never mistaken for a "$...$" pair.
_INLINE_MATH_RE = re.compile(r"\$(?![\s$])([^$\n]{1,400}?)(?<![\s$])\$")
_ESCAPED_DOLLAR = "\x00dollar\x00"
_SUP_BRACE_RE = re.compile(r"\^\s*\{([^{}]{1,12})\}")
_SUB_BRACE_RE = re.compile(r"_\s*\{([^{}]{1,12})\}")
_SUP_CHAR_RE = re.compile(r"\^\s*([0-9n+\-])")
_SUB_CHAR_RE = re.compile(r"_\s*([0-9a-ex+\-])")
_SPACING_RE = re.compile(r"\\[,;:!> ]")
_LEFTOVER_BRACE_CMD_RE = re.compile(r"\\[a-zA-Z]+\s*\{([^{}]*)\}")


def _translate(chars: str, table: dict[str, str]) -> str | None:
    """Map every character through ``table``; return None if any is unmappable."""
    out: list[str] = []
    for char in chars:
        replacement = table.get(char)
        if replacement is None:
            return None
        out.append(replacement)
    return "".join(out)


def _sup_repl(match: re.Match[str]) -> str:
    body = match.group(1).strip()
    converted = _translate(body, _SUPERSCRIPTS)
    return converted if converted is not None else f"^({body})"


def _sub_repl(match: re.Match[str]) -> str:
    body = match.group(1).strip()
    converted = _translate(body, _SUBSCRIPTS)
    return converted if converted is not None else f"_({body})"


def latex_to_unicode(text: str) -> str:
    """Convert the LaTeX constructs models commonly emit into Unicode math.

    Handles \\frac, \\sqrt, Greek letters, operators, super/subscripts and
    strips math-mode delimiters. Anything unrecognised is left untouched so no
    information is silently lost.
    """
    if not text:
        return ""

    result = text

    # Display math delimiters and environments. A literal "\$" is parked first
    # so it is never treated as a delimiter.
    result = result.replace(r"\$", _ESCAPED_DOLLAR)
    result = result.replace("$$", "")
    result = result.replace(r"\[", "").replace(r"\]", "")
    result = result.replace(r"\(", "(").replace(r"\)", ")")
    result = _ENVIRONMENT_RE.sub("", result)
    # Paired inline math: keep the content, drop the $ signs (so "$5" survives).
    result = _INLINE_MATH_RE.sub(r"\1", result)
    result = result.replace(_ESCAPED_DOLLAR, "$")

    # Text wrappers before anything else consumes their braces.
    for _ in range(5):
        new = _TEXT_WRAPPER_RE.sub(r"\1", result)
        if new == result:
            break
        result = new

    # Fractions and roots, innermost first. They are resolved in the same loop
    # because each can be nested inside the other (\frac{-b}{\sqrt{x}}): every
    # pass strips one brace level until nothing changes.
    for _ in range(8):
        new = _FRAC_RE.sub(r"(\1)/(\2)", result)
        new = _SQRT_N_RE.sub(lambda m: f"({m.group(2)})^(1/{m.group(1)})", new)
        new = _SQRT_RE.sub(r"√(\1)", new)
        if new == result:
            break
        result = new
    result = result.replace(r"\sqrt", "√")

    # Sizing hints carry no meaning in plain text.
    result = result.replace(r"\left", "").replace(r"\right", "")

    # Named symbols.
    result = _SYMBOL_RE.sub(lambda m: _SYMBOLS[m.group(0)], result)

    # Super/subscripts.
    result = _SUP_BRACE_RE.sub(_sup_repl, result)
    result = _SUB_BRACE_RE.sub(_sub_repl, result)
    result = _SUP_CHAR_RE.sub(lambda m: _SUPERSCRIPTS.get(m.group(1), f"^{m.group(1)}"), result)
    result = _SUB_CHAR_RE.sub(lambda m: _SUBSCRIPTS.get(m.group(1), f"_{m.group(1)}"), result)

    # Spacing macros and line breaks.
    result = _SPACING_RE.sub(" ", result)
    result = result.replace("\\\\", "\n")

    # Unknown \cmd{arg} -> arg (keeps the payload, drops the macro).
    for _ in range(3):
        new = _LEFTOVER_BRACE_CMD_RE.sub(r"\1", result)
        if new == result:
            break
        result = new

    # Tidy up whitespace introduced by the removals.
    result = re.sub(r"[ \t]{2,}", " ", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


# ------------------------------------------------------------------- escaping


def escape_markdown_v2(text: str) -> str:
    """Escape every character Telegram's MarkdownV2 parser treats as special."""
    if not text:
        return ""
    return _ESCAPE_RE.sub(r"\\\1", text)


def unescape_markdown_v2(text: str) -> str:
    """Inverse of :func:`escape_markdown_v2`, used for the plain-text fallback."""
    if not text:
        return ""
    return _UNESCAPE_RE.sub(r"\1", text)


def _restore_section_headers(escaped: str) -> str:
    """Un-escape the allowlisted ``📥 *Input*`` style headers so they stay bold."""
    return _HEADER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}*{m.group(3)}*", escaped)


# ------------------------------------------------------------------- chunking


def _trailing_backslashes(text: str) -> int:
    count = 0
    for char in reversed(text):
        if char == "\\":
            count += 1
        else:
            break
    return count


def _safe_cut(line: str, limit: int) -> int:
    """Largest cut index <= limit that never splits a ``\\x`` escape pair."""
    cut = min(limit, len(line))
    # Prefer a word boundary if one is reasonably close to the limit.
    window_start = max(1, cut - 200)
    space = line.rfind(" ", window_start, cut)
    if space > 0:
        cut = space
    while cut > 1 and _trailing_backslashes(line[:cut]) % 2 == 1:
        cut -= 1
    return max(cut, 1)


def _hard_wrap(line: str, max_len: int) -> list[str]:
    """Split a single over-long line into escape-safe pieces."""
    if len(line) <= max_len:
        return [line]
    pieces: list[str] = []
    remainder = line
    while len(remainder) > max_len:
        cut = _safe_cut(remainder, max_len)
        pieces.append(remainder[:cut].rstrip())
        remainder = remainder[cut:].lstrip()
        if not remainder:
            break
    if remainder:
        pieces.append(remainder)
    return pieces


def split_message(text: str, max_len: int = DEFAULT_CHUNK_LEN) -> list[str]:
    """Split ``text`` into chunks of at most ``max_len`` chars, on line breaks.

    Lines are kept intact whenever possible; a line longer than ``max_len`` is
    wrapped at a space (and never between a backslash and the character it
    escapes).
    """
    if not text:
        return []
    max_len = max(1, min(max_len, TELEGRAM_MAX_MESSAGE_LEN))
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    buffer = ""
    for raw_line in text.split("\n"):
        for piece in _hard_wrap(raw_line, max_len):
            if not buffer:
                buffer = piece
            elif len(buffer) + 1 + len(piece) <= max_len:
                buffer = f"{buffer}\n{piece}"
            else:
                chunks.append(buffer)
                buffer = piece
    if buffer:
        chunks.append(buffer)
    return [chunk for chunk in chunks if chunk.strip()]


# ------------------------------------------------------------------- pipeline


def prepare_for_telegram(raw: str, max_len: int = DEFAULT_CHUNK_LEN) -> list[str]:
    """Full pipeline: LaTeX -> Unicode, MarkdownV2 escape, header allowlist, split."""
    if not raw or not raw.strip():
        return []
    converted = latex_to_unicode(raw)
    escaped = escape_markdown_v2(converted)
    escaped = _restore_section_headers(escaped)
    return split_message(escaped, max_len=max_len)


__all__ = [
    "escape_markdown_v2",
    "unescape_markdown_v2",
    "latex_to_unicode",
    "split_message",
    "prepare_for_telegram",
    "TELEGRAM_MAX_MESSAGE_LEN",
    "DEFAULT_CHUNK_LEN",
]
