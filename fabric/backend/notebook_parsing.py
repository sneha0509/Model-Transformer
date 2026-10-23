"""Static notebook parsers with source-located diagnostics."""

import ast
import warnings

from sqlglot.dialects.spark import Spark


def parse_python(source, filename, issues=None):
    """Collect source warnings on inventory; avoid repeating escape warnings on later passes."""
    with warnings.catch_warnings(record=issues is not None) as caught:
        for category in (SyntaxWarning, DeprecationWarning):
            if issues is None:
                warnings.filterwarnings("ignore", message="invalid escape sequence", category=category)
            else:
                warnings.simplefilter("always", category)
        try:
            return ast.parse(source, filename=filename)
        finally:
            if caught is not None:
                for warning in caught:
                    issue = {
                        "file": filename, "line": warning.lineno,
                        "message": f"Python {warning.category.__name__}: {warning.message}",
                    }
                    if issue not in issues:
                        issues.append(issue)


class _NotebookSparkParser(Spark.Parser):
    def _warn_unsupported(self):
        # The scanner reports Command nodes as gaps with notebook/line evidence.
        pass


def parse_spark_sql(query):
    dialect = Spark()
    return _NotebookSparkParser(dialect=dialect).parse(dialect.tokenize(query), query)


def sql_placeholders(query):
    """Mask whole Python expressions, including nested braces and quoted strings."""
    parts, placeholders = [], {}
    end = 0
    while (start := query.find("{", end)) != -1:
        depth, position, quote = 1, start + 1, ""
        while position < len(query) and depth:
            character = query[position]
            if quote:
                if character == "\\":
                    position += 2
                    continue
                if query.startswith(quote, position):
                    position += len(quote)
                    quote = ""
                    continue
            elif character in {"'", '"'}:
                quote = character * 3 if query.startswith(character * 3, position) else character
                position += len(quote)
                continue
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
            position += 1
        if depth:
            break
        key = f"__fabric_dynamic_{len(placeholders)}__"
        while key in query or key in placeholders:
            key = "_" + key
        placeholders[key] = query[start:position]
        parts.extend((query[end:start], key))
        end = position
    parts.append(query[end:])
    return "".join(parts), placeholders
