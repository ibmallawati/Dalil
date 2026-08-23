FROM python:3.11-slim
# Small, stable Python 3.11 image — matches your local pinning.

# Create a non-root user. Cloud Run, HF Spaces and Render all run containers
# as a low-privilege user rather than root, so files must be owned by this
# user or writes get "Permission denied".
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    HF_HOME=/home/user/.cache/huggingface
# ↑ HF_HOME = a writable folder for the embedding model to download into.

WORKDIR /home/user/app
# ↑ Work inside the user's own folder (writable), not root-owned /app.

ENV PYTHONUNBUFFERED=1
# ↑ Python block-buffers stdout when it isn't a TTY (true in any container).
# Without this, log lines can sit in a buffer and show up late/batched (or
# not at all before a crash) in any hosted log viewer, all of which read only
# stdout/stderr. This forces every write to flush immediately.

COPY --chown=user requirements.txt pyproject.toml ./
# ↑ --chown=user: the copied files belong to our user, not root.

RUN pip install --no-cache-dir -r requirements.txt

# Bake the embedding model INTO the image (~1.1 GB).
#
# Without this it downloads from the HF hub on first use — and because
# warm_embedder() runs before app.run(), the container does not open its port
# until that finishes. On a scale-to-zero host like Cloud Run that download
# repeats on EVERY cold start, inside the startup-probe window. Baking it in
# turns a network fetch into a local disk read.
#
# Must match EMBED_MODEL in chatbot.config, and must run as `user` so the
# weights land in this image's HF_HOME rather than a root-owned cache.
ARG EMBED_MODEL=intfloat/multilingual-e5-base
RUN python -c "from sentence_transformers import SentenceTransformer; \
    SentenceTransformer('${EMBED_MODEL}')"

COPY --chown=user src/ ./src/
# ↑ --no-deps: dependencies were already installed from requirements.txt
# above; this just registers the `chatbot` package itself (editable, so the
# COPY --chown=user . . below doesn't require a reinstall).
RUN pip install --no-cache-dir --no-deps -e .

COPY --chown=user . .
# ↑ Copy the rest of your code (templates/, static/, data/, etc), owned by
# our user.

EXPOSE 7860
# ↑ Documents the port the app listens on by default. The actual port is
# controlled at runtime via chatbot.config reading $PORT (Cloud Run sets 8080,
# Render sets its own; 7860 is the fallback for local runs and HF Spaces).

CMD ["python", "-m", "chatbot.app"]