"""CSV-driven test runner (design doc §6, §11, §12).

One CSV row per test case. Columns use dotted prefixes (seedN.*, call.*, resp.*,
dbN.*, kafkaN.*); small maps live in a single cell as ``key=value;key=value``.
"""
