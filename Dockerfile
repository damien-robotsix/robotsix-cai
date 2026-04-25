# Clean-slate pydantic-first reimplementation of robotsix-cai.
#
# Empty stub for now — only enough to build a runnable image.
# Fleshed out as the rewrite progresses.

FROM python:3.12-slim

RUN groupadd --system --gid 1000 cai \
    && useradd --system --gid cai --uid 1000 --create-home --shell /bin/bash cai

RUN pip install --no-cache-dir --root-user-action=ignore pydantic pydantic-settings

USER cai
WORKDIR /app

CMD ["python", "-c", "print('cai stub — empty image, no app yet')"]
