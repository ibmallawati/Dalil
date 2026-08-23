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

# No model is baked in any more, and none is downloaded at runtime.
# Embeddings go over HTTPS (see chatbot.rag.retrieval), which is what takes
# this image from ~1.2 GB of resident memory down to well under 512 MB —
# the difference between deployable on a free host and not.

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