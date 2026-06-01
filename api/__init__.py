"""FastAPI consumer of the ``legal`` data pipeline.

This package adapts HTTP requests to the uniform dispatch seam in
``legal.dispatch`` and echoes the normalized envelope 1:1 with the CLI. It adds
no source-access logic of its own beyond request/response adaptation.
"""
