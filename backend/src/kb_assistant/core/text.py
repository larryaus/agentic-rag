import re

# ECMAScript WhiteSpace and LineTerminator, shared by trim() and /\s+/.
JS_WHITESPACE = "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
JS_SPACES = re.compile("[" + JS_WHITESPACE + "]+")


def js_trim(text: str) -> str:
    return text.strip(JS_WHITESPACE)


def reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")
