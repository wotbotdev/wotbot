FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src/ src/
RUN pip install --no-cache-dir '.[browser]'
# alembic.ini and migrations/ ship inside the installed package (see
# pyproject force-include), so no separate COPY is needed.

# The default image serves the API, agent workers and isolated panel validator.
# Keep the browser install in its own layer; validation only needs headless Chromium.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home validator

EXPOSE 8123 8919

CMD ["wotbot", "serve"]
