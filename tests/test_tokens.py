from pathlib import Path

from spec_mcp.frontend import load_tokens

CSS = """
/* header comment with --fake-in-comment: 1 */
:root {
  --info-tint: #E3EEEF; /* tint */ --info-ink: #235860;
  --primary: var(--anchor); --primary-foreground: #FDFBF7;
  /* multi
     line --also-fake: 2
     comment */
  --r-control: 8px;
}
.dark { --info-ink: #9fd; --surface-2: #111; }
@media (prefers-color-scheme: dark) { :root { --ring: #fff; } }
"""


def test_load_tokens_many_per_line_and_blocks(tmp_path: Path):
    p = tmp_path / "tokens.css"
    p.write_text(CSS)
    toks = load_tokens(p)
    assert toks == ["--info-ink", "--info-tint", "--primary", "--primary-foreground", "--r-control", "--ring", "--surface-2"]
    assert "--anchor" not in toks  # a var() usage is not a declaration
    assert "--fake-in-comment" not in toks and "--also-fake" not in toks


def test_load_tokens_missing_file(tmp_path: Path):
    assert load_tokens(tmp_path / "nope.css") == []
