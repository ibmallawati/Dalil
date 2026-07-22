FROM python:3.11-slim
# Small, stable Python 3.11 image — matches your local pinning.

# Create a non-root user. Render (like HF Spaces) runs containers as a
# low-privilege user rather than root, so files must be owned by this
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
# not at all before a crash) in Render's log dashboard, which only reads
# stdout/stderr. This forces every write to flush immediately.

COPY --chown=user requirements.txt pyproject.toml ./
# ↑ --chown=user: the copied files belong to our user, not root.

RUN pip install --no-cache-dir -r requirements.txt

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
# controlled at runtime via chatbot.config reading $PORT (Render sets its
# own; this falls back to 7860 for local runs or HF Spaces).

CMD ["python", "-m", "chatbot.app"]