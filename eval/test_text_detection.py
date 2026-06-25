"""Text/binary detection eval gate (test_text_detection.py).

Regression guard for the binary-detection bug: the previous `_looks_like_text`
decoded an 8 KB head slice as UTF-8 and treated a decode error as "binary".
A multi-byte UTF-8 character straddling the 8192-byte cut raised
UnicodeDecodeError, so valid UTF-8 markdown with long lines was silently
misclassified as binary and routed to _failed/ -- dropping real articles.

These tests pin the contract of the NUL-only detector (git's heuristic: a file
is binary iff it contains a NUL byte). They are deterministic and make NO real
LLM calls.

RED/GREEN: every case marked OLD-WRONG below fails against the pre-fix
slice-decode implementation and passes against the NUL-only implementation.

SAFETY: isolated tmp_path dirs only; never touches live wiki runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from wiki_weaver.lib import _looks_like_text  # noqa: E402


# (name, bytes, expected_is_text, was_old_wrong)
_CASES: list[tuple[str, bytes, bool, bool]] = [
    # clean ASCII markdown -> TEXT (control)
    ("clean_ascii_md", b"# Title\n\nA normal short markdown body.\n", True, False),
    # empty file -> TEXT (git treats empty as non-binary; empties handled downstream)
    ("empty", b"", True, False),
    # valid UTF-8 with a 3-byte char (U+2019) straddling byte 8192 -> TEXT.
    # Root cause in isolation: old slice-decode splits the char at the 8 KB cut.
    (
        "utf8_multibyte_at_8192",
        b"x" * 8191 + "\u2019".encode() + b" tail text\n",
        True,
        True,  # OLD wrongly said BINARY
    ),
    # latin-1 smart quote (0x92) + en-dash (0x96), no NUL, invalid UTF-8 -> TEXT.
    # Readable text in a non-UTF-8 encoding. OLD: decode fails -> BINARY.
    ("latin1_smartquote_no_nul", b"It\x92s a deal \x96 ok.\n", True, True),
    # >8 KB clean text then a NUL at ~byte 9000 -> BINARY (real NUL, past old
    # 8 KB window). OLD only sniffed first 8 KB -> missed it -> wrongly TEXT.
    ("nul_past_window", b"a" * 9000 + b"\x00" + b"more\n", False, True),
    # UTF-16-LE markdown -> BINARY (NUL bytes throughout; engine can't read it).
    ("utf16_markdown", "# Heading\n\nBody.\n".encode("utf-16-le"), False, False),
    # real PNG header (has NUL) -> BINARY (true-positive control)
    ("real_png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x01\x02", False, False),
]


@pytest.mark.parametrize(
    "name,data,expected,_old_wrong", _CASES, ids=[c[0] for c in _CASES]
)
def test_text_detection(
    tmp_path: Path, name: str, data: bytes, expected: bool, _old_wrong: bool
) -> None:
    p = tmp_path / name
    p.write_bytes(data)
    got = _looks_like_text(p)
    label = lambda b: "TEXT" if b else "BINARY"  # noqa: E731
    assert got is expected, f"{name}: expected {label(expected)}, got {label(got)}"


def test_regression_anchor_long_line_with_smart_quotes(tmp_path: Path) -> None:
    """The shape of the real article the pilot silently dropped: one very long
    line (>8 KB) containing UTF-8 smart quotes -- must classify as TEXT."""
    line = ("Here\u2019s a hack \u2014 " * 600).encode()  # one long UTF-8 line, >8 KB
    assert len(line) > 8192
    p = tmp_path / "long_smartquote_line.md"
    p.write_bytes(b"---\ntitle: x\n---\n\n" + line + b"\n")
    assert _looks_like_text(p) is True
