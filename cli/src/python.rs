//! Python semantics the checks lean on: `str` whitespace and line splitting, number parsing,
//! `Path.read_text(errors="ignore")`, and which lines `tokenize` reports comments on.

use std::collections::BTreeSet;
use std::path::Path;

use ruff_python_ast::token::{TokenFlags, TokenKind};
use ruff_python_parser::lexer::lex;
use ruff_python_parser::Mode;

/// `str.isspace`: Unicode White_Space plus the ASCII separators U+001C..U+001F.
pub fn is_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// `str.strip()` with no arguments.
pub fn strip(s: &str) -> &str {
    s.trim_matches(is_space)
}

/// `str.splitlines()`: `\r\n` is one break, and so are the rarer separators Python honours.
pub fn splitlines(s: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut chars = s.char_indices().peekable();
    while let Some((i, c)) = chars.next() {
        let breaks = matches!(
            c,
            '\n' | '\r'
                | '\u{0b}'
                | '\u{0c}'
                | '\u{1c}'
                | '\u{1d}'
                | '\u{1e}'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        );
        if !breaks {
            continue;
        }
        lines.push(&s[start..i]);
        start = i + c.len_utf8();
        if c == '\r' && chars.peek().map(|&(_, n)| n) == Some('\n') {
            chars.next();
            start += 1;
        }
    }
    if start < s.len() {
        lines.push(&s[start..]);
    }
    lines
}

/// Python allows `_` only between two digits in a numeric literal string.
fn without_digit_underscores(s: &str) -> Option<String> {
    let b = s.as_bytes();
    for (i, _) in s.match_indices('_') {
        let digit_before = i > 0 && b[i - 1].is_ascii_digit();
        let digit_after = b.get(i + 1).is_some_and(u8::is_ascii_digit);
        if !(digit_before && digit_after) {
            return None;
        }
    }
    Some(s.replace('_', ""))
}

/// `float(s)`, or None where Python raises ValueError.
pub fn parse_float(s: &str) -> Option<f64> {
    without_digit_underscores(strip(s))?.parse().ok()
}

/// `int(s)`, or None where Python raises ValueError.
pub fn parse_int(s: &str) -> Option<i64> {
    without_digit_underscores(strip(s))?.parse().ok()
}

/// `Path.read_text(errors="ignore")`: undecodable bytes dropped, universal newlines.
pub fn read_text(path: &Path) -> std::io::Result<String> {
    let bytes = std::fs::read(path)?;
    let text: String = bytes.utf8_chunks().map(|chunk| chunk.valid()).collect();
    Ok(text.replace("\r\n", "\n").replace('\r', "\n"))
}

/// CPython's tokenizer `MAXLEVEL`: one more open bracket is "too many nested parentheses".
const MAX_NESTING: usize = 200;

/// The 1-based lines `tokenize.generate_tokens` reports a COMMENT token on, or None where it raises.
///
/// Runs ruff's raw lexer, since its parser re-lexes around unclosed brackets and CPython never does.
/// The lexer recovers from errors CPython's tokenizer raises on and flags some it accepts, so the
/// raising cases are decided here: unterminated strings and brackets, a bad line continuation,
/// malformed numbers, NUL, non-printable characters, too deep nesting, and inconsistent indentation.
pub fn comment_lines(text: &str) -> Option<BTreeSet<usize>> {
    if text.contains('\0') {
        return None;
    }
    let line_starts: Vec<usize> = std::iter::once(0)
        .chain(text.match_indices('\n').map(|(i, _)| i + 1))
        .collect();
    let mut comments = BTreeSet::new();
    let (mut depth, mut open_interpolations) = (0usize, 0usize);
    let mut logical_starts = vec![0];
    let mut lexer = lex(text, Mode::Module);
    loop {
        let kind = lexer.next_token();
        let range = lexer.current_range();
        let (start, end) = (range.start().to_usize(), range.end().to_usize());
        if lexer.current_flags().contains(TokenFlags::UNCLOSED_STRING) {
            return None;
        }
        match kind {
            TokenKind::EndOfFile => break,
            TokenKind::Comment => {
                comments.insert(line_starts.partition_point(|&s| s <= start));
            }
            TokenKind::Lpar | TokenKind::Lsqb | TokenKind::Lbrace if depth >= MAX_NESTING => {
                return None
            }
            TokenKind::Lpar | TokenKind::Lsqb | TokenKind::Lbrace => depth += 1,
            TokenKind::Rpar | TokenKind::Rsqb | TokenKind::Rbrace => {
                depth = depth.saturating_sub(1)
            }
            TokenKind::FStringStart | TokenKind::TStringStart => open_interpolations += 1,
            TokenKind::FStringEnd | TokenKind::TStringEnd => open_interpolations -= 1,
            TokenKind::Newline => logical_starts.push(end),
            TokenKind::Int | TokenKind::Float | TokenKind::Complex
                if malformed_number(&text[start..end], &text[end..]) =>
            {
                return None
            }
            TokenKind::Unknown if unknown_raises(text, start, end) => return None,
            _ => {}
        }
    }
    if depth > 0
        || open_interpolations > 0
        || indentation_fault(text.as_bytes(), &logical_starts).is_some()
    {
        return None;
    }
    Some(comments)
}

/// Whether CPython's tokenizer raises where ruff's lexer emitted an error token.
fn unknown_raises(text: &str, start: usize, end: usize) -> bool {
    let tok = text[start..end].trim_start_matches([' ', '\t', '\x0c']);
    let Some(first) = tok.chars().next() else {
        return end == text.len();
    };
    match first {
        '\\' | '{' | '}' | '\'' | '"' => true,
        '0'..='9' | '.' => {
            let b = tok.as_bytes();
            (b.len() > 1 && b[0] == b'0' && b"xXoObB".contains(&b[1]))
                || malformed_number(tok, &text[end..])
        }
        c => tok.chars().count() == 1 && !is_printable(c),
    }
}

fn is_printable(c: char) -> bool {
    let format_char = matches!(
        c,
        '\u{ad}' | '\u{200b}'..='\u{200f}' | '\u{202a}'..='\u{202e}' | '\u{2060}'..='\u{2064}' | '\u{feff}'
    );
    c == ' ' || !(c.is_control() || c.is_whitespace() || format_char)
}

/// CPython rejects a literal that ruff splits into a number and a following name.
fn malformed_number(lit: &str, rest: &str) -> bool {
    let b = lit.as_bytes();
    let radix = b.len() > 1 && b[0] == b'0' && b"xXoObB".contains(&b[1]);
    let last = *b.last().unwrap_or(&b'0');
    let mut next = rest.bytes();
    let n1 = next.next();
    if n1 == Some(b'_') && last.is_ascii_hexdigit() {
        return true;
    }
    if radix {
        return b"oObB".contains(&b[1]) && n1.is_some_and(|c| c.is_ascii_digit());
    }
    let exponent_without_digits = matches!(n1, Some(b'e' | b'E'))
        && matches!(next.next(), Some(b'+' | b'-'))
        && !next.next().is_some_and(|c| c.is_ascii_digit());
    last != b'j' && last != b'J' && exponent_without_digits
}

/// Which error CPython's tokenizer raises on bad indentation.
#[derive(Debug, PartialEq)]
pub enum IndentFault {
    IndentationError,
    TabError,
}

/// CPython's indentation bookkeeping (tab stops of 8, cross-checked against tab stops of 1), run
/// at the start of every logical line; whitespace- and comment-only lines are skipped. Returns the
/// first fault with the offset of the line's first token.
pub fn indentation_fault(src: &[u8], logical_starts: &[usize]) -> Option<(usize, IndentFault)> {
    const MAX_INDENT: usize = 100;
    let mut stack = vec![(0usize, 0usize)];
    for &start in logical_starts {
        let mut pos = start;
        loop {
            let (mut col, mut alt, mut cont) = (0usize, 0usize, 0usize);
            loop {
                match src.get(pos) {
                    Some(b' ') => (col, alt) = (col + 1, alt + 1),
                    Some(b'\t') => (col, alt) = ((col / 8 + 1) * 8, alt + 1),
                    Some(b'\x0c') => (col, alt) = (0, 0),
                    Some(b'\\') if src.get(pos + 1) == Some(&b'\n') => {
                        if cont == 0 {
                            cont = col;
                        }
                        pos += 1;
                    }
                    _ => break,
                }
                pos += 1;
            }
            match src.get(pos) {
                None => break,
                Some(b'#' | b'\n' | b'\r') => match src[pos..].iter().position(|&c| c == b'\n') {
                    Some(nl) => pos += nl + 1,
                    None => break,
                },
                Some(_) => {
                    if cont != 0 {
                        (col, alt) = (cont, cont);
                    }
                    let (top, alt_top) = *stack.last().unwrap();
                    if col > top {
                        if stack.len() >= MAX_INDENT {
                            return Some((pos, IndentFault::IndentationError));
                        }
                        if alt <= alt_top {
                            return Some((pos, IndentFault::TabError));
                        }
                        stack.push((col, alt));
                    } else {
                        while stack.len() > 1 && col < stack.last().unwrap().0 {
                            stack.pop();
                        }
                        let (top, alt_top) = *stack.last().unwrap();
                        if col != top {
                            return Some((pos, IndentFault::IndentationError));
                        }
                        if alt != alt_top {
                            return Some((pos, IndentFault::TabError));
                        }
                    }
                    break;
                }
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splitlines_matches_python() {
        assert_eq!(splitlines("a\r\nb\rc\nd"), ["a", "b", "c", "d"]);
        assert_eq!(splitlines("a\n\nb\n"), ["a", "", "b"]);
        assert_eq!(splitlines("a\x0cb\u{2028}c"), ["a", "b", "c"]);
        assert!(splitlines("").is_empty());
    }

    #[test]
    fn strip_includes_ascii_separators() {
        assert_eq!(strip("\u{1f} x\t\u{a0}"), "x");
    }

    #[test]
    fn numbers_parse_like_python() {
        assert_eq!(parse_float(" 0.5 "), Some(0.5));
        assert_eq!(parse_float("1_0"), Some(10.0));
        assert!(parse_float("inf").is_some_and(f64::is_infinite));
        assert_eq!(parse_float("1__0"), None);
        assert_eq!(parse_float(""), None);
        assert_eq!(parse_int("+2_40"), Some(240));
        assert_eq!(parse_int("2.0"), None);
    }

    /// Expected values come from CPython 3.12+'s `tokenize.generate_tokens`; `None` is where it raises.
    #[test]
    fn comment_lines_match_tokenize() {
        let cases: &[(&str, Option<&[usize]>)] = &[
            ("x = 1  # c\n", Some(&[1])),
            ("def broken(:\n    'unterminated\n", None),
            ("def (\n", None),
            ("x = (1 +\n# c\n", None),
            ("x = ]\nf(\n", None),
            ("x = ]\nf()\n", Some(&[])),
            ("x = (]\n", Some(&[])),
            ("if x:\n        a\n    b\n", None),
            ("x = $ # c\n", Some(&[1])),
            ("x = 0777\n", Some(&[])),
            ("x = 1_\n", None),
            ("x = 1_j\n", None),
            ("x = 1j_\n", Some(&[])),
            ("x = 1._5\n", Some(&[])),
            ("x = 0x1_\n", None),
            ("x = 0b12\n", None),
            ("x = 0x\n", None),
            ("x = 1e\n", Some(&[])),
            ("x = 1e+x\n", None),
            ("x = 1abc\n", Some(&[])),
            ("s = '''\n# not\n'''  # yes\n", Some(&[3])),
            ("f'{x # c\n}'\n", Some(&[1])),
            ("f'{'\n", None),
            ("x = f'abc\n", None),
            ("x = fr\"\"\"\n{a}(\n", None),
            ("f(1,\nx = 2\n# c\n", None),
            ("x = (1,\n  y)\n  # c\nz = 2\n", Some(&[3])),
            ("f'}'\n", None),
            ("x = 1 \\\n# c\n", Some(&[2])),
            ("x = 1 \\ y\n", None),
            ("\tif x:\n  \tpass\n", None),
            ("if x:\n\tpass\n        pass\n", None),
            ("if x:\n  pass\n   # c\n  y\n", Some(&[3])),
            ("if x:\n    a\n  \\\nb\n", None),
            ("if x:\n  a\n\\\n  b\n", Some(&[])),
            ("if x:\n    a\n  ", Some(&[])),
            ("\x0c  x = 1\n\x0c y = 2\n", None),
            ("# c\x0c\n#d\n", Some(&[1, 2])),
            ("x = 1\x0b # c\n", None),
            ("x = 1 # c\x00\n", None),
            ("\u{feff}# c\n", Some(&[1])),
            ("x = 1 # c", Some(&[1])),
            ("", Some(&[])),
        ];
        for (src, want) in cases {
            let want = want.map(|w| w.iter().copied().collect::<BTreeSet<_>>());
            assert_eq!(comment_lines(src), want, "{src:?}");
        }
    }
}
